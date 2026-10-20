"""Main-board news discovery. Public names resolve symbols; models never invent them."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import re

from .calendar import market_clock, quote_quality
from .mainboard import MAINBOARD, exclusions
from .news import RISK_WORDS, version
from .news_feeds import MARKET_REPORT, company_context

CATALYST = re.compile("中标|订单|签订|签署|预增|增长|扭亏|回购|增持|分红|获批|获准|突破|投产|收购|重组|战略合作|涨价|提价")
EXCLUDED_NAME = re.compile(r"ST|退|^[NC]", re.I)
MAX_CANDIDATES = 40


def validated_directory(data, now):
    """Reject partial, stale, foreign or duplicated public directories."""
    stamp = datetime.fromisoformat(data["as_of"])
    stocks = data["stocks"]
    symbols = [s["symbol"] for s in stocks]
    if (data.get("origin") != "eastmoney_public_mainboard" or stamp.date() != now.date()
        or not 0 <= (now - stamp).total_seconds() <= 86400
        or not 1 <= len(stocks) <= 6000 or len(stocks) != data["total"]
        or len(set(symbols)) != len(symbols) or not all(MAINBOARD.fullmatch(s) for s in symbols)
        or any(not isinstance(s.get("name"), str) or not 2 <= len(s["name"]) <= 40 for s in stocks)):
        raise ValueError("利好雷达需要同日完整、无重复的沪深主板名录")
    return data


class CompanyResolver:
    def __init__(self, stocks):
        names = Counter(s["name"] for s in stocks)
        self.stocks = {s["symbol"]: s for s in stocks if not EXCLUDED_NAME.search(s["name"])}
        self.names = {s["name"]: s["symbol"] for s in self.stocks.values()
                      if names[s["name"]] == 1 and len(s["name"]) >= 3}
        self.pattern = re.compile("|".join(re.escape(n) for n in sorted(self.names, key=len, reverse=True)) or r"(?!)")
        self.codes = {s[:6]: s for s in self.stocks}

    def attach(self, item):
        text = item["title"] + " " + item["content"]
        symbols = {self.names[m.group()] for m in self.pattern.finditer(text)}
        # A bare six-digit amount/date is not a security identifier.
        for match in re.finditer(r"(?:证券代码[：:\s]*|股票代码[：:\s]*|[（(]|(?<![A-Za-z0-9])(?:SH|SZ))([06]\d{5})(?=\D|$)", text, re.I):
            if match[1] in self.codes:
                symbols.add(self.codes[match[1]])
        for match in re.finditer(r"(?<!\d)([06]\d{5})\.(SH|SZ)(?![A-Za-z0-9])", text, re.I):
            symbol = match[1] + "." + match[2].upper()
            if symbol in self.stocks:
                symbols.add(symbol)
        relations = {r["symbol"]: r for r in item["relations"]}
        for symbol in sorted(symbols):
            stock = self.stocks[symbol]
            relations[symbol] = {"symbol": symbol, "scope": "direct", "matched": [stock["name"] if stock["name"] in text else symbol],
                                 "context": company_context(text, stock)}
        return item | {"relations": list(relations.values())}


def discovery_rows(state, news, now):
    directory = state.get("directory", {})
    try:
        validated_directory(directory, now)
    except (ValueError, KeyError, TypeError):
        return []
    stocks = {s["symbol"]: s for s in directory["stocks"] if not EXCLUDED_NAME.search(s["name"])}
    grouped = {}
    for item in news.get("items", []):
        age = (now - datetime.fromisoformat(item["published_at"])).total_seconds()
        if not 0 <= age <= 72 * 3600 or MARKET_REPORT.search(item["title"]):
            continue
        analysis = news.get("analyses", {}).get(item["id"])
        if analysis and analysis.get("version") != version(item):
            analysis = None
        for relation in item["relations"]:
            symbol = relation["symbol"]
            if relation["scope"] != "direct" or symbol not in stocks:
                continue
            impact = analysis.get("symbol_impacts", {}).get(symbol, "uncertain") if analysis else "pending"
            event = {k: item[k] for k in ("id", "title", "source", "url", "published_at", "first_seen_at")}
            event.update(relation=relation, snippet=item["content"][:350], version=version(item),
                source_urls=item.get("source_urls", [item["url"]]),
                analysis=analysis | {"impact": impact} if analysis else None,
                risk_flag=bool(RISK_WORDS.search(company_context(item["title"] + " " + item["content"], stocks[symbol]))),
                reference_only=age > 1800, fresh_catalyst=age <= 1800 and impact == "positive", market_report=False)
            grouped.setdefault(symbol, []).append(event)
    selected = []
    for symbol, events in grouped.items():
        events.sort(key=lambda e: e["published_at"], reverse=True)
        # Keep a newly discovered company visible when a later report reverses it.
        positive = any((e["analysis"] or {}).get("impact") == "positive" and e["published_at"][:10] == now.date().isoformat() for e in events)
        pending = any(not e["analysis"] and e["published_at"][:10] == now.date().isoformat() and CATALYST.search(e["title"] + " " + e["snippet"]) for e in events)
        tracked = state.get("tracked", {}).get(symbol, "")[:10] == now.date().isoformat()
        if positive or pending or tracked:
            selected.append({**stocks[symbol], "events": events[:8], "positive": positive, "pending": pending})
    selected.sort(key=lambda r: (not r["positive"], -datetime.fromisoformat(r["events"][0]["published_at"]).timestamp(), r["symbol"]))
    return selected[:MAX_CANDIDATES]


def market_news_view(state, news, quotes, risk, settings, plan, now):
    enabled = bool(settings.get("market_news_enabled"))
    clock = market_clock(now)
    directory = state.get("directory", {})
    try:
        validated_directory(directory, now)
        directory_valid = True
    except (ValueError, KeyError, TypeError):
        directory_valid = False
    source = news.get("sources", {}).get("cls", {})
    age = (now - datetime.fromisoformat(source["checked_at"])).total_seconds() if source.get("checked_at") else None
    source_fresh = age is not None and 0 <= age <= 180 and not source.get("error")
    rows, conditions = [], []
    active = enabled and settings.get("enabled") and settings.get("recurring_news_enabled") and clock["calendar_verified"] and clock["is_session"] and 540 <= now.hour * 60 + now.minute <= 1260
    focus_stocks = {s["symbol"]: s for s in (plan or {}).get("stocks", [])}
    for discovered in discovery_rows(state, news, now) if enabled else []:
        symbol = discovered["symbol"]
        review = state.get("reviews", {}).get(symbol, {})
        quote = quotes.get(symbol) or review.get("quote")
        if quote and (quote.get("symbol") != symbol or EXCLUDED_NAME.search(quote.get("name", ""))):
            quote = None
        quality = quote_quality(quote, clock, now)
        price_flags = exclusions(quote, now) if quote else ["尚未取得该股报价"]
        events = discovered["events"]
        risks = [e for e in events if e["risk_flag"] or (e["analysis"] or {}).get("impact") in {"negative", "uncertain"}]
        fresh_positive = any(e["fresh_catalyst"] for e in events)
        reasons = list(price_flags)
        if not fresh_positive:
            reasons.append("没有近30分钟内已整理的直接利好；旧闻或待整理信息只作背景")
        if risks:
            reasons.append("直接相关新闻存在风险或方向未确认，不能用利好覆盖")
        if not source_fresh:
            reasons.append("快讯来源过期或失败")
        if not risk.get("configured") or not risk.get("complete") or not risk.get("selection_allowed"):
            reasons.append("本地账户资料或当前回撤规则未通过，暂停新增仓位判断")
        checked = review.get("checked_at")
        review_age = (now - datetime.fromisoformat(checked)).total_seconds() if checked else None
        review_fresh = review_age is not None and 0 <= review_age <= 660 and not review.get("error")
        if not review_fresh:
            reasons.append("个股新闻与公告索引尚未完成或已过期")
        if review.get("title_flags"):
            reasons.append("公告标题存在风险词，须阅读原文排雷")
        original = focus_stocks.get(symbol, {})
        if original and original.get("disclosure_status") != "reviewed":
            reasons.append("原重点池公告限制继续有效：" + original.get("disclosure_summary", "待核验"))
        market_ready = bool(active and not price_flags and fresh_positive and not risks and source_fresh)
        status = "量价观察条件满足；公告原文仍待核验" if market_ready else "利好背景观察；条件未通过" if discovered["positive"] else "疑似催化待整理"
        if risks:
            status = "方向变化或风险待核验，暂停观察确认"
        row = {"symbol": symbol, "name": discovered["name"], "quality": quality, "quote": quote,
               "market_ready": market_ready, "entry_ready": False, "status": status, "reasons": reasons,
               "news": {"events": events}, "review": {k: review.get(k) for k in ("checked_at", "news_checked_at", "announcement_since", "error", "title_flags")},
               "announcements": sorted(review.get("announcements", []), key=lambda a: (not bool(RISK_WORDS.search(a["title"])), a["id"] not in review.get("new_disclosure_ids", [])))[:20], "trading_plan_current": False,
               "observation_conditions": ["直接利好原文及公告风险经人工核验；模型摘要只作整理", "连续竞价且报价不超过90秒；涨幅0%至7%、成交额至少1亿元、换手率0%至20%", "站上开盘价与日内均价，偏离均价不超过2%；高开及高点回落均小于3%，避开一字行情", "补齐持仓并通过本地账户风控；原重点股公告限制仍有效"],
               "invalidation_conditions": ["消息更正、否认、风险公告或方向转负/待核验", "新闻超过30分钟仅作背景；报价过期或量价条件不再满足", "回撤预警/暂停新增/最大阈值以本地配置为准；新买A股T+1"]}
        rows.append(row)
        if not active:
            continue
        for announcement in row["announcements"]:
            if symbol not in focus_stocks and (RISK_WORDS.search(announcement["title"]) or announcement["id"] in review.get("new_disclosure_ids", [])):
                conditions.append({"key": f"radar_disclosure_{symbol}_{announcement['id']}", "severity": "warning",
                    "message": f"{discovered['name']} {symbol} 公告索引新增或含风险词：{announcement['title'][:150]}；原文尚未核验，暂停买入确认",
                    "announcement": announcement})
        for event in events:
            phase = (event.get("analysis") or {}).get("impact", "pending")
            if event["published_at"][:10] != now.date().isoformat() or phase == "pending":
                continue
            # Original five retain their existing alert/restriction pipeline.
            if symbol in focus_stocks:
                continue
            if phase not in {"positive", "negative", "uncertain"} and not (phase == "neutral" and state.get("tracked", {}).get(symbol, "")[:10] == now.date().isoformat()):
                continue
            conditions.append({"key": f"radar_news_{symbol}_{event['id']}_{event['version']}_{phase}",
                "severity": "warning" if event["risk_flag"] or phase != "positive" else "info", "news_event": event,
                "message": f"主板利好雷达 {discovered['name']} {symbol}：{event['title'][:110]}；模型{phase}，{status}；非买入确认"})
        if market_ready:
            conditions.append({"key": "radar_price_" + symbol, "severity": "info", "message": f"{discovered['name']} {symbol} 利好与量价观察条件满足；公告原文及账户限制仍需核对，非买入确认"})
    return {"enabled": enabled, "scope": "sh_sz_mainboard", "active": bool(active), "directory_valid": directory_valid,
            "directory_as_of": directory.get("as_of"), "universe_total": directory.get("total", 0),
            "directory_error": state.get("directory_error"), "source_fresh": source_fresh,
            "checked_at": source.get("checked_at"), "analysis_at": news.get("analysis_at"), "analysis_error": news.get("analysis_error"),
            "analysis_running": news.get("analysis_running", False),
            "pending_count": sum(any(not e.get("analysis") for e in row["news"]["events"]) for row in rows),
            "rows": rows, "conditions": conditions, "candidate_limit": MAX_CANDIDATES,
            "note": "财联社公开快讯发现沪深主板公司，排除ST/退市及N/C新股；非全市场全量资讯覆盖。公司名或明确证券代码匹配，不把行业新闻推断成公司订单。最多展示40只，逐批补查个股新闻、近30天公告索引和报价；公告原文始终待人工核验。"}
