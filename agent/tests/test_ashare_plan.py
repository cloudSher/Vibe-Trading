"""Private-free fixtures for dated publication and real M5 signal gates."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from src.ashare.calendar import market_clock
from src.ashare.feeds import PublicFeeds, parse_five_minute, parse_tencent_five_minute
from src.ashare.focus import focus_view
from src.ashare.models import FocusPlan, WatchSettings
from src.ashare.plan import authorized_symbols, digest
from src.ashare.recurring_relay import check_recurring_relay
from src.ashare.service import AshareService
from tests.test_ashare_monitor import NOW, Feeds, SYMBOL, account_input, quote

TIME = (NOW + timedelta(days=1, seconds=30))


def plan():
    return FocusPlan.model_validate({"plan_id": "mainboard-2026-10-20", "version": 1,
        "rules_version": "breakout-retest-m5-v1", "research_cutoff": "2026-10-19T23:09:49+08:00",
        "reviewed_at": "2026-10-19T23:10:00+08:00", "session_date": "2026-10-20",
        "reference_date": "2026-10-19", "review_since": "2026-09-20", "news_enabled": False,
        "stocks": [{"symbol": SYMBOL, "name": "测试股票", "reference_close": 9.99, "reference_vwap": 9.95,
            "disclosure_status": "reviewed", "rules": {"breakout": 10.1, "hold": 10.1, "invalid_below": 10.0}}]})


def bars(now=TIME):
    values = [(9, 35, 10.04, 10.01, 100), (9, 40, 10.06, 10.03, 100),
              (9, 45, 10.08, 10.05, 100), (9, 50, 10.14, 10.06, 200),
              (9, 55, 10.13, 10.1, 100), (10, 0, 10.13, 10.11, 100)]
    return {"checked_at": now.isoformat(), "items": [{"as_of": now.replace(hour=h, minute=m, second=0).isoformat(),
        "open": low + .01, "close": close, "high": close + .02, "low": low,
        "volume_shares": vol, "amount_yuan": vol * (low + close) / 2} for h, m, close, low, vol in values], "error": None}


def view(*, p=None, b=None, q=None, now=TIME, risk=None, anns=None):
    value = (p or plan()).model_dump(mode="json")
    value.update(published_at="2026-10-20T00:00:00+08:00", content_hash=digest(value))
    return focus_view(value, {"checked_at": now.isoformat(), "items": anns or []},
        {SYMBOL: q or quote(now, price=10.13) | {"high": 10.17, "vwap": 10.06, "previous_close": 9.99}}, {},
        risk or {"configured": True, "selection_allowed": True, "max_drawdown_pct": 20},
        market_clock(now), now, bars={SYMBOL: b if b is not None else bars(now)})


def test_complete_volume_breakout_and_next_closed_retest_are_required():
    result = view()
    row = result["rows"][0]
    assert row["entry_ready"] and row["price_evidence"]["volume_ratio"] == 2
    assert row["price_evidence"]["retest_at"].startswith("2026-10-20T09:55")
    assert authorized_symbols(result) == {SYMBOL}
    assert "10.10" in " ".join(row["observation_conditions"])


@pytest.mark.parametrize("case", ["volume", "gap", "missing", "stale", "unfinished", "retest", "old_day", "pre945", "high_gap", "deviation", "announcement", "risk", "pending", "ex_rights"])
def test_structured_conditions_fail_closed(case):
    b, p, args = bars(), plan(), {}
    if case == "volume": b["items"][3]["volume_shares"] = 149
    elif case == "gap": b["items"].pop(1)
    elif case == "missing": b = {}
    elif case == "stale": b["checked_at"] = (TIME - timedelta(seconds=151)).isoformat()
    elif case == "unfinished": args["now"] = TIME.replace(hour=9, minute=55, second=2)
    elif case == "retest": b["items"][4]["low"] = 10.09
    elif case == "old_day": args["now"] = TIME + timedelta(days=1)
    elif case == "pre945": args["now"] = TIME.replace(hour=9, minute=44)
    elif case == "high_gap": args["q"] = quote(TIME, price=10.13) | {"open": 10.5, "vwap": 10.06, "high": 10.6}
    elif case == "deviation": args["q"] = quote(TIME, price=10.4) | {"vwap": 10.06, "high": 10.41}
    elif case == "announcement": args["anns"] = [{"id": "AN-new", "symbols": [SYMBOL], "title": "新公告"}]
    elif case == "risk": args["risk"] = {"configured": False, "selection_allowed": False, "max_drawdown_pct": 20}
    elif case == "pending": p.stocks[0].disclosure_status = "pending"
    elif case == "ex_rights": args["q"] = quote(TIME, price=10.13) | {"vwap": 10.06, "high": 10.17, "previous_close": 9.8}
    assert not view(p=p, b=b, **args)["rows"][0]["entry_ready"]


def test_account_block_preserves_price_evidence_and_invalidates_after_break():
    risk = {"configured": True, "selection_allowed": False, "max_drawdown_pct": 20}
    output = view(risk=risk)
    assert output["rows"][0]["market_ready"] and not output["rows"][0]["entry_ready"]
    assert "账户" in output["rows"][0]["risk_blockers"][0]
    b = bars(); b["items"][-1].update(close=9.98, low=9.97)
    broken = view(b=b, q=quote(TIME, price=9.98))["rows"][0]
    assert broken["price_evidence"]["invalidated"] and broken["status"] == "观察已失效"


def test_publish_is_idempotent_immutable_persistent_and_preserves_financial_state(tmp_path):
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: TIME)
    svc.account(account_input())
    svc.state["settings"] = WatchSettings(warning_drawdown_pct=12, restrict_drawdown_pct=16, max_drawdown_pct=20).model_dump()
    protected = {k: deepcopy(svc.state[k]) for k in ("account", "ledger", "settings")}
    svc.publish_focus(plan())
    published = deepcopy(svc.state["focus"])
    svc.publish_focus(plan())
    assert svc.state["focus"] == published
    assert len(svc.state["focus_publication_audit"]) == 1
    assert svc.state["focus_publication_audit"][0]["protected_fields_unchanged"] == dict.fromkeys(protected, True)
    assert all(svc.state[k] == v for k, v in protected.items())
    changed = plan(); changed.stocks[0].rules.breakout = 10.2
    with pytest.raises(ValueError, match="不可覆盖"): svc.publish_focus(changed)
    changed.version = 2
    svc.publish_focus(changed)
    assert svc.state["focus_plan_history"][0]["plan"] == published
    restored = AshareService(tmp_path, feeds=Feeds(), clock=lambda: TIME)
    assert restored.plan_snapshot()["focus"]["version"] == 2
    assert all(restored.state[k] == v for k, v in protected.items())
    public = json.dumps(restored.plan_snapshot(), ensure_ascii=False)
    for forbidden in ('"cash"', "available_quantity", "historical_peak_equity", '"last_nav"'):
        assert forbidden not in public


def test_recurring_relay_uses_published_scope_and_version_dedupes_and_detects_corruption(tmp_path):
    focus = view()
    identity = {k: focus[k] for k in ("plan_id", "version", "content_hash")}
    notice = focus["conditions"][0] | {"id": "new-plan-event", "created_at": TIME.isoformat(), "active": True}
    source = {"generated_at": TIME.isoformat(), "monitor_running": True,
        "news_monitoring": {"recurring": True, "enabled": True, "session_date": "2026-10-20"},
        "focus": focus, "market": market_clock(TIME), "health": {"news": True, "disclosures": True, "quotes": True, "focus_m5": True},
        "alerts": [notice, notice | {"id": "old", "plan_identity": identity | {"version": 0}}]}
    path = tmp_path / "relay.json"
    path.write_text(json.dumps(source))
    output = check_recurring_relay(tmp_path, now=TIME)
    assert output["notify"] and output["plan_identity"] == identity
    assert [a["id"] for a in output["alerts"]] == ["new-plan-event"]
    assert output["observations"][0]["observation_conditions"] == focus["rows"][0]["observation_conditions"]
    assert not check_recurring_relay(tmp_path, now=TIME)["notify"]
    source["focus"]["rows"][0]["rules"]["breakout"] = 99
    path.write_text(json.dumps(source))
    assert check_recurring_relay(tmp_path, now=TIME)["status"] == "monitor_unavailable"
    assert not check_recurring_relay(tmp_path, now=TIME)["notify"]
    source["focus"] = view()
    path.write_text(json.dumps(source))
    assert check_recurring_relay(tmp_path, now=TIME)["status"] == "recovered"
    bad_identity = identity | {"version": 2}
    assert check_recurring_relay(tmp_path, now=TIME, expected_plan=bad_identity)["notify"]
    assert not check_recurring_relay(tmp_path, now=TIME, expected_plan=bad_identity)["notify"]
    assert check_recurring_relay(tmp_path, now=TIME, expected_plan=identity)["status"] == "recovered"


def test_m5_parser_enforces_symbol_units_and_source_timestamps():
    payload = {"rc": 0, "data": {"code": "600000", "market": 1,
        "klines": ["2026-10-20 09:35,10.0,10.1,10.2,9.9,200,200000"]}}
    parsed = parse_five_minute(payload, SYMBOL, TIME)
    assert parsed["items"][0]["volume_shares"] == 20000
    assert parsed["items"][0]["as_of"] == "2026-10-20T09:35:00+08:00"
    with pytest.raises(ValueError): parse_five_minute(payload, "000933.SZ", TIME)


def test_old_announcements_are_grouped_as_background_not_new_catalysts():
    output = view(anns=[{"id": "old-1", "date": "2026-10-16", "title": "历史公告", "symbols": [SYMBOL]},
                        {"id": "old-2", "date": "2026-10-17", "title": "历史公告2", "symbols": [SYMBOL]}])
    alerts = [c for c in output["conditions"] if c["key"].startswith("focus_disclosure_")]
    assert len(alerts) == 1 and "不是今日新催化" in alerts[0]["message"]
    assert alerts[0]["announcement_ids"] == ["old-1", "old-2"]


def test_m5_fallback_has_actual_volume_and_never_fabricates_bar_amount(monkeypatch):
    from src.ashare import feeds as module
    import requests
    payload = {"code": 0, "data": {"sh600000": {"m5": [
        ["202610200935", "10", "10.1", "10.2", "9.9", "200", {}, "0"]]}}}
    parsed = parse_tencent_five_minute(payload, SYMBOL, TIME)
    assert parsed["items"][0]["volume_shares"] == 20000
    assert parsed["items"][0]["amount_yuan"] is None
    calls = []
    class Response:
        def json(self): return payload
    def get(url, **kw):
        calls.append(url)
        if "push2his" in url: raise requests.ConnectionError("fixture failure")
        return Response()
    monkeypatch.setattr(module, "_get", get)
    feed = PublicFeeds()
    assert feed.five_minute_bars(SYMBOL, TIME)["source"] == "tencent_public_m5"
    feed.five_minute_bars(SYMBOL, TIME)
    assert sum("push2his" in u for u in calls) == 1
    with pytest.raises(ValueError): parse_tencent_five_minute(payload, "000933.SZ", TIME)


def test_relay_m5_failure_once_then_recovery_without_changing_plan(tmp_path):
    focus = view()
    source = {"generated_at": TIME.isoformat(), "monitor_running": True,
        "news_monitoring": {"recurring": True, "enabled": True, "session_date": "2026-10-20"},
        "focus": focus, "market": market_clock(TIME), "health": {"news": True, "disclosures": True, "quotes": True, "focus_m5": False}, "alerts": []}
    path = tmp_path / "relay.json"; path.write_text(json.dumps(source))
    assert check_recurring_relay(tmp_path, now=TIME)["alerts"][0]["key"] == "health_focus_m5"
    assert not check_recurring_relay(tmp_path, now=TIME)["notify"]
    source["health"]["focus_m5"] = True; path.write_text(json.dumps(source))
    assert "已恢复" in check_recurring_relay(tmp_path, now=TIME)["alerts"][0]["message"]
    assert not check_recurring_relay(tmp_path, now=TIME)["notify"]


def test_live_like_service_emits_plan_scoped_invalid_alert_and_no_model_account_context(tmp_path):
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: TIME)
    svc.publish_focus(plan())
    svc.state["quotes"] = {SYMBOL: quote(TIME, price=10.13) | {"vwap": 10.06, "high": 10.17, "previous_close": 9.99}}
    svc.state["focus_bars"] = {SYMBOL: bars()}
    svc.state["disclosures"] = {"checked_at": TIME.isoformat(), "items": []}
    svc._evaluate(TIME)
    assert any(a["key"] == "focus_ready_" + SYMBOL and a["plan_identity"]["version"] == 1 for a in svc.state["alerts"])
    svc.state["quotes"][SYMBOL]["price"] = 9.98
    svc.state["focus_bars"][SYMBOL]["items"][-1].update(close=9.98, low=9.97)
    svc._evaluate(TIME + timedelta(seconds=30))
    assert any(a["key"] == "focus_invalid_" + SYMBOL for a in svc.state["alerts"])
    public = svc.public_snapshot()["focus"]
    assert "risk_policy" not in public and "risk_blockers" not in public["rows"][0]
