"""Frozen public research ranking, independent of account and execution gates.

Scores are an explicit, uncalibrated research rubric, never probabilities.
The input, all dispositions, cutoff, and weights travel with the dated plan.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re

from .calendar import is_session, prior_session

VERSION = 'mainboard-research-v1'
MAINBOARD = re.compile(r'(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ)$')
EVENT_POINTS = {'restructure': 30, 'order': 28, 'earnings': 24, 'buyback': 18,
                'capacity': 14, 'theme': 8, 'none': 0}
METHOD = {
    'version': VERSION,
    'event_points': EVENT_POINTS,
    'new_event_bonus': 5, 'known_event_multiplier': .5,
    'original_source_points': 10, 'unreviewed_source_points': 3,
    'momentum_max': 20, 'liquidity_max': 15, 'theme_max': 10,
    'crowding_penalty_max': 10, 'risk_penalty_max': 25,
    'note': '事前固定的研究比较规则，未经收益或涨停概率校准；交易条件与账户风控独立计算。',
}


def checksum(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def event_type(text):
    # Administrative documents and purchases are not new customer orders.
    if re.search('业绩说明会|监管协议|一致行动|关联交易协议|法律意见|公告精选|公告集锦|暂缓|终止.*收购', text): return 'none'
    if re.search('限制性股票|股权激励.*回购注销|回购注销.*激励', text): return 'none'
    if re.search('采购合同|项目投资协议|扩建.*协议|建设.*协议', text): return 'capacity'
    if re.search('收购|重组|复牌', text): return 'restructure'
    if re.search('中标|订单|合同|定点|签约|签订|签署', text): return 'order'
    if re.search('三季|净利|业绩|预增|扭亏', text): return 'earnings'
    if re.search('回购|增持', text): return 'buyback'
    if re.search('投产|扩产|获批|获准|涨价|提价|贷款承诺函', text): return 'capacity'
    return 'none'


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def score_candidate(candidate, reference_date, cutoff):
    c = deepcopy(candidate)
    symbol, name = c['symbol'], c['name']
    q, evidence, review = c.get('quote') or {}, c.get('evidence', []), c.get('review') or {}
    excluded = []
    if not MAINBOARD.fullmatch(symbol) or re.search(r'ST|退|^[NC]', name, re.I):
        excluded.append('超出沪深普通主板范围或风险警示/新股标记')
    if (q.get('symbol') != symbol or q.get('as_of', '')[:10] != reference_date
        or not all(_number(q.get(k)) and q[k] > 0 for k in ('price', 'previous_close', 'open', 'high', 'low'))
        or q['low'] > min(q['open'], q['price']) or q['high'] < max(q['open'], q['price'])):
        excluded.append('参考日公开报价缺失或口径不匹配')
    for e in evidence:
        stamp = datetime.fromisoformat(e['published_at'])
        if stamp.tzinfo is None or stamp > cutoff:
            raise ValueError('候选证据晚于冻结截止时间或缺少时区')
        if not e.get('url', '').startswith('https://'):
            raise ValueError('候选必须保留公开原文链接')
    direct = [e for e in evidence if e.get('relation') == 'direct']
    kind = review.get('event_type') or max((event_type(e['title']) for e in direct),
        key=lambda x: EVENT_POINTS[x], default='none')
    if kind not in EVENT_POINTS: raise ValueError('未知催化类型')
    novelty = review.get('novelty', 'unknown')
    if novelty not in {'new', 'known', 'unknown'}: raise ValueError('未知消息新旧标签')
    original = bool(review.get('original_reviewed'))
    if original and (not review.get('evidence_quote') or not any(
            review['evidence_quote'] in e.get('source_text', '') for e in direct)):
        raise ValueError('原文核验标记必须有可逐字回查的直接证据')
    if review.get('exclude_reason'): excluded.append(review['exclude_reason'])
    change = (q.get('price', 0) / q.get('previous_close', 1) - 1) * 100 if q.get('previous_close') else 0
    event = EVENT_POINTS[kind] if direct else 0
    if novelty == 'new' and event: event += METHOD['new_event_bonus']
    if novelty == 'known': event *= METHOD['known_event_multiplier']
    source = 10 if original else 3 if direct else 0
    momentum = max(0, min(12, change * 1.2))
    if q.get('vwap') and q.get('price', 0) >= q['vwap']: momentum += 4
    if q.get('high') and q.get('price', 0) >= q['high'] * .99: momentum += 4
    amount, turnover = q.get('amount_yuan') or 0, q.get('turnover_pct')
    liquidity = (8 if amount >= 500_000_000 else 5 if amount >= 100_000_000 else 0)
    liquidity += 4 if _number(turnover) and .5 <= turnover <= 15 else 2 if _number(turnover) and 0 < turnover <= 20 else 0
    liquidity += 3 if q.get('high') != q.get('low') else 0
    theme = review.get('theme_points', 0)
    if not _number(theme) or not 0 <= theme <= 10: raise ValueError('板块研究分必须在0到10之间')
    if theme and not review.get('theme_source_url', '').startswith('https://'):
        raise ValueError('板块研究分必须保留关联来源，不能推断公司订单')
    risk = review.get('risk_penalty', 0)
    if not _number(risk) or not 0 <= risk <= 25: raise ValueError('风险扣分必须在0到25之间')
    if risk and not review.get('risk_points'): raise ValueError('风险扣分必须有原因')
    crowding = (5 if change >= 9 else 0) + (5 if q.get('high') == q.get('low') or (_number(turnover) and turnover > 20) else 0)
    parts = {'catalyst': round(event, 2), 'source': source, 'momentum': round(momentum, 2),
             'liquidity': liquidity, 'theme': theme, 'crowding_penalty': crowding, 'risk_penalty': risk}
    total = round(event + source + momentum + liquidity + theme - crowding - risk, 2)
    admission = []
    if not direct or kind == 'none': admission.append('缺少可比较的直接催化，保留价格强度候补')
    if not original: admission.append('直接催化原文尚未核验，不能进入发布五股')
    c.update(score=total, score_parts=parts, event_type=kind, novelty=novelty,
        excluded_reasons=excluded, admission_reasons=admission,
        eligible=not excluded and not admission, entry_ready=False)
    return c


def build_audit(inputs):
    cutoff = datetime.fromisoformat(inputs['cutoff'])
    target = datetime.fromisoformat(inputs['target_date']).date()
    reference = inputs['reference_date']
    if (cutoff.tzinfo is None or is_session(target) is not True
        or str(prior_session(target)) != reference or cutoff.date() >= target):
        raise ValueError('研究截止必须早于目标交易日，参考日须为已核验的上一交易日')
    if inputs.get('method_version') != VERSION: raise ValueError('研究规则版本不匹配')
    candidates = inputs['candidates']
    if not 5 <= len(candidates) <= 500 or len({c['symbol'] for c in candidates}) != len(candidates):
        raise ValueError('候选数量或去重核验失败')
    coverage = inputs.get('coverage', {})
    if not coverage.get('directory_verified') or coverage.get('reference_quote_coverage', 0) < .9:
        raise ValueError('主板名录或参考日报价覆盖不足90%，停止发布研究排名')
    if any(coverage.get(k) is False for k in ('cls_interval_complete','announcement_pages_complete')):
        raise ValueError('所声明的快讯时段或公告分页未采集完整，停止发布排名')
    rows = [score_candidate(c, reference, cutoff) for c in candidates]
    rows.sort(key=lambda r: (-r['score'], r['symbol']))
    selected = [r['symbol'] for r in rows if r['eligible']][:5]
    if len(selected) != 5: raise ValueError('未取得五只原文核验候选，禁止补凑发布名单')
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
        row['selected'] = row['symbol'] in selected
        row['decision'] = ('入选观察计划；仍需盘中量价与独立风险门槛' if row['selected'] else
            '；'.join(row['excluded_reasons'] + row['admission_reasons']) or
            f"同口径研究分低于第五只入选股（{next(r['score'] for r in rows if r['symbol'] == selected[-1]):g}分），保留候补")
    result = {'run_id': inputs['run_id'], 'method': METHOD, 'inputs': deepcopy(inputs), 'rows': rows,
        'selected_symbols': selected, 'candidate_count': len(rows), 'cutoff': inputs['cutoff'],
        'target_date': inputs['target_date'], 'reference_date': reference,
        'coverage': deepcopy(coverage), 'validation_status': '未经收益或涨停概率样本外验证',
        'input_hash': checksum(inputs)}
    result['content_hash'] = checksum(result)
    return result


def validate_audit(audit):
    rebuilt = build_audit(audit['inputs'])
    if rebuilt != audit: raise ValueError('冻结研究排名、分项或内容校验失败')
    return rebuilt


def evaluate_outcome(audit, quotes):
    """Price outcomes only. Never turn hypothetical returns into executed P&L."""
    validate_audit(audit)
    rows = []
    for c in audit['rows']:
        q = quotes.get(c['symbol']) or {}
        valid = q.get('as_of', '')[:10] == audit['target_date'] and q.get('price') and q.get('open') and q.get('previous_close')
        rows.append({'symbol': c['symbol'], 'name': c['name'], 'rank': c['rank'], 'selected': c['selected'],
            'available': bool(valid), 'source_time': q.get('as_of'),
            'day_change_pct': round((q['price']/q['previous_close']-1)*100, 4) if valid else None,
            'open_to_close_pct': round((q['price']/q['open']-1)*100, 4) if valid else None,
            'closed_limit_up': abs(q['price']-q['limit_up']) < .005 if valid and q.get('limit_up') else None})
    selected = [r for r in rows if r['selected'] and r['available']]
    all_valid = [r for r in rows if r['available']]
    return {'run_id': audit['run_id'], 'research_hash': audit['content_hash'], 'target_date': audit['target_date'],
        'rows': rows, 'selected_coverage': len(selected), 'candidate_coverage': len(all_valid),
        'selected_limit_up_count': sum(r['closed_limit_up'] is True for r in selected),
        'selected_mean_day_change_pct': round(sum(r['day_change_pct'] for r in selected)/5, 4) if len(selected)==5 else None,
        'selected_mean_open_to_close_pct': round(sum(r['open_to_close_pct'] for r in selected)/5, 4) if len(selected)==5 else None,
        'candidate_mean_open_to_close_pct': round(sum(r['open_to_close_pct'] for r in all_valid)/len(all_valid), 4) if all_valid else None,
        'note': '冻结候选的价格结果，未扣费；非实际成交收益。缺失报价不补零，全市场涨停召回率另需同口径完整名单。'}
