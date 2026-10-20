"""Shanghai clock and verified 2026 exchange closures.

Source: https://www.sse.com.cn/disclosure/dealinstruc/closed/
Verified 2026-10-18. Unknown calendar years fail closed for live signals.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
CALENDAR_SOURCE = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
_HOLIDAY_RANGES = {
    2026: [("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"),
           ("05-01", "05-05"), ("06-19", "06-21"), ("09-25", "09-27"),
           ("10-01", "10-07")],
}


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def is_session(day: date) -> bool | None:
    if day.weekday() >= 5:
        return False
    ranges = _HOLIDAY_RANGES.get(day.year)
    if ranges is None:
        return None
    md = day.strftime("%m-%d")
    return not any(start <= md <= end for start, end in ranges)


def prior_session(day: date, *, include: bool = False) -> date | None:
    candidate = day if include else day - timedelta(days=1)
    for _ in range(35):
        result = is_session(candidate)
        if result is None:
            return None
        if result:
            return candidate
        candidate -= timedelta(days=1)
    return None


def market_clock(now: datetime) -> dict:
    now = now.astimezone(SHANGHAI)
    day = now.date()
    session = is_session(day)
    minute = now.hour * 60 + now.minute
    phase, label = "closed", "休市"
    if session is None:
        phase, label = "calendar_unknown", "交易日历待核验"
    elif session:
        if minute < 9 * 60 + 15:
            phase, label = "preopen", "盘前"
        elif minute < 9 * 60 + 25:
            phase, label = "opening_auction", "开盘集合竞价"
        elif minute < 9 * 60 + 30:
            phase, label = "auction_pause", "竞价间隔"
        elif minute < 11 * 60 + 30:
            phase, label = "continuous", "连续竞价"
        elif minute < 13 * 60:
            phase, label = "lunch", "午间休市"
        elif minute < 14 * 60 + 57:
            phase, label = "continuous", "连续竞价"
        elif minute < 15 * 60:
            phase, label = "closing_auction", "收盘集合竞价"
        else:
            phase, label = "after_close", "已收盘"
    expected = day if session and minute >= 9 * 60 + 15 else prior_session(day)
    completed = prior_session(day, include=bool(session and minute >= 15 * 60))
    return {
        "now": now.isoformat(), "phase": phase, "label": label,
        "is_session": session, "live_window": phase in {"continuous", "opening_auction", "closing_auction"},
        "continuous": phase == "continuous", "calendar_verified": day.year in _HOLIDAY_RANGES,
        "expected_quote_date": expected.isoformat() if expected else None,
        "latest_completed_session": completed.isoformat() if completed else None,
        "calendar_source": CALENDAR_SOURCE,
    }


def quote_quality(quote: dict | None, clock: dict, now: datetime) -> str:
    if quote is None:
        return "missing"
    if not clock["calendar_verified"]:
        return "calendar_unknown"
    try:
        stamp = datetime.fromisoformat(quote["as_of"])
        age = (now - stamp).total_seconds()
    except (KeyError, ValueError, TypeError):
        return "timestamp_missing"
    if age < -5:
        return "future_timestamp"
    if stamp.date().isoformat() != clock["expected_quote_date"]:
        return "stale"
    if clock["live_window"] and age > 90:
        return "stale"
    if not quote.get("price") or quote["price"] <= 0:
        return "invalid_price"
    return "fresh" if clock["live_window"] else "reference"
