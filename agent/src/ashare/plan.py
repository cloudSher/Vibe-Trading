"""Published, versioned observation plans. Pure evaluation; never orders."""
from datetime import datetime, timedelta
from copy import deepcopy
import hashlib
import json

from .calendar import SHANGHAI, quote_quality
from .models import FocusPlan


def digest(plan):
    value = FocusPlan.model_validate(plan).model_dump(mode="json", exclude={"published_at", "content_hash"})
    # Retain the exact checksum of previously published plans without research.
    if value.get('screening') is None:
        value.pop('screening', None)
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def authorized_symbols(focus):
    """Detect mixed plans/versions; the local relay is the trusted input, not news."""
    manifest = focus.get("manifest")
    if not manifest:
        if focus.get("plan_id"):
            raise ValueError("发布计划缺少内容清单")
        return None
    value = FocusPlan.model_validate(manifest)
    if not value.plan_id or not value.published_at or digest(manifest) != value.content_hash:
        raise ValueError("计划内容校验失败")
    identity = {k: manifest[k] for k in ("plan_id", "version", "content_hash")}
    if any(focus.get(k) != v for k, v in identity.items()) or focus.get("session_date") != manifest["session_date"]:
        raise ValueError("计划版本不一致")
    rows = focus.get("rows", [])
    if len(rows) != len(value.stocks) or [r["symbol"] for r in rows] != [s.symbol for s in value.stocks]:
        raise ValueError("计划名单不一致")
    for row, stock in zip(rows, value.stocks):
        if row.get("rules") != stock.rules.model_dump() or row.get("name") != stock.name:
            raise ValueError("计划观察价位不一致")
    return frozenset(s.symbol for s in value.stocks)


def _stamp(bar):
    return datetime.fromisoformat(bar["as_of"])


def _adjacent(a, b):
    return (_stamp(b) - _stamp(a)).total_seconds() == 300


def price_evidence(stock, state, quote, now):
    """Only complete provider M5 bars; no synthetic bars made from polled ticks."""
    rules = stock["rules"]
    result = {"ready": False, "invalidated": False, "stage": "waiting", "breakout_at": None,
              "retest_at": None, "volume_ratio": None, "bar_as_of": None, "reasons": [],
              "bar_source": state.get("source"), "source_warning": state.get("source_warning"),
              "vwap_method": "quote_amount_volume"}
    bars = [b for b in state.get("items", []) if _stamp(b).date() == now.date()
            and _stamp(b) + timedelta(seconds=5) <= now]
    result["bar_as_of"] = bars[-1]["as_of"] if bars else None
    checked = state.get("checked_at")
    age = (now - datetime.fromisoformat(checked)).total_seconds() if checked else float("inf")
    if state.get("error") or not 0 <= age <= 150 or not bars or not 0 <= (now - _stamp(bars[-1])).total_seconds() <= 390:
        result.update(stage="data_missing", reasons=["当日完整5分钟K线缺失、过期或获取失败，不能确认放量与回踩"])
        return result
    price = quote["price"]
    if (rules.get("strong_invalid_below") and price < rules["strong_invalid_below"]) or (
            bars[-1]["close"] < rules["invalid_below"] and price < rules["invalid_below"]):
        result.update(invalidated=True, stage="invalidated", reasons=[f"已失守失效位 {rules['invalid_below']:.2f}，取消未执行观察"])
        return result
    # The previous three bars must be contiguous. Lunch and missing bars cannot
    # supply a made-up volume baseline. The retest is the next complete M5 bar.
    attempts = []
    for i in range(3, len(bars) - 1):
        baseline, bar, retest = bars[i-3:i], bars[i], bars[i+1]
        if (_stamp(bar).hour, _stamp(bar).minute) < (9, 45):
            continue
        if not all(_adjacent(a, b) for a, b in zip(bars[i-3:i+1], bars[i-2:i+2])):
            continue
        average = sum(b["volume_shares"] for b in baseline) / 3
        ratio = bar["volume_shares"] / average if average > 0 else 0
        if not (baseline[-1]["close"] <= rules["breakout"] < bar["close"] and ratio >= rules["breakout_volume_ratio"]):
            continue
        attempts.append(i)
        if (retest["low"] <= rules["breakout"] * (1 + rules["retest_tolerance_pct"] / 100)
                and retest["low"] >= rules["hold"] and retest["close"] >= rules["breakout"]):
            # A prior confirmation does not survive a later broken hold or
            # intraday VWAP. Recompute VWAP only from a complete day of bars.
            subsequent = bars[i+1:]
            broken = any(b["low"] < rules["hold"] for b in subsequent)
            if _stamp(bars[0]).hour == 9 and _stamp(bars[0]).minute == 35 and all(b.get("amount_yuan") is not None for b in bars):
                volume = amount = 0
                for j, b in enumerate(bars):
                    volume += b["volume_shares"]
                    amount += b["amount_yuan"]
                    if j > i and volume > 0 and b["close"] < amount / volume:
                        broken = True
            if broken:
                result.update(invalidated=True, stage="invalidated", reasons=["突破后失守承接位或当日均价，原观察已失效"])
                continue
            if (now - _stamp(retest)).total_seconds() <= 600 and price >= rules["breakout"]:
                if quote.get("vwap") and price < quote["vwap"]:
                    result.update(invalidated=True, stage="invalidated", reasons=["突破后最新价失守当日VWAP，原观察失效"])
                    continue
                return result | {"ready": True, "invalidated": False, "stage": "confirmed", "reasons": [],
                    "breakout_at": bar["as_of"], "retest_at": retest["as_of"], "volume_ratio": round(ratio, 3)}
    if not result["invalidated"]:
        result["reasons"] = ["等待放量突破与下一根完整5分钟K线回踩承接" if not attempts else "突破后的完整5分钟回踩未通过或确认已过期"]
    return result


def published_view(plan, disclosures, quotes, risk, clock, now, *, enabled, news, bars):
    plan = deepcopy(plan)
    identity = {k: plan[k] for k in ("plan_id", "version", "content_hash")}
    current = now.date().isoformat() == plan["session_date"]
    checked = disclosures.get("checked_at")
    age = (now - datetime.fromisoformat(checked)).total_seconds() if checked else float("inf")
    fresh = not disclosures.get("error") and 0 <= age <= 600
    rows, conditions = [], []
    research_rows = {r['symbol']: r for r in (plan.get('screening') or {}).get('rows', [])}
    for stock in plan["stocks"]:
        s, rules = stock["symbol"], stock["rules"]
        q = quotes.get(s)
        quality = quote_quality(q, clock, now)
        anns = [a for a in disclosures.get("items", []) if s in a["symbols"]]
        new_ids = [a["id"] for a in anns if a["id"] not in stock["reviewed_ids"]]
        stock_news = (news or {}).get("stocks", {}).get(s)
        blockers = []
        if not fresh: blockers.append("公告检查过期或失败")
        if new_ids: blockers.append("有新增未核验公告")
        if stock["disclosure_status"] in {"blocked", "pending"}: blockers.append("公告风险尚未解除，暂停买入观察确认")
        if plan.get("news_enabled"): blockers.extend((stock_news or {}).get("blocked", ["等待新闻采集与关联检查"]))
        if not risk["selection_allowed"]: blockers.append("账户风控未通过，不能作为买入确认；完整持仓与净值基准请在账户页核对")
        reasons = []
        if not current: reasons.append("计划已到期，未自动顺延" if now.date().isoformat() > plan["session_date"] else "等待目标交易日")
        if not enabled or not plan["enabled"]: reasons.append("监控暂停")
        if not clock["continuous"]: reasons.append("非连续竞价，盘前/盘后只作观察准备")
        if (now.hour, now.minute) < (9, 45): reasons.append("9:45前仅观察")
        if quality != "fresh": reasons.append("报价非有效盘中报价（要求90秒内）")
        evidence = {"ready": False, "invalidated": False, "stage": "waiting", "reasons": []}
        if current and clock["continuous"] and quality == "fresh" and (now.hour, now.minute) >= (9, 45):
            evidence = price_evidence(stock, bars.get(s, {}), q, now)
            reasons.extend(evidence["reasons"])
            if evidence.get("bar_as_of") and datetime.fromisoformat(q["as_of"]) < datetime.fromisoformat(evidence["bar_as_of"]):
                reasons.append("报价早于最新完整5分钟K线，等待数据同步")
            price, vwap = q["price"], q.get("vwap")
            if not q.get("previous_close") or abs(q["previous_close"] - stock["reference_close"]) > .011:
                reasons.append("行情昨收与计划参考收盘不一致，可能除权或更正，需重审价位")
            if not vwap or not q.get("low") or not q.get("high") or not q["low"] * .995 <= vwap <= q["high"] * 1.005:
                reasons.append("当日VWAP口径无法核验")
            elif price < vwap or (price / vwap - 1) * 100 > rules["max_vwap_deviation_pct"]:
                reasons.append("未站稳当日VWAP或偏离超过2%，不追价")
            if price < rules["hold"]: reasons.append("未站稳计划承接位")
            if not q.get("open") or (q["open"] / stock["reference_close"] - 1) * 100 >= plan["max_open_gap_pct"]:
                reasons.append("开盘价缺失或高开达到3%，不追价")
            if (q.get("amount_yuan") or 0) < 100_000_000: reasons.append("当日成交额不足1亿元")
            if q.get("turnover_pct") is None or q["turnover_pct"] > 20: reasons.append("换手率缺失或超过20%")
            if not 0 < q["change_pct"] < 7: reasons.append("当前涨幅不在0%至7%观察范围")
            if "ST" in q["name"].upper() or "退" in q["name"] or q.get("high") == q.get("low"):
                reasons.append("风险警示/退市或一字价格")
            if any(q.get(k) and abs(price - q[k]) <= max(.02, q[k] * .002) for k in ("limit_up", "limit_down")):
                reasons.append("接近涨跌停，不能确认可成交机会")
            if q.get("high") and price / q["high"] <= .97: reasons.append("较日内高点回落达到3%")
        market_ready = bool(evidence["ready"] and not reasons)
        entry = market_ready and not blockers
        status = ("计划已到期" if now.date().isoformat() > plan["session_date"] else
                  "盘前等待" if current and (now.hour, now.minute) < (9, 30) else
                  "观察已失效" if evidence["invalidated"] else
                  "买入观察条件满足" if entry else "量价满足，风险限制中" if market_ready else "等待观察条件")
        observation = ["仅目标日9:45后，报价≤90秒；当日VWAP重新计算",
            f"放量突破 {rules['breakout']:.2f}（完整5分钟成交量≥此前3根均量的{rules['breakout_volume_ratio']:g}倍）",
            f"下一根完整5分钟K线回踩：低点不低于 {rules['hold']:.2f}，触及突破位上方{rules['retest_tolerance_pct']:g}%以内且收盘≥{rules['breakout']:.2f}；确认保留10分钟",
            "高开≥3%或偏离当日VWAP>2%不追；成交额≥1亿元、换手≤20%、涨幅0~7%",
            "公告、新闻核验与完整账户风控均须通过；满足仅提示观察，手动决策"]
        invalidation = [f"完整5分钟K线收于 {rules['invalid_below']:.2f} 下方且最新价未收复，或突破后失守承接位/当日VWAP",
            *([f"最新价低于 {rules['strong_invalid_below']:.2f}，立即取消未执行观察"] if rules.get("strong_invalid_below") else []),
            "新增未核验/负面公告或新闻、旧价位遇除权除息需重审、报价或5分钟K线过期，均停止确认",
            f"账户回撤{risk.get('warning_pct', 12):g}%预警、{risk.get('restrict_pct', 16):g}%暂停新增、{risk.get('max_drawdown_pct', 20):g}%最大阈值；不保证实际损失不越线；新买A股T+1"]
        row = {**stock, "quote": q, "quality": quality, "market_ready": market_ready, "entry_ready": entry,
            "status": status, "confirmations": 0, "new_disclosure_ids": new_ids, "announcements": anns,
            "news": stock_news, "reasons": list(dict.fromkeys(reasons + blockers)), "risk_blockers": blockers,
            "risk_status": "限制中" if blockers else "核验通过，仍有市场风险", "price_evidence": evidence,
            "observation_conditions": observation, "invalidation_conditions": invalidation}
        rows.append(row)
        if s in research_rows:
            research = research_rows[s]
            row['research'] = {k: research[k] for k in ('rank', 'score', 'score_parts', 'decision')}
        if market_ready:
            conditions.append({"key": "focus_ready_" + s, "severity": "warning" if entry else "info",
                "message": f"{stock['name']} {s}：{status}。" + ("；".join(blockers) if blockers else "须本人核对并手动操作，新买A股T+1")})
        if current and evidence["invalidated"]:
            conditions.append({"key": "focus_break_" + s, "severity": "warning", "message": f"{stock['name']} {s} 观察已失效：" + "；".join(evidence["reasons"])})
        backlog = [a for a in anns if a["id"] in new_ids and a.get("date", plan["session_date"]) < plan["session_date"]]
        if backlog:
            conditions.append({"key": "focus_disclosure_backlog_" + s, "severity": "warning",
                "announcement_ids": [a["id"] for a in backlog],
                "message": f"{stock['name']} {s} 有{len(backlog)}份此前公告待原文核验，属于当前计划的历史风险背景，不是今日新催化；暂停买入观察确认"})
        for ann in anns:
            if ann["id"] in new_ids:
                if ann in backlog:
                    continue
                conditions.append({"key": "focus_disclosure_" + s + "_" + ann["id"], "severity": "warning",
                    "message": f"{stock['name']} {s} 未核验公告（标注披露日{ann.get('date', '待核对')}，不等于源发布时间）：{ann.get('title', '')[:180]}，暂停买入观察确认"})
        if current and not fresh:
            conditions.append({"key": "focus_disclosure_feed", "severity": "warning", "message": "重点计划公告检查不可用或过期"})
        if current and clock["continuous"] and evidence["stage"] == "data_missing":
            conditions.append({"key": "focus_bars_" + s, "severity": "warning", "message": f"{stock['name']} {s}：" + "；".join(evidence["reasons"])})
    return {**identity, "manifest": plan, "published_at": plan["published_at"], "research_cutoff": plan["research_cutoff"],
        "configured": True, "enabled": enabled and plan["enabled"], "session_date": plan["session_date"],
        "reference_date": plan["reference_date"], "review_since": plan["review_since"], "reviewed_at": plan["reviewed_at"],
        "disclosures_checked_at": checked, "disclosures_fresh": fresh, "disclosure_error": disclosures.get("error"),
        "max_open_gap_pct": plan["max_open_gap_pct"], "rows": rows,
        "lifecycle": "expired" if now.date().isoformat() > plan["session_date"] else "current" if current else "scheduled",
        "risk_policy": {k: risk.get(k) for k in ("level", "selection_allowed", "drawdown_basis", "warning_pct", "restrict_pct", "max_drawdown_pct")},
        "conditions": list({c["key"]: c | {"plan_identity": identity} for c in conditions}.values()),
        "rule_note": "页面、Codex提醒及会话以此版本为准；主板雷达候选另列，不自动替换计划。真实5分钟K线放量突破与回踩规则未经收益回测；旧闻仅作计划背景。"}
