"""Local virtual portfolio only. No broker, account ledger, or order APIs."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import json
import os
import uuid

from .calendar import is_session, market_clock, quote_quality
from .plan import digest
from .rotation import stocks as active_stocks


def identity(plan):
    return {k: plan.get(k) for k in ('plan_id', 'version', 'content_hash')}


def create(plan, cash, policy, now):
    if not plan.get('published_at') or digest(plan) != plan.get('content_hash'):
        raise ValueError('模拟组合需要有效的本地已发布计划')
    if now.date().isoformat() > plan['session_date']:
        raise ValueError('计划已到期，不能补造历史模拟买入')
    return {'schema_version': 1, 'mode': 'local_simulation_only', 'enabled': True,
        'created_at': now.isoformat(), 'plan': deepcopy(plan), 'plan_identity': identity(plan),
        'initial_cash': cash, 'cash': cash, 'peak_equity': cash, 'risk_latched': False,
        'policy': deepcopy(policy), 'allocation_pct': 10, 'reserve_pct': 50,
        'fee_rate': .0015, 'slippage_rate': .001, 'positions': {}, 'trades': [],
        'entered_symbols': [], 'waiting': {}, 'checked_at': None}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name('.paper-' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def metrics(state, quotes, now):
    clock = market_clock(now)
    holdings, complete = [], True
    for symbol, position in state['positions'].items():
        q = quotes.get(symbol)
        quality = quote_quality(q, clock, now)
        valid = quality in {'fresh', 'reference'}
        complete &= valid
        value = position['quantity'] * q['price'] if valid else None
        holdings.append({**position, 'symbol': symbol, 'price': q['price'] if valid else None,
            'quote_as_of': (q or {}).get('as_of'), 'quality': quality, 'market_value': value,
            'available_quantity': position['quantity'] if now.date().isoformat() >= position['sellable_date'] else 0,
            'unrealized_pnl': round(value - position['cost_total'], 2) if valid else None})
    equity = round(state['cash'] + sum(h['market_value'] for h in holdings), 2) if complete else None
    peak = max(state['peak_equity'], equity or 0)
    dd = max(0, (1 - equity / peak) * 100) if equity is not None else None
    policy = state['policy']
    level = ('unknown' if dd is None else 'critical' if state['risk_latched'] or dd >= policy['max_drawdown_pct'] else
        'restrict' if dd >= policy['restrict_pct'] else 'warning' if dd >= policy['warning_pct'] else 'normal')
    return {'complete': complete, 'equity': equity, 'peak_equity': peak, 'drawdown_pct': dd,
        'return_pct': (equity / state['initial_cash'] - 1) * 100 if equity is not None else None,
        'pnl': round(equity - state['initial_cash'], 2) if equity is not None else None,
        'realized_pnl': round(sum(t.get('realized_pnl', 0) for t in state['trades']), 2),
        'level': level, 'holdings': holdings}


def next_session(day):
    for offset in range(1, 20):
        target = day + timedelta(days=offset)
        verified = is_session(target)
        if verified is None:
            return None
        if verified:
            return target.isoformat()
    return None


def execution_price(q, buy, state):
    price = Decimal(str(q['price'])) * (1 + Decimal(str(state['slippage_rate'])) * (1 if buy else -1))
    return float(price.quantize(Decimal('.01'), rounding=ROUND_CEILING if buy else ROUND_FLOOR))


def executable(q, now, price):
    if quote_quality(q, market_clock(now), now) != 'fresh' or q.get('high') == q.get('low'):
        return False
    if not q.get('limit_up') or not q.get('limit_down'):
        return False
    return q['limit_down'] + .02 < price < q['limit_up'] - .02


def advance(state, view, quotes, now, *, monitoring_enabled=True, plan_matches=True):
    """Pure reducer. `view` must be recomputed with this virtual account's risk."""
    s = deepcopy(state)
    m = metrics(s, quotes, now)
    if m['equity'] is not None:
        s['peak_equity'] = m['peak_equity']
        if m['drawdown_pct'] >= s['policy']['max_drawdown_pct']:
            s['risk_latched'] = True
    clock = market_clock(now)
    rows = {r['symbol']: r for r in view.get('rows', [])}
    active = bool(s['enabled'] and monitoring_enabled and clock['continuous'] and clock['calendar_verified'])
    for symbol, position in list(s['positions'].items()):
        q = quotes.get(symbol)
        if quote_quality(q, clock, now) != 'fresh':
            continue
        rules = position.get('rules') or next(r['rules'] for r in s['plan']['stocks'] if r['symbol'] == symbol)
        row = rows.get(symbol, {})
        news_blockers = [b for b in row.get('risk_blockers', []) if not b.startswith('账户风控')]
        reason = ('模拟账户达到最大回撤阈值' if s['risk_latched'] else
            '失守承接位/当日VWAP' if q['price'] < rules['hold'] or q.get('vwap') and q['price'] < q['vwap'] else
            '新增公告或新闻限制' if news_blockers and plan_matches else None)
        if reason:
            position.setdefault('pending_exit', {'reason': reason, 'at': now.isoformat()})
        pending = position.get('pending_exit')
        price = execution_price(q, False, s)
        if (not active or not pending or now.date().isoformat() < position['sellable_date']
            or not executable(q, now, price) or q['as_of'] <= position['quote_as_of']):
            continue
        gross = round(price * position['quantity'], 2)
        fee = round(gross * s['fee_rate'], 2)
        s['cash'] = round(s['cash'] + gross - fee, 2)
        s['trades'].append({'side': 'sell', 'simulation': True, 'symbol': symbol, 'name': position['name'],
            'quantity': position['quantity'], 'price': price, 'fee': fee, 'at': now.isoformat(),
            'quote_as_of': q['as_of'], 'reason': pending['reason'],
            'realized_pnl': round(gross - fee - position['cost_total'], 2)})
        del s['positions'][symbol]
    m = metrics(s, quotes, now)
    target_day = s['plan']['session_date'] == now.date().isoformat()
    for stock in active_stocks(s):
        symbol = stock['symbol']; row = rows.get(symbol, {}); q = quotes.get(symbol)
        reasons = []
        if not s['enabled'] or not monitoring_enabled: reasons.append('模拟或后台监控已暂停')
        if not plan_matches: reasons.append('当前计划身份不匹配，暂停新增模拟仓位')
        if not target_day: reasons.append('仅计划目标日允许新增，不顺延或回填')
        if not active or (now.hour, now.minute) < (9, 45): reasons.append('等待目标日9:45后连续竞价')
        if m['level'] in {'restrict', 'critical', 'unknown'}: reasons.append('模拟账户风控限制新增')
        reasons.extend(row.get('reasons', []))
        if not row.get('entry_ready'): reasons.append('统一计划价格及新闻公告门槛未全部通过')
        if symbol in s['entered_symbols']: reasons.append('本试验每股仅一次建仓，不反复回填')
        if len(s['positions']) >= 5: reasons.append('已有五只模拟持仓，等待原仓位可卖并退出后再补位')
        if symbol in s['positions']:
            continue
        price = execution_price(q, True, s) if q else None
        if not q or not executable(q, now, price): reasons.append('报价无效、一字价格、涨跌停限制或滑点价不可用')
        evidence = row.get('price_evidence') or {}
        if (not evidence.get('ready') or not evidence.get('retest_at') or not evidence.get('volume_ratio')
            or not evidence.get('bar_as_of')): reasons.append('缺少完整5分钟放量突破回踩证据')
        elif datetime.fromisoformat(evidence['retest_at']) < datetime.fromisoformat(s['created_at']):
            reasons.append('信号早于模拟创建时间，不补造历史成交')
        sellable = next_session(now.date())
        if not sellable: reasons.append('下一交易日未核验，暂停模拟建仓')
        s['waiting'][symbol] = list(dict.fromkeys(reasons))
        if reasons:
            continue
        budget = min(s['initial_cash'] * .1, s['cash'] - s['initial_cash'] * .5)
        quantity = int(budget / (price * (1 + s['fee_rate'])) / 100) * 100
        if quantity <= 0:
            s['waiting'][symbol] = ['预算不足一个整手或需保留50%现金']
            continue
        gross = round(price * quantity, 2); fee = round(gross * s['fee_rate'], 2)
        s['cash'] = round(s['cash'] - gross - fee, 2)
        position = {'symbol': symbol, 'name': stock['name'], 'quantity': quantity,
            'entry_price': price, 'cost_total': round(gross + fee, 2), 'entered_at': now.isoformat(),
            'quote_as_of': q['as_of'], 'sellable_date': sellable, 'evidence': deepcopy(evidence)}
        position['rules'] = deepcopy(stock['rules'])
        s['positions'][symbol] = position; s['entered_symbols'].append(symbol)
        s['trades'].append({'side': 'buy', 'simulation': True, 'symbol': symbol, 'name': stock['name'],
            'quantity': quantity, 'price': price, 'fee': fee, 'at': now.isoformat(),
            'quote_as_of': q['as_of'], 'reason': '统一计划条件全部通过（模拟账户独立风控）', 'evidence': deepcopy(evidence)})
    s['checked_at'] = now.isoformat()
    return s


def snapshot(state, quotes, now, *, plan_matches=True):
    m = metrics(state, quotes, now)
    rows = []
    display = {x['symbol']:x for x in active_stocks(state)}
    for symbol, p in state['positions'].items():
        display.setdefault(symbol, {'symbol':symbol,'name':p['name'],'rules':p.get('rules') or next(x['rules'] for x in state['plan']['stocks'] if x['symbol']==symbol)})
    for stock in display.values():
        p = next((p for p in m['holdings'] if p['symbol'] == stock['symbol']), None)
        rows.append({'symbol': stock['symbol'], 'name': stock['name'], 'budget': state['initial_cash'] * .1,
            'rules': stock['rules'], 'position': p, 'waiting': state['waiting'].get(stock['symbol'], ['等待条件检查'])})
    return {k: deepcopy(state[k]) for k in ('mode', 'enabled', 'created_at', 'checked_at', 'plan_identity',
        'initial_cash', 'cash', 'allocation_pct', 'reserve_pct', 'fee_rate', 'slippage_rate', 'policy', 'trades')} | {
        'risk': m, 'rows': rows, 'plan_matches': plan_matches, 'rotation': deepcopy(state.get('rotation')),
        'note': '纯本地模拟；摩擦成本假设每边0.15%并另计0.10%滑点，不是券商实际收费。公开报价不能证明真实可成交，T+1限制与缺失数据均保留。'}
