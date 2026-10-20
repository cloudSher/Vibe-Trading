"""Public-news schedule, independent of the dated trading observation plan."""
from .calendar import SHANGHAI, is_session


def news_monitoring(plan, settings, now):
    now = now.astimezone(SHANGHAI)
    recurring = bool(settings.get("recurring_news_enabled", False))
    enabled = bool(settings.get("enabled", True) and plan and plan.get("enabled") and plan.get("news_enabled"))
    session = is_session(now.date())
    minute = now.hour * 60 + now.minute
    active = enabled and (session is True and 540 <= minute <= 1260 if recurring else
                         now.date().isoformat() == plan["session_date"] and 540 <= minute <= 910)
    return {"recurring": recurring, "enabled": enabled, "active": bool(active),
            "session_date": now.date().isoformat(), "is_session": session,
            "calendar_verified": session is not None, "start": "09:00", "end": "21:00" if recurring else "15:10",
            "trading_plan_current": bool(plan and plan["session_date"] == now.date().isoformat()),
            "trading_plan_date": plan["session_date"] if plan else None}
