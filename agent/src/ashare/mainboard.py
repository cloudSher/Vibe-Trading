"""Complete, bounded SH/SZ main-board discovery. No account or order access."""
from __future__ import annotations

import argparse
import json
import math
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import requests

from .calendar import SHANGHAI, market_clock, now_shanghai, quote_quality
from .feeds import _get, number

SOURCE = "https://quote.eastmoney.com/center/gridlist.html#hs_a_board"
FIELDS = "f2,f3,f5,f6,f8,f10,f12,f13,f14,f15,f16,f17,f18,f124"
MAINBOARD = re.compile(r"(?:60[0135]\d{3}\.SH|00[0123]\d{3}\.SZ)\Z")


def parse_row(raw: dict, received_at: datetime) -> dict:
    code = str(raw.get("f12", ""))
    market = str(raw.get("f13", ""))
    symbol = code + ({"0": ".SZ", "1": ".SH"}.get(market, ".UNKNOWN"))
    volume, amount = number(raw.get("f5")), number(raw.get("f6"))
    low, high = number(raw.get("f16")), number(raw.get("f15"))
    shares = volume * 100 if volume and volume > 0 else None
    vwap = amount / shares if amount and shares else None
    if vwap is not None and (not low or not high or not low * .995 <= vwap <= high * 1.005):
        vwap = None
    try:
        stamp = datetime.fromtimestamp(float(raw["f124"]), SHANGHAI).isoformat()
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        stamp = None
    return {"symbol": symbol, "name": str(raw.get("f14", "")), "kind": "stock",
            "price": number(raw.get("f2")), "previous_close": number(raw.get("f18")),
            "change_pct": number(raw.get("f3")), "open": number(raw.get("f17")),
            "high": high, "low": low, "volume_shares": shares, "amount_yuan": amount,
            "turnover_pct": number(raw.get("f8")), "volume_ratio": number(raw.get("f10")),
            "vwap": vwap, "as_of": stamp, "received_at": received_at.isoformat(),
            "source": "eastmoney_public_mainboard", "currency": "CNY", "latency_guaranteed": False}


def exclusions(q: dict, now: datetime) -> list[str]:
    reasons = []
    if not MAINBOARD.fullmatch(q.get("symbol", "")):
        reasons.append("非沪深主板")
    if re.search(r"ST|退|^[NC]", q.get("name", "").upper()):
        reasons.append("ST/退市/上市初期标记")
    quality = quote_quality(q, market_clock(now), now)
    if quality != "fresh" or not market_clock(now)["continuous"]:
        reasons.append("非连续竞价或报价时效未通过")
    required = ("price", "previous_close", "open", "high", "low", "vwap", "amount_yuan", "turnover_pct", "change_pct")
    if any(number(q.get(k)) is None for k in required) or any((q.get(k) or 0) <= 0 for k in required[:7]):
        return reasons + ["量价数据不完整或均价单位异常"]
    price, vwap = q["price"], q["vwap"]
    if not 0 < q["change_pct"] < 7:
        reasons.append("涨幅不在0%至7%之间")
    if q["amount_yuan"] < 100_000_000:
        reasons.append("成交额不足1亿元")
    if not 0 < q["turnover_pct"] <= 20:
        reasons.append("换手率缺失或超过20%")
    if price <= q["open"] or price < vwap:
        reasons.append("未站上开盘价及日内均价")
    if price / vwap - 1 > .02:
        reasons.append("偏离日内均价超过2%，暂不追价")
    if q["open"] / q["previous_close"] - 1 >= .03:
        reasons.append("高开达到3%")
    if 1 - price / q["high"] >= .03 or q["high"] == q["low"]:
        reasons.append("日内高点回落达到3%或一字行情")
    return reasons


def scan_mainboard(*, clock=now_shanghai, progress=None) -> dict:
    """Stable code ordering, 100/page, <=60 pages, total and uniqueness checked."""
    from backtest.loaders._http import DEFAULT_USER_AGENT
    started = clock()
    deadline = time.monotonic() + 120
    collected, total = {}, None
    pages = 0
    used_hosts = set()
    for page in range(1, 61):
        if time.monotonic() > deadline:
            raise ValueError("扫描超过120秒时限，不能发布为当前完整结果")
        params = {
            "pn": page, "pz": 100, "po": 0, "np": 1, "fltt": 2, "invt": 2,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281", "fid": "f12",
            "fs": "m:0 t:6,m:1 t:2", "fields": FIELDS}
        # Public quote servers occasionally close a connection without a body.
        # Retry only this idempotent page, at most three attempts in total.
        hosts = ("push2.eastmoney.com", "82.push2.eastmoney.com", "push2.eastmoney.com")
        for attempt, host in enumerate(hosts):
            try:
                response = _get(f"https://{host}/api/qt/clist/get", params=params,
                    headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://quote.eastmoney.com/"})
                used_hosts.add(host)
                break
            except requests.RequestException:
                if attempt == len(hosts) - 1 or time.monotonic() > deadline:
                    raise ValueError(f"第{page}页网络读取失败（已有限重试）；本轮不完整") from None
        payload = response.json()
        data = payload.get("data") or {}
        count = data.get("total")
        if payload.get("rc") != 0 or not isinstance(count, int) or not 1 <= count <= 6000:
            raise ValueError("主板接口结构或股票总数异常")
        if total is not None and total != count:
            raise ValueError("分页期间股票总数变化，不能标为完整扫描")
        total = count
        raw_rows = data.get("diff")
        if isinstance(raw_rows, dict):
            raw_rows = list(raw_rows.values())
        expected = min(100, total - (page - 1) * 100)
        if not isinstance(raw_rows, list) or len(raw_rows) != expected:
            raise ValueError("主板分页缺失，不能用局部涨幅榜替代全市场")
        for raw in raw_rows:
            q = parse_row(raw, clock())
            if q["symbol"] in collected:
                raise ValueError("主板分页出现重复代码，扫描不完整")
            collected[q["symbol"]] = q
        pages = page
        if progress:
            progress({"received": len(collected), "total": total, "pages": page})
        if len(collected) == total:
            break
    if len(collected) != total:
        raise ValueError("主板扫描未覆盖全部记录")
    now = clock()
    rejected = Counter()
    candidates, fresh = [], 0
    for q in collected.values():
        if quote_quality(q, market_clock(now), now) == "fresh":
            fresh += 1
        reasons = exclusions(q, now)
        if reasons:
            rejected.update(reasons)
        else:
            candidates.append(q)
    candidates.sort(key=lambda q: (-q["amount_yuan"], q["symbol"]))
    return {"session_date": started.date().isoformat(), "started_at": started.isoformat(),
            "completed_at": now.isoformat(), "coverage_complete": True, "universe_total": total,
            "received_total": len(collected), "pages": pages, "request_hosts": sorted(used_hosts), "fresh_count": fresh,
            "qualifying_total": len(candidates), "excluded_counts": dict(rejected),
            "rows": candidates[:20], "source_url": SOURCE,
            "method": "沪深主板完整分页；量价过滤后按成交额降序，最多保留20只；不是胜率或收益排名",
            "raw_rows": list(collected.values())}


def cache_universe(scan: dict, path: Path) -> None:
    """Save only public symbols/names from a verified complete source response."""
    rows = scan.get("raw_rows", [])
    if not scan.get("coverage_complete") or len(rows) != scan.get("universe_total"):
        return
    data = {"origin": "eastmoney_public_mainboard", "as_of": scan["completed_at"],
            "total": len(rows), "stocks": [{"symbol": q["symbol"], "name": q["name"]} for q in rows]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False))
    temp.chmod(0o600)
    temp.replace(path)


def scan_cached_universe(path: Path, *, feeds, clock=now_shanghai, progress=None) -> dict:
    """Fresh Tencent quotes over today's complete EM universe, never stale prices."""
    started, deadline = clock(), time.monotonic() + 120
    cached = json.loads(path.read_text())
    as_of = datetime.fromisoformat(cached["as_of"])
    stocks = cached["stocks"]
    symbols = [s["symbol"] for s in stocks]
    if (cached.get("origin") != "eastmoney_public_mainboard" or as_of.date() != started.date()
        or not 0 <= (started - as_of).total_seconds() <= 21600 or not 1 <= len(stocks) <= 6000
        or len(symbols) != cached["total"] or len(set(symbols)) != len(symbols)
        or not all(MAINBOARD.fullmatch(s) for s in symbols)):
        raise ValueError("备用名录不是同日完整沪深主板名录")
    quotes = {}
    for offset in range(0, len(symbols), 60):
        if time.monotonic() > deadline:
            raise ValueError("腾讯主板逐批取价超过120秒时限")
        quotes.update(feeds.quotes(symbols[offset:offset + 60], clock()))
        if progress:
            progress({"received": min(offset + 60, len(symbols)), "total": len(symbols), "pages": offset // 60 + 1})
    if len(quotes) < len(symbols) * .9 or set(quotes) - set(symbols):
        raise ValueError("腾讯返回报价覆盖不足或包含非请求代码")
    now, rejected, candidates, fresh = clock(), Counter(), [], 0
    for symbol in symbols:
        q = quotes.get(symbol)
        if q is None:
            rejected["未返回有效报价"] += 1
            continue
        if quote_quality(q, market_clock(now), now) == "fresh":
            fresh += 1
        reasons = exclusions(q, now)
        if reasons:
            rejected.update(reasons)
        else:
            candidates.append(q)
    candidates.sort(key=lambda q: (-q["amount_yuan"], q["symbol"]))
    return {"session_date": started.date().isoformat(), "started_at": started.isoformat(),
            "completed_at": now.isoformat(), "coverage_complete": True, "universe_total": len(symbols),
            "received_total": len(symbols), "quote_received_total": len(quotes),
            "missing_quote_total": len(symbols) - len(quotes), "pages": math.ceil(len(symbols)/60),
            "universe_as_of": as_of.isoformat(), "source": "tencent_quotes_cached_mainboard_universe",
            "fresh_count": fresh, "qualifying_total": len(candidates), "excluded_counts": dict(rejected),
            "rows": candidates[:20], "source_url": SOURCE,
            "method": "同日东方财富完整主板名录 + 腾讯逐批取价；缺失/过期报价剔除；成交额排序，不是胜率"}


def mainboard_view(state: dict, quotes: dict, now: datetime, *, enabled: bool) -> dict:
    scan = state.get("scan") or {}
    same_day = scan.get("session_date") == now.date().isoformat()
    age = (now - datetime.fromisoformat(scan["completed_at"])).total_seconds() if scan.get("completed_at") else math.inf
    valid = bool(enabled and same_day and 0 <= age <= 660 and scan.get("coverage_complete") and not state.get("error"))
    rows = []
    for original in scan.get("rows", []):
        # Once a shortlist is created, use the independent 30s quote cycle.
        # A missing Tencent record must not silently reuse an old scan quote.
        quote = quotes.get(original["symbol"])
        reasons = exclusions(quote, now) if quote else ["等待独立盘中报价"]
        if not valid:
            reasons.insert(0, "扫描未完成、过期、失败或监控暂停")
        rows.append({"symbol": original["symbol"], "name": original["name"],
                     "quote": quote, "scan_quote": original, "quality": quote_quality(quote, market_clock(now), now),
                     "market_ready": not reasons, "entry_ready": False,
                     "status": "量价通过，公告/新闻待核验" if not reasons else "观察条件暂未满足",
                     "reasons": reasons, "disclosure_status": "pending", "news_status": "pending"})
        if original.get("review"):
            rows[-1]["review"] = original["review"]
    return {k: v for k, v in state.items() if k != "scan"} | {
        "scan": {k: v for k, v in scan.items() if k not in {"rows", "raw_rows"}},
        "valid": valid, "rows": rows,
        "note": "独立主板发现池，不改动原五只重点监控和公告限制；未核验公告/新闻，不能作为买入确认。"}


def review_candidates(scan: dict, *, feeds, news_feeds, clock=now_shanghai) -> None:
    """Public indexes for only the first five; never marks source text reviewed."""
    stocks = [{"symbol": q["symbol"], "name": q["name"]} for q in scan["rows"][:5]]
    if not stocks:
        return
    now = clock()
    try:
        disclosures = feeds.disclosures([s["symbol"] for s in stocks], (now.date() - timedelta(days=30)).isoformat(), now)
        announcement_error = disclosures.get("error")
    except Exception as exc:
        disclosures = {"items": []}
        announcement_error = "公告索引检索失败：" + type(exc).__name__
    risk_terms = re.compile(r"立案|调查|处罚|退市|减持|质押|亏损|下修|终止|违约|担保|诉讼|仲裁|解禁|解除限售|上市流通|向特定对象发行|异常波动|风险提示")
    for stock, row in zip(stocks, scan["rows"]):
        announcements = sorted([a for a in disclosures["items"] if stock["symbol"] in a["symbols"]], key=lambda a: (a["date"], a["id"]), reverse=True)
        error, news = announcement_error, []
        try:
            items = news_feeds.eastmoney(stock, stocks, clock())
            news = sorted([i for i in items if datetime.fromisoformat(i["published_at"]) >= now - timedelta(hours=72)
                           and any(r["symbol"] == stock["symbol"] and r["scope"] == "direct" for r in i["relations"])],
                          key=lambda i: i["published_at"], reverse=True)[:4]
        except Exception as exc:
            error = "；".join(filter(None, [error, "个股新闻检索失败：" + type(exc).__name__]))
        flags = ["公告标题风险线索：" + a["title"] for a in announcements if risk_terms.search(a["title"])]
        flags += ["新闻标题风险线索：" + n["title"] for n in news if risk_terms.search(n["title"])]
        row["review"] = {"checked_at": clock().isoformat(), "announcements": announcements[:30],
                         "announcement_count": len(announcements), "news": [{k: n[k] for k in ("title", "published_at", "source", "url")} for n in news],
                         "title_flags": flags[:10], "error": error, "status": "pending_original_review"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = scan_mainboard(progress=lambda p: print(json.dumps(p), flush=True))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k not in {"raw_rows", "rows"}}, ensure_ascii=False))
    print(json.dumps(result["rows"][:5], ensure_ascii=False))
