"""Bounded public quote/LHB clients. Source timestamps are never retrieval times."""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests

from .calendar import SHANGHAI
from .models import normalize_symbol

_HTTP_LOCK = threading.Lock()
_LAST_REQUEST: dict[str, float] = {}
_HOST_LOCKS: dict[str, threading.Lock] = {}
INDEX_CODES = {"000001.SH": "sh000001", "399001.SZ": "sz399001", "399006.SZ": "sz399006"}
LHB_SOURCE = "https://data.eastmoney.com/stock/tradedetail.html"


def number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _get(url: str, **kwargs):
    hostname = urlsplit(url).hostname or ""
    host = "eastmoney" if hostname.endswith(".eastmoney.com") else hostname
    with _HTTP_LOCK:
        lock = _HOST_LOCKS.setdefault(host, threading.Lock())
    with lock:
        delay = 1 - (time.monotonic() - _LAST_REQUEST.get(host, 0))
        if delay > 0:
            time.sleep(delay)
        _LAST_REQUEST[host] = time.monotonic()
        response = requests.get(url, timeout=(3, 8), **kwargs)
    response.raise_for_status()
    return response


def parse_tencent(text: str, requested: dict[str, str], received_at: datetime) -> dict[str, dict]:
    result = {}
    for match in re.finditer(r'v_([a-z]{2}\d{6})="([^"]*)"', text):
        wire, raw = match.groups()
        symbol = requested.get(wire)
        parts = raw.split("~")
        if symbol is None or len(parts) < 49 or parts[2] != wire[2:]:
            continue
        price, prev = number(parts[3]), number(parts[4])
        if price is None or price <= 0 or prev is None or prev <= 0:
            continue
        try:
            stamp = datetime.strptime(parts[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        except ValueError:
            continue
        volume, amount = number(parts[6]), number(parts[37])
        is_index = symbol in INDEX_CODES
        shares = volume * 100 if volume is not None and not is_index else None
        amount_yuan = amount * 10_000 if amount is not None else None
        low, high = number(parts[34]), number(parts[33])
        vwap = amount_yuan / shares if amount_yuan and shares and shares > 0 else None
        if vwap is not None and low and high and not low * .995 <= vwap <= high * 1.005:
            # Some wire instruments use a different volume unit. Never emit a
            # 100x-wrong average or infer a correction from the stock code.
            shares, vwap = None, None
        result[symbol] = {
            "symbol": symbol, "name": parts[1], "kind": "index" if is_index else "stock",
            "price": price, "previous_close": prev, "open": number(parts[5]),
            "high": high, "low": low,
            "change_pct": (price / prev - 1) * 100,
            "volume_shares": shares, "amount_yuan": amount_yuan,
            "turnover_pct": number(parts[38]) if not is_index else None,
            "vwap": vwap,
            "limit_up": number(parts[47]) if not is_index else None,
            "limit_down": number(parts[48]) if not is_index else None,
            "as_of": stamp.isoformat(), "received_at": received_at.isoformat(),
            "source": "tencent_public_quote", "currency": "CNY", "adjustment": "unadjusted",
            "latency_guaranteed": False,
        }
    return result


def parse_five_minute(payload, symbol, now):
    data = payload.get("data") or {}
    if payload.get("rc") != 0 or data.get("code") != symbol[:6] or data.get("market") != (1 if symbol.endswith(".SH") else 0):
        raise ValueError("5分钟K线代码或来源结构无效")
    rows = data.get("klines")
    if not isinstance(rows, list) or not rows or len(rows) > 120:
        raise ValueError("5分钟K线缺失或超过上限")
    items = []
    for row in rows:
        parts = row.split(",")
        if len(parts) < 7:
            raise ValueError("5分钟K线字段不足")
        stamp = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI)
        opening, close, high, low, lots, amount = [number(v) for v in parts[1:7]]
        minute = stamp.hour * 60 + stamp.minute
        if (any(v is None for v in (opening, close, high, low, lots, amount)) or
                not 0 < low <= min(opening, close) <= max(opening, close) <= high or lots < 0 or amount < 0 or
                (lots > 0 and not low * .98 <= amount / (lots * 100) <= high * 1.02) or
                stamp.minute % 5 or not (575 <= minute <= 690 or 785 <= minute <= 900) or
                items and stamp <= datetime.fromisoformat(items[-1]["as_of"])):
            raise ValueError("5分钟K线价格、数量口径或时间顺序无效")
        items.append({"as_of": stamp.isoformat(), "open": opening, "close": close, "high": high, "low": low,
                      "volume_shares": lots * 100, "amount_yuan": amount})
    return {"checked_at": now.isoformat(), "items": items, "error": None,
            "source": "eastmoney_public_m5", "adjustment": "unadjusted", "bar_time": "end", "latency_guaranteed": False}


def parse_tencent_five_minute(payload, symbol, now):
    wire = symbol[-2:].lower() + symbol[:6]
    data = (payload.get("data") or {}).get(wire) or {}
    rows = data.get("m5")
    if payload.get("code") != 0 or not isinstance(rows, list) or not 0 < len(rows) <= 80:
        raise ValueError("腾讯5分钟K线结构无效")
    items = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError("腾讯5分钟K线字段缺失")
        stamp = datetime.strptime(row[0], "%Y%m%d%H%M").replace(tzinfo=SHANGHAI)
        opening, close, high, low, lots = [number(v) for v in row[1:6]]
        minute = stamp.hour * 60 + stamp.minute
        if (any(v is None for v in (opening, close, high, low, lots)) or
                not 0 < low <= min(opening, close) <= max(opening, close) <= high or lots < 0 or
                stamp.minute % 5 or not (575 <= minute <= 690 or 785 <= minute <= 900) or
                items and stamp <= datetime.fromisoformat(items[-1]["as_of"])):
            raise ValueError("腾讯5分钟K线价格、数量或时间顺序无效")
        items.append({"as_of": stamp.isoformat(), "open": opening, "close": close, "high": high, "low": low,
                      "volume_shares": lots * 100, "amount_yuan": None})
    # This endpoint supplies volume but no bar amount. Never invent bar VWAP;
    # the quote feed's checked amount/volume remains the current-day VWAP gate.
    return {"checked_at": now.isoformat(), "items": items, "error": None,
            "source": "tencent_public_m5", "adjustment": "unadjusted", "bar_time": "end", "latency_guaranteed": False,
            "source_warning": "东方财富5分钟源暂不可用，使用腾讯公开5分钟K线；当日VWAP使用独立报价核验"}


class PublicFeeds:
    def five_minute_bars(self, symbol: str, now: datetime) -> dict:
        if time.monotonic() >= getattr(self, "_m5_em_retry_after", 0):
            try:
                response = _get("https://push2his.eastmoney.com/api/qt/stock/kline/get", params={
                    "secid": ("1." if symbol.endswith(".SH") else "0.") + symbol[:6],
                    "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                    "klt": "5", "fqt": "0", "end": "20500101", "lmt": "120"})
                return parse_five_minute(response.json(), symbol, now)
            except (requests.RequestException, ValueError, KeyError, TypeError):
                self._m5_em_retry_after = time.monotonic() + 300
        wire = symbol[-2:].lower() + symbol[:6]
        response = _get("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline",
                        params={"param": wire + ",m5,,80"})
        return parse_tencent_five_minute(response.json(), symbol, now)

    def disclosures(self, symbols: list[str], start: str, now: datetime) -> dict:
        end = now.date().isoformat()
        items, total = {}, None
        for page in range(1, 6):
            response = _get("https://np-anotice-stock.eastmoney.com/api/security/ann", params={
                "sr": -1, "page_size": 100, "page_index": page, "ann_type": "A",
                "stock_list": ",".join(s[:6] for s in symbols), "begin_time": start, "end_time": end})
            raw = response.json()
            data = raw.get("data") or {}
            if raw.get("success") != 1 or not isinstance(data.get("list"), list):
                raise ValueError("公告接口未返回完整结构")
            total = int(data.get("total_hits", -1))
            if total < 0 or total > 500:
                raise ValueError("公告数量异常或超过完整核验上限")
            for row in data["list"]:
                code = str(row.get("art_code", ""))
                day = str(row.get("notice_date", ""))[:10]
                if not re.fullmatch(r"AN\d{18}", code) or not start <= day <= end:
                    raise ValueError("公告标识或日期无效")
                found = [s for s in symbols if any(c.get("stock_code") == s[:6] for c in row.get("codes", []))]
                if not found:
                    raise ValueError("公告股票代码不匹配")
                items[code] = {"id": code, "date": day, "title": str(row.get("title", ""))[:500],
                               "symbols": found, "source_url": f"https://data.eastmoney.com/notices/detail/{found[0][:6]}/{code}.html"}
            if page * 100 >= total:
                break
        if len(items) != total:
            raise ValueError("公告分页不完整，禁止按全部已核验处理")
        return {"checked_at": now.isoformat(), "since": start, "through": end,
                "total": total, "items": list(items.values()), "error": None}

    def quotes(self, symbols: list[str], now: datetime) -> dict[str, dict]:
        wires = {INDEX_CODES[s] if s in INDEX_CODES else s[-2:].lower() + s[:6]: s for s in symbols}
        result = {}
        items = list(wires.items())
        for offset in range(0, len(items), 60):
            batch = dict(items[offset:offset + 60])
            response = _get("https://qt.gtimg.cn/q=" + ",".join(batch), headers={"Referer": "https://gu.qq.com/"})
            response.encoding = "gbk"
            result.update(parse_tencent(response.text, batch, now))
        return result

    def _billboard(self, start: str, end: str, size: int) -> dict:
        fields = "SECURITY_CODE,SECUCODE,SECURITY_NAME_ABBR,TRADE_DATE,BILLBOARD_NET_AMT,BILLBOARD_DEAL_AMT,ACCUM_AMOUNT,CHANGE_RATE,TURNOVERRATE,EXPLANATION"
        response = _get("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
            "sortColumns": "TRADE_DATE,BILLBOARD_DEAL_AMT", "sortTypes": "-1,-1",
            "pageSize": size, "pageNumber": 1, "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
            "columns": fields, "source": "WEB", "client": "WEB",
            "filter": f"(TRADE_DATE<='{end}')(TRADE_DATE>='{start}')",
        })
        payload = response.json()
        if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
            raise ValueError("龙虎榜数据源未返回有效数据")
        return payload["result"]

    def billboard(self, now: datetime, latest_completed_session: str | None) -> dict:
        if not latest_completed_session:
            raise ValueError("交易日历未核验，无法确定可使用的龙虎榜日期")
        end = latest_completed_session
        start = (datetime.fromisoformat(end) - timedelta(days=30)).date().isoformat()
        probe = self._billboard(start, end, 1)
        first = probe.get("data") or []
        if not first:
            raise ValueError("最近 30 天未获取到龙虎榜，保留旧榜单并标为不可验证")
        day = str(first[0]["TRADE_DATE"])[:10]
        if day > end or day < start:
            raise ValueError("龙虎榜日期越界")
        payload = self._billboard(day, day, 1000)
        rows = payload.get("data") or []
        if payload.get("pages", 1) > 1 or payload.get("count", 0) > len(rows):
            raise ValueError("龙虎榜返回不完整，暂停候选筛选")
        return shape_billboard(rows, day, now)


def shape_billboard(rows: list[dict], day: str, now: datetime) -> dict:
    grouped: dict[str, dict] = {}
    for raw in rows:
        if str(raw.get("TRADE_DATE", ""))[:10] != day:
            continue
        try:
            symbol = normalize_symbol(str(raw.get("SECUCODE") or raw.get("SECURITY_CODE") or ""))
        except ValueError:
            continue
        amount = number(raw.get("BILLBOARD_DEAL_AMT"))
        if amount is None or amount < 0:
            continue
        reason = str(raw.get("EXPLANATION") or "")
        old = grouped.get(symbol)
        reasons = list(dict.fromkeys([*(old or {}).get("reasons", []), reason]))
        if old is None or amount > old["billboard_amount"]:
            grouped[symbol] = {
                "symbol": symbol, "name": str(raw.get("SECURITY_NAME_ABBR") or symbol),
                "billboard_date": day, "billboard_amount": amount,
                "net_buy_amount": number(raw.get("BILLBOARD_NET_AMT")),
                "market_amount": number(raw.get("ACCUM_AMOUNT")), "reasons": reasons,
            }
        else:
            old["reasons"] = reasons
    items = sorted(grouped.values(), key=lambda row: (-row["billboard_amount"], row["symbol"]))
    if not items:
        raise ValueError("龙虎榜中没有可识别的 A 股，不能生成股票池")
    return {"date": day, "fetched_at": now.isoformat(), "source": "eastmoney_billboard",
            "source_url": LHB_SOURCE, "rows": items, "record_count": len(rows),
            "deduplication": "同一股票按最大成交额记录去重，不累加重叠上榜原因",
            "status": "ok"}
