"""Bounded public news readers. Articles are data, never executable content."""
from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

from .calendar import SHANGHAI
from .feeds import _get
from backtest.loaders._http import DEFAULT_USER_AGENT

SECTORS = {
    "000887.SZ": ["空气悬架", "人形机器人", "汽车零部件", "液冷"],
    "600699.SH": ["汽车电子", "智能驾驶", "汽车安全"],
    "001696.SZ": ["通用航空", "航空发动机", "摩托车", "低空经济"],
    "600319.SH": ["氯碱", "烧碱", "氯化聚乙烯"],
    "002531.SZ": ["风电", "海上风电", "风电塔筒"],
}
MARKET_REPORT = re.compile(r"龙虎榜|融资融券|融资余额|融资净买入|杠杆资金|深股通|沪股通|主力资金|营业部最青睐|主力动向|涨停股复盘|只股[短中长]线走稳|净流入资金|连板股分析|^(?:收评|午评|早评|投资日历)[：:]|收盘涨停|涨停[（(]附股")


def company_context(text, stock):
    pattern = re.escape(stock["name"]) + r"|(?<!\d)" + stock["symbol"][:6] + r"(?!\d)"
    matches = list(re.finditer(pattern, text))[:6]
    return " ".join(text[max(0, m.start() - 90):m.end() + 90] for m in matches)


def plain(value, limit=1800):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()[:limit]


def public_url(value):
    parsed = urlsplit(str(value or ""))
    host = parsed.hostname or ""
    if parsed.scheme not in {"https", "http"} or parsed.username or parsed.password:
        return None
    if not (host == "cls.cn" or host.endswith(".cls.cn") or host.endswith(".eastmoney.com")):
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path, "", ""))


def article(source, title, content, published, url, now, stocks):
    title, content = plain(title, 250), plain(content)
    text = title + " " + content
    if not title:
        title = content[:100]
    stamp = datetime.fromisoformat(str(published))
    stamp = stamp.replace(tzinfo=SHANGHAI) if stamp.tzinfo is None else stamp.astimezone(SHANGHAI)
    if stamp > now + timedelta(seconds=5):
        raise ValueError("新闻发布时间在未来")
    if not title or not public_url(url):
        raise ValueError("新闻缺少有效正文或原文地址")
    relations = []
    for stock in stocks:
        symbol, name = stock["symbol"], stock["name"]
        terms = [term for term in SECTORS.get(symbol, []) if term in text]
        direct = name in text or bool(re.search(r"(?<!\d)" + symbol[:6] + r"(?!\d)", text))
        if direct or terms:
            relations.append({"symbol": symbol, "scope": "direct" if direct else "sector",
                              "matched": [name] if name in text else [symbol[:6]] if direct else terms,
                              "context": company_context(text, stock) if direct else ""})
    # Same headline on the same source day collapses cross-provider reposts.
    event_id = hashlib.sha256((stamp.date().isoformat() + "|" + re.sub(r"\W", "", title)).encode()).hexdigest()[:24]
    return {"id": event_id, "title": title, "content": content, "published_at": stamp.isoformat(),
            "first_seen_at": now.isoformat(), "source": source, "url": public_url(url), "relations": relations,
            "market_report": bool(MARKET_REPORT.search(title))}


class NewsFeeds:
    def cls(self, stocks, now, previous_latest=None):
        items, cursor, covered = [], int(now.timestamp()), previous_latest is None
        for _ in range(5):
            params = {"app": "CailianpressWeb", "category": "", "last_time": cursor,
                      "os": "web", "refresh_type": "1", "rn": "20", "sv": "8.4.6"}
            params["sign"] = hashlib.md5(hashlib.sha1(urlencode(params).encode()).hexdigest().encode()).hexdigest()
            raw = _get("https://www.cls.cn/v1/roll/get_roll_list", params=params,
                       headers={"Referer": "https://www.cls.cn/telegraph", "User-Agent": "Mozilla/5.0"}).json()
            if raw.get("errno", 0) != 0 or not isinstance(raw.get("data"), dict) or not isinstance(raw["data"].get("roll_data"), list):
                raise ValueError("财联社未返回有效电报列表")
            rows = raw["data"]["roll_data"]
            if not rows:
                if not items:
                    raise ValueError("财联社返回空列表，无法确认采集状态")
                covered = True
                break
            stamps = []
            for row in rows:
                stamp = datetime.fromtimestamp(int(row["ctime"]), SHANGHAI)
                stamps.append(stamp.timestamp())
                items.append(article("财联社", row.get("title"), row.get("content"), stamp.isoformat(),
                                     "https://www.cls.cn/detail/" + str(row["id"]), now, stocks))
            if previous_latest is None or min(stamps) <= previous_latest:
                covered = True
                break
            next_cursor = int(min(stamps)) - 1
            if next_cursor >= cursor:
                break
            cursor = next_cursor
        if not covered:
            raise ValueError("财联社回补达到上限，可能漏报，暂停新闻确认")
        return items, max((datetime.fromisoformat(i["published_at"]).timestamp() for i in items), default=previous_latest)

    def eastmoney(self, stock, stocks, now):
        params = {"uid": "", "keyword": stock["symbol"][:6], "type": ["cmsArticleWebOld"], "client": "web",
                  "clientType": "web", "param": {"cmsArticleWebOld": {
                      "searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": 100}}}
        response = _get("https://search-api-web.eastmoney.com/search/jsonp",
                        params={"cb": "", "param": json.dumps(params, ensure_ascii=False), "_": str(int(now.timestamp()))},
                        headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://so.eastmoney.com/"})
        body = response.text.strip()
        if not body.startswith("{"):
            body = body[body.find("(") + 1:body.rfind(")")]
        raw = json.loads(body)
        if not isinstance(raw.get("result"), dict) or not isinstance(raw["result"].get("cmsArticleWebOld"), list):
            raise ValueError("东方财富未返回有效新闻结构")
        rows = raw["result"]["cmsArticleWebOld"]
        return [article("东方财富/" + plain(row.get("mediaName"), 60), row.get("title"), row.get("content"),
                        row["date"], row.get("url"), now, stocks) for row in rows]
