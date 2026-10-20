"""Pure, dated focus-watch rules. No orders and no model dependency."""
from datetime import datetime

from .calendar import market_clock, quote_quality


def focus_view(plan, disclosures, quotes, history, risk, clock, now, *, enabled=True, news=None, disclosure_monitoring=False, bars=None):
    if not plan:
        return {"configured": False, "rows": [], "conditions": []}
    if plan.get("plan_id"):
        from .plan import published_view
        return published_view(plan, disclosures, quotes, risk, clock, now, enabled=enabled, news=news, bars=bars or {})
    conditions, rows = [], []
    valid_day = now.date().isoformat() == plan["session_date"]
    if valid_day and not risk["configured"]:
        conditions.append({"key": "focus_account_missing", "severity": "warning",
                           "message": "尚未录入实际账户，回撤风控无法计算；行情满足也不能作为买入确认"})
    checked = disclosures.get("checked_at")
    disclosure_fresh = bool(checked and not disclosures.get("error") and
                            (now - datetime.fromisoformat(checked)).total_seconds() <= 600)
    items = disclosures.get("items", [])
    for stock in plan["stocks"]:
        symbol = stock["symbol"]
        stock_news = (news or {}).get("stocks", {}).get(symbol)
        quote = quotes.get(symbol)
        quality = quote_quality(quote, clock, now)
        announcements = [item for item in items if symbol in item["symbols"]]
        new_ids = [item["id"] for item in announcements if item["id"] not in stock["reviewed_ids"]]
        review_ok = disclosure_fresh and not new_ids and stock["disclosure_status"] in {"reviewed", "caution"}
        reasons = []
        if not enabled or not plan["enabled"]:
            reasons.append("监控暂停")
        if not valid_day:
            reasons.append("等待目标交易日" if now.date().isoformat() < plan["session_date"] else "计划已到期，未自动顺延")
        if not clock["continuous"]:
            reasons.append("非连续竞价")
        if now.hour == 9 and now.minute < 45:
            reasons.append("开盘前15分钟仅观察")
        if quality != "fresh":
            reasons.append("报价非有效盘中报价")
        if not disclosure_fresh:
            reasons.append("公告检查过期或失败")
        if new_ids:
            reasons.append("有新增未核验公告")
        if stock["disclosure_status"] in {"pending", "blocked"}:
            reasons.append("公告风险暂停买入观察")
        if plan.get("news_enabled"):
            reasons.extend((stock_news or {}).get("blocked", ["等待新闻采集与关联检查"]))
        valid = enabled and plan["enabled"] and valid_day and clock["continuous"] and quality == "fresh"
        confirmations, market_ready, reference_broken = 0, False, False
        if valid:
            price = quote["price"]
            opening = quote.get("open") or 0
            vwap = quote.get("vwap") or 0
            low, high = quote.get("low"), quote.get("high")
            if not opening or not vwap or not low or not high or not low * .995 <= vwap <= high * 1.005:
                reasons.append("均价/成交量口径无法核验")
            if not 0 < quote["change_pct"] < 7:
                reasons.append("当前涨幅不在0%至7%之间")
            if (quote.get("amount_yuan") or 0) < 100_000_000:
                reasons.append("当日成交额不足1亿元")
            if quote.get("turnover_pct") is None or quote["turnover_pct"] > 20:
                reasons.append("换手率缺失或超过20%")
            if opening and (opening / stock["reference_close"] - 1) * 100 >= plan["max_open_gap_pct"]:
                reasons.append("高开达到保守观察上限，不自动确认")
            if "ST" in quote["name"].upper() or "退" in quote["name"]:
                reasons.append("风险警示/退市股票")
            if high == low:
                reasons.append("一字价格，缺少回落承接证据")
            if any(quote.get(key) and abs(price - quote[key]) <= max(.02, quote[key] * .002)
                   for key in ("limit_up", "limit_down")):
                reasons.append("接近涨跌停，不能确认可成交机会")
            if high and price / high <= .97:
                reasons.append("日内高点回落达到3%")
            if price < stock["reference_vwap"] or price < vwap or price <= opening:
                reasons.append("价格未同时站上上日参考均价/当日均价/开盘价")
            # Only distinct real source ticks, same session, contiguous gaps <=90s.
            recent = []
            for point in history.get(symbol, []):
                stamp = datetime.fromisoformat(point["as_of"])
                if stamp.date() != now.date() or not market_clock(stamp)["continuous"]:
                    continue
                if not 0 <= (now - stamp).total_seconds() <= 600:
                    continue
                if recent and (stamp - datetime.fromisoformat(recent[-1]["as_of"])).total_seconds() > 90:
                    recent = []
                if not recent or point["as_of"] > recent[-1]["as_of"]:
                    recent.append(point)
            tail = recent[-3:]
            if len(tail) == 3 and (datetime.fromisoformat(tail[-1]["as_of"]) - datetime.fromisoformat(tail[0]["as_of"])).total_seconds() >= 60:
                reference_broken = all(p["price"] < stock["reference_vwap"] for p in tail)
                if all(p.get("vwap") and p["price"] >= p["vwap"] for p in tail) and all(tail[i]["price"] <= tail[i+1]["price"] for i in range(2)):
                    confirmations = 3
            pullback = False
            if len(recent) >= 5:
                prior = recent[:-2]
                trough_index = min(range(len(prior)), key=lambda i: prior[i]["price"])
                trough = prior[trough_index]["price"]
                peak = max((p["price"] for p in prior[:trough_index]), default=trough)
                pullback = peak > trough and trough / peak <= .998 and price / trough >= 1.002 and trough >= stock["reference_vwap"]
            if confirmations < 3 or not pullback:
                reasons.append("等待真实回踩及至少60秒的三次报价确认")
            market_ready = not reasons
            if reference_broken:
                conditions.append({"key": "focus_break_" + symbol, "severity": "warning",
                                   "message": f"{stock['name']} {symbol} 连续三次报价失守上日参考均价 {stock['reference_vwap']:.2f}，原强势观察预案暂停；非卖出委托"})
        entry_ready = market_ready and review_ok and risk["selection_allowed"]
        if market_ready:
            level = "info" if not entry_ready else "warning"
            message = ("满足买入观察条件，仍须本人核对并手动决策" if entry_ready else
                       "量价观察条件满足，但账户风控未通过；不能作为买入确认")
            conditions.append({"key": "focus_ready_" + symbol, "severity": level,
                               "message": f"{stock['name']} {symbol}：{message}。报价 {quote['price']:.2f}，时间 {quote['as_of']}；新买A股T+1，当日不能卖出"})
        if valid_day or disclosure_monitoring:
            for item in announcements:
                if item["id"] not in new_ids:
                    continue
                conditions.append({"key": "focus_disclosure_" + symbol + "_" + item["id"], "severity": "warning",
                                   "message": f"{stock['name']} {symbol} 新增未核验公告 {item['id']}：{item.get('title', '')[:180]}，暂停买入观察提醒，请先核验原文"})
        if (valid_day or disclosure_monitoring) and stock["disclosure_status"] == "blocked":
            conditions.append({"key": "focus_review_block_" + symbol, "severity": "warning",
                               "message": f"{stock['name']} {symbol} 公告风险暂停买入观察：{stock['disclosure_summary'][:350]}"})
        if (valid_day or disclosure_monitoring) and not disclosure_fresh:
            conditions.append({"key": "focus_disclosure_feed", "severity": "warning", "message": "重点池公告检查不可用或过期，暂停买入观察提醒"})
        rows.append({**stock, "quote": quote, "quality": quality, "market_ready": market_ready,
                     "entry_ready": entry_ready, "confirmations": confirmations,
                     "new_disclosure_ids": new_ids, "announcements": announcements,
                     "reasons": list(dict.fromkeys(reasons)),
                     "news": stock_news,
                     "observation_conditions": [
                         "仅目标交易日9:45后，报价90秒内有效",
                         f"不低于上日参考均价{stock['reference_vwap']:.2f}和当日均价，且高于开盘价；真实回踩后至少60秒三次报价确认",
                         "当日涨幅0%至7%、成交额至少1亿元、换手率不超过20%；高开达到3%不自动确认",
                         f"公告与新闻检查有效且无未解决限制；实际账户完整，未触及{risk.get('restrict_pct', 4):g}%/{risk.get('max_drawdown_pct', 5):g}%限制"],
                     "invalidation_conditions": [
                         f"连续三次报价跌破上日参考均价{stock['reference_vwap']:.2f}",
                         "低于当日均价或不高于开盘价、较日内高点回落至少3%、接近涨跌停",
                         "出现未核验公告、直接负面/不确定新闻，或行情及资讯采集失效",
                         f"账户回撤{risk.get('warning_pct', 3):g}%预警、{risk.get('restrict_pct', 4):g}%暂停新增判断、{risk.get('max_drawdown_pct', 5):g}%保持风险状态；T+1可能限制卖出"],
                     "status": "买入观察条件满足" if entry_ready else "量价满足/风控待核" if market_ready else "待确认"})
    return {"configured": True, "enabled": plan["enabled"] and enabled, "session_date": plan["session_date"],
            "reference_date": plan["reference_date"], "review_since": plan["review_since"],
            "reviewed_at": plan["reviewed_at"], "disclosures_checked_at": checked,
            "disclosures_fresh": disclosure_fresh, "disclosure_error": disclosures.get("error"),
            "max_open_gap_pct": plan["max_open_gap_pct"], "rows": rows,
            "conditions": list({c["key"]: c for c in conditions}.values()),
            "rule_note": "仅目标日；9:45后；报价<=90秒；成交额>=1亿元；涨幅0~7%；回踩后当日均价上方至少60秒三次报价确认。规则未经收益回测。"}
