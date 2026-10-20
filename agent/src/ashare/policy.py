"""Deterministic configurable drawdown policy and transparent observation filters.

No broker actions. These outputs guide a human; they cannot cap realised losses.
Deposits/withdrawals change account units, never erase drawdown history.
"""
from __future__ import annotations

import math
from copy import deepcopy
from datetime import datetime, timedelta

from .calendar import quote_quality

_EPS = 1e-12


def drawdown_thresholds(settings: dict) -> dict:
    return {"warning_pct": settings.get("warning_drawdown_pct", 3),
            "restrict_pct": settings.get("restrict_drawdown_pct", 4),
            "max_drawdown_pct": settings.get("max_drawdown_pct", 5)}


def risk_assessment(account: dict | None, quotes: dict, ledger: dict, settings: dict,
                    clock: dict, now: datetime) -> tuple[dict, dict]:
    updated = deepcopy(ledger)
    period = updated.get("monitoring_period")
    thresholds = drawdown_thresholds(settings)
    warning, restrict, maximum = (thresholds[k] / 100 for k in ("warning_pct", "restrict_pct", "max_drawdown_pct"))
    base = {**thresholds, "initialized": ledger.get("units") is not None,
            "configured": account is not None, "complete": False, "equity": None,
            "drawdown_pct": None, "latched": bool((period or ledger).get("breached")),
            "drawdown_basis": "monitoring_period" if period else "historical_high",
            "monitoring_period": deepcopy(period), "historical_drawdown_pct": None,
            "historical_latched": bool(ledger.get("breached")),
            "selection_allowed": False, "holdings": [], "conditions": [],
            "basis": "live_quote" if clock["live_window"] else "reference_quote"}
    if account is None:
        return updated, {**base, "level": "incomplete", "guidance": "请先录入现金、持仓及净值高点；账户风险尚未启用"}
    problems = []
    if not clock["calendar_verified"]:
        problems.append("交易日历未核验")
    if str(account.get("confirmed_at", ""))[:10] < (clock["expected_quote_date"] or "9999"):
        problems.append("持仓及现金尚未在本交易日确认")
    holdings = []
    for position in account["positions"]:
        symbol = position["symbol"]
        quote = quotes.get(symbol)
        quality = quote_quality(quote, clock, now)
        valid = quality in {"fresh", "reference"}
        if not valid:
            problems.append(f"{symbol} 报价不可用于风控：{quality}")
        value = position["quantity"] * quote["price"] if valid else None
        cost = position.get("cost_price")
        holdings.append({**position, "name": (quote or {}).get("name", symbol),
                         "price": quote["price"] if valid else None, "market_value": value,
                         "quality": quality, "locked_quantity": position["quantity"] - position["available_quantity"],
                         "cost_pnl_pct": (quote["price"] / cost - 1) * 100 if valid and cost else None})
    base["holdings"] = holdings
    if problems:
        return updated, {**base, "level": "critical" if base["latched"] else "incomplete",
                         "guidance": "暂停新增仓位判断；" + "；".join(problems),
                         "conditions": [{"key": "account_data", "severity": "warning", "message": "；".join(problems)}]}
    equity = math.fsum([account["cash"], *[p["market_value"] for p in holdings]])
    if not math.isfinite(equity) or equity <= 0:
        return updated, {**base, "level": "incomplete", "guidance": "账户净值必须大于零"}
    units = ledger.get("units")
    flow = ledger.get("pending_flow", 0)
    if units is None:
        units = equity
        updated["peak_nav"] = max(1, account["historical_peak_equity"] / equity)
    elif flow:
        pre_flow_equity = equity - flow
        if pre_flow_equity <= 0:
            return updated, {**base, "level": "incomplete", "guidance": "出入金与净值不一致，无法进行份额化调整"}
        flow_nav = pre_flow_equity / units
        units += flow / flow_nav
    nav = equity / units
    peak_nav = max(updated["peak_nav"], nav)
    historical_drawdown = max(0, 1 - nav / peak_nav)
    if historical_drawdown + _EPS >= maximum:
        updated["breached"] = True
    updated.update(units=units, peak_nav=peak_nav, last_nav=nav, last_equity=equity, pending_flow=0)
    active_peak = peak_nav
    if period:
        period["peak_nav"] = max(period["peak_nav"], nav)
        active_peak = period["peak_nav"]
    drawdown = max(0, 1 - nav / active_peak)
    if period and drawdown + _EPS >= maximum:
        period["breached"] = True
    latched = bool((period or updated).get("breached"))
    level = "critical" if latched else "restrict" if drawdown + _EPS >= restrict else "warning" if drawdown + _EPS >= warning else "normal"
    guidance = {"critical": f"风险状态已锁定（最大回撤阈值 {thresholds['max_drawdown_pct']:g}%）：暂停新增仓位；核对可卖仓位并手动处理风险，缓解后需人工确认恢复",
                "restrict": f"回撤达到 {thresholds['restrict_pct']:g}%：暂停新增仓位判断，优先检查持仓风险",
                "warning": f"回撤达到 {thresholds['warning_pct']:g}%：预警，控制风险敞口并检查可卖仓位",
                "normal": "未触发回撤预警；仍须手动核对交易条件"}[level]
    conditions = []
    if level != "normal":
        conditions.append({"key": "drawdown_" + level, "severity": "critical" if latched else "warning",
                           "risk_period_id": period["id"] if period else None,
                           "message": f"{'本监测阶段' if period else '历史高点口径'}账户回撤 {drawdown * 100:.2f}%。{guidance}"})
    for holding in holdings:
        weight = holding["market_value"] / equity * 100
        holding["weight_pct"] = weight
        if weight > settings["max_single_position_pct"] + _EPS:
            conditions.append({"key": "concentration_" + holding["symbol"], "severity": "warning",
                               "message": f"{holding['symbol']} 仓位 {weight:.1f}%，超过单股观察上限 {settings['max_single_position_pct']:g}%"})
        if holding["locked_quantity"]:
            conditions.append({"key": "locked_" + holding["symbol"], "severity": "info",
                               "message": f"{holding['symbol']} 可卖 {holding['available_quantity']} 股，录入的不可卖仓位 {holding['locked_quantity']} 股；请在券商端核对 T+1"})
    return updated, {**base, "level": level, "complete": True, "equity": equity,
                     "cash": account["cash"], "drawdown_pct": drawdown * 100,
                     "peak_equity_equivalent": units * active_peak, "latched": latched,
                     "monitoring_period": deepcopy(period),
                     "historical_drawdown_pct": historical_drawdown * 100,
                     "historical_peak_equity_equivalent": units * peak_nav,
                     "historical_gap_to_peak": max(0, units * peak_nav - equity),
                     "historical_latched": bool(updated.get("breached")),
                     "period_return_pct": (nav / period["start_nav"] - 1) * 100 if period else None,
                     "budget_to_5pct": max(0, equity - units * active_peak * 0.95),
                     "budget_to_limit": max(0, equity - units * active_peak * (1 - maximum)),
                     "selection_allowed": clock["continuous"] and not latched and drawdown + _EPS < restrict,
                     "guidance": guidance, "conditions": conditions,
                     "confirmed_at": account["confirmed_at"],
                     "method": "份额化净值相对本监测阶段高点；历史高点与亏损另行保留，出入金调整份额" if period else "份额化净值相对历史高点；出入金调整份额，保留历史回撤"}


def observations(pool: dict, quotes: dict, history: dict, clock: dict, now: datetime,
                 *, pool_valid: bool) -> list[dict]:
    result = []
    for rank, row in enumerate(pool.get("rows", []), 1):
        quote = quotes.get(row["symbol"])
        quality = quote_quality(quote, clock, now)
        item = {**row, "quote": quote, "quality": quality, "flags": [], "score": None,
                "candidate": False, "five_minute_change_pct": None, "label": "等待有效报价"}
        if quote is None or quality not in {"fresh", "reference"}:
            result.append(item)
            continue
        price, amount = quote["price"], quote.get("amount_yuan") or 0
        change = quote["change_pct"]
        vwap, opening = quote.get("vwap"), quote.get("open")
        turnover = quote.get("turnover_pct")
        stamp = datetime.fromisoformat(quote["as_of"])
        before = [point for point in history.get(row["symbol"], [])
                  if 300 <= (stamp - datetime.fromisoformat(point["as_of"])).total_seconds() <= 390]
        if before:
            item["five_minute_change_pct"] = (price / before[-1]["price"] - 1) * 100
        flags = item["flags"]
        if "ST" in quote["name"].upper() or "退" in quote["name"]:
            flags.append("风险警示/退市标记，排除候选")
        if amount < 100_000_000:
            flags.append("成交额不足 1 亿元")
        if turnover is None:
            flags.append("缺少换手率")
        elif turnover > 20:
            flags.append("换手超过 20%，高波动")
        if change >= 7:
            flags.append("日涨幅达到 7%，注意追高")
        if (row.get("net_buy_amount") or 0) <= 0:
            flags.append("龙虎榜净买额非正，存在资金分歧")
        for key, label in (("limit_up", "接近涨停"), ("limit_down", "接近跌停")):
            band = quote.get(key)
            if band and abs(price - band) <= max(0.02, band * 0.002):
                flags.append(label)
        high = quote.get("high") or price
        item["pullback_from_high_pct"] = (1 - price / high) * 100 if high > 0 else None
        if high > 0 and price / high <= 0.97:
            flags.append("较日内高点回落至少 3%")
        score = 20 * (row.get("net_buy_amount", 0) is not None and (row.get("net_buy_amount") or 0) > 0)
        score += 25 * min(1, amount / 1_000_000_000)
        score += 20 * bool(opening and price > opening)
        score += 20 * bool(vwap and price >= vwap)
        score += 15 * (1 - (rank - 1) / max(1, len(pool.get("rows", []))))
        item["score"] = round(score, 1)
        item["candidate"] = bool(pool_valid and clock["continuous"] and quality == "fresh" and not flags and
                                 0 < change < 7 and opening and price > opening and vwap and price >= vwap)
        item["label"] = "条件观察" if item["candidate"] else "休市复盘" if not clock["continuous"] else "条件未满足"
        result.append(item)
    return sorted(result, key=lambda row: (not row["candidate"], -(row["score"] or 0), row["symbol"]))
