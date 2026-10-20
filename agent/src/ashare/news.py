"""Deterministic relevance, freshness and news restrictions, independent of orders."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from .news_feeds import MARKET_REPORT, SECTORS, company_context
from .schedule import news_monitoring
from .calendar import SHANGHAI

RISK_WORDS = re.compile("立案|处罚|退市|减持|质押|亏损|下修|终止|违约|担保|辟谣|否认|未签|未获|传闻")


def version(item):
    # Preserve existing five-stock analyses when only discovery scope changes.
    # Expanded associations require a new per-company model result.
    extra = [r for r in item["relations"] if r["symbol"] not in SECTORS]
    relations = "|" + "|".join(sorted(r["symbol"] + ":" + r["scope"] for r in item["relations"])) if extra else ""
    prefix = "per-symbol-v3|" if extra else "per-symbol-v2|"
    return hashlib.sha256((prefix + item["title"] + "|" + item["content"] + relations).encode()).hexdigest()[:16]


def merge_items(previous, incoming, now):
    saved = {i["id"]: dict(i) for i in previous}
    for item in incoming:
        prior = saved.get(item["id"])
        if prior:
            # Keep the first provider's text for duplicate syndication; retain links.
            links = list(dict.fromkeys([*prior.get("source_urls", [prior["url"]]), item["url"]]))[:8]
            relations = {r["symbol"]: r for r in prior["relations"]}
            for relation in item["relations"]:
                if relation["symbol"] not in relations or relation["scope"] == "direct":
                    relations[relation["symbol"]] = relation
            if item["source"] == prior["source"] and item["published_at"] >= prior["published_at"]:
                saved[item["id"]] = item | {"first_seen_at": prior["first_seen_at"], "source_urls": links, "relations": list(relations.values())}
            else:
                prior["source_urls"] = links
                prior["relations"] = list(relations.values())
        else:
            saved[item["id"]] = item | {"source_urls": [item["url"]]}
    # Source time, not retrieval time, controls relevance retention.
    kept = [i for i in saved.values() if 0 <= (now - datetime.fromisoformat(i["published_at"])).total_seconds() <= 7 * 86400]
    return sorted(kept, key=lambda i: i["published_at"], reverse=True)[:500]


def pending_items(state, now):
    analyses = state.get("analyses", {})
    return [i for i in state.get("items", []) if i["relations"] and not MARKET_REPORT.search(i["title"])
            and 0 <= (now - datetime.fromisoformat(i["published_at"])).total_seconds() <= 72 * 3600
            and analyses.get(i["id"], {}).get("version") != version(i)]


def news_view(state, plan, now, *, settings=None):
    now = now.astimezone(SHANGHAI)
    if not plan or not plan.get("news_enabled"):
        return {"enabled": False, "sources": {}, "stocks": {}, "conditions": []}
    sources, conditions, stocks = {}, [], {}
    monitoring = news_monitoring(plan, settings or {}, now)
    active = monitoring["active"] if monitoring["recurring"] else now.date().isoformat() == plan["session_date"] and plan["enabled"]
    requested_window = monitoring["active"]
    for key in ["cls", *["eastmoney_" + s["symbol"] for s in plan["stocks"]]]:
        raw = state.get("sources", {}).get(key, {})
        age = (now - datetime.fromisoformat(raw["checked_at"])).total_seconds() if raw.get("checked_at") else None
        maximum_age = (180 if key == "cls" else 660) if requested_window else (1260 if key == "cls" else 3660)
        fresh = age is not None and 0 <= age <= maximum_age and not raw.get("error")
        sources[key] = {**raw, "fresh": fresh}
    for stock in plan["stocks"]:
        symbol, events, blocked = stock["symbol"], [], []
        healthy = sources["cls"]["fresh"] and sources["eastmoney_" + symbol]["fresh"]
        if not healthy:
            blocked.append("新闻采集失败、过期或尚未完成，暂停联合观察确认")
        for item in state.get("items", []):
            item = {**item, "market_report": bool(MARKET_REPORT.search(item["title"]))}
            relation = next((r for r in item["relations"] if r["symbol"] == symbol), None)
            if not relation:
                continue
            age = (now - datetime.fromisoformat(item["published_at"])).total_seconds()
            if not 0 <= age <= 72 * 3600:
                continue
            analysis = state.get("analyses", {}).get(item["id"])
            if analysis and analysis.get("version") != version(item):
                analysis = None
            if analysis:
                analysis = {**analysis, "impact": analysis.get("symbol_impacts", {}).get(symbol, "uncertain")}
            direct = relation["scope"] == "direct"
            relevant_text = company_context(item["title"] + " " + item["content"], stock)
            risky = bool(direct and RISK_WORDS.search(relevant_text) and (
                not item["market_report"] or company_context(item["title"], stock) and RISK_WORDS.search(item["title"])))
            pending = bool(direct and not item["market_report"] and not analysis)
            uncertain = bool(direct and not item["market_report"] and analysis and analysis["impact"] in {"negative", "uncertain"})
            if risky or pending or uncertain:
                blocked.append("直接相关新闻含风险词或方向待核验" if risky or uncertain else "直接相关新闻等待模型整理")
            public = {k: item[k] for k in ("id", "title", "source", "url", "published_at", "first_seen_at")}
            events.append(public | {"relation": relation, "source_urls": item.get("source_urls", [item["url"]]),
                                   "snippet": item["content"][:350], "analysis": analysis, "risk_flag": risky,
                                   "fresh_catalyst": bool(age <= 1800 and direct and not item["market_report"] and analysis and analysis["impact"] == "positive"),
                                   "reference_only": age > 1800, "market_report": item["market_report"]})
            # Only target-day events. Old baselines are shown, never relabelled as new.
            meaningful = direct and not item["market_report"] or bool(analysis and age <= 1800 and not item["market_report"])
            if active and item["published_at"][:10] == now.date().isoformat() and meaningful:
                phase = analysis["impact"] if analysis else "pending"
                conditions.append({"key": "focus_news_" + symbol + "_" + item["id"] + "_" + version(item) + "_" + phase,
                                   "severity": "warning" if risky or uncertain or pending or analysis and analysis["impact"] == "negative" else "info",
                                   "news_event": events[-1] | {"version": version(item)},
                                   "message": f"{stock['name']} {symbol} {'直接相关' if direct else '仅行业关联'}新闻：{item['title'][:130]}。源时间 {item['published_at']}；{'风险/待核验，暂停观察确认' if risky or uncertain or pending else '需结合量价和账户风控，非买入指令'}"})
        if active and blocked and healthy:
            conditions.append({"key": "focus_news_restrict_" + symbol, "severity": "warning",
                               "message": f"{stock['name']} {symbol} 相关新闻存在待核验或风险事项，暂停联合观察确认，请查看原文"})
        events.sort(key=lambda e: not (e["risk_flag"] or e["relation"]["scope"] == "direct" and
                    not e["market_report"] and (not e["analysis"] or e["analysis"]["impact"] in {"negative", "uncertain"})))
        stocks[symbol] = {"healthy": healthy, "blocked": list(dict.fromkeys(blocked)), "events": events[:8],
                          "has_fresh_catalyst": any(e["fresh_catalyst"] for e in events),
                          "note": "未发现可作为新增直接催化的消息；仍可按量价条件观察" if not any(e["fresh_catalyst"] for e in events) else "存在近期直接相关的正向新闻判断，尚需量价与风控确认"}
    if active and any(not s["fresh"] for s in sources.values()):
        conditions.append({"key": "focus_news_feed", "severity": "warning",
                           "message": "新闻采集不完整或过期，暂停受影响股票的联合买入观察确认；请查看新闻来源状态"})
    return {"enabled": True, "sources": sources, "stocks": stocks, "conditions": conditions, "monitoring": monitoring,
            "analysis_at": state.get("analysis_at"), "analysis_error": state.get("analysis_error"),
            "analysis_running": state.get("analysis_running", False),
            "pending_count": len(pending_items(state, now)), "event_count": len(state.get("items", [])),
            "note": "公开接口轮询，不保证秒级或全量覆盖；东方财富为相关性搜索最多100条后按源时间展示，可能遗漏。新闻时间与抓取时间分开，模型整理不等于事实核验或胜率。"}
