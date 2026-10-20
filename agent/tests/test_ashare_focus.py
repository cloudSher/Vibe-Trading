from copy import deepcopy
from datetime import timedelta
import json

import pytest

from src.ashare.calendar import market_clock
from src.ashare.focus import focus_view
from src.ashare.models import FocusPlan
from src.ashare.relay import check_relay
from tests.test_ashare_monitor import NOW, SYMBOL, Feeds, quote, account_input
from src.ashare.service import AshareService


def plan(**kw):
    value = {"session_date": "2026-10-19", "reference_date": "2026-10-16", "review_since": "2026-09-18",
             "reviewed_at": "2026-10-18T15:00:00+08:00", "stocks": [{"symbol": SYMBOL, "name": "测试股票",
             "reference_close": 9.9, "reference_vwap": 9.95, "disclosure_status": "reviewed", "reviewed_ids": []}]}
    return FocusPlan.model_validate(value | kw).model_dump(mode="json")


def history(now=NOW):
    return {SYMBOL: [{"as_of": (now - timedelta(seconds=(4-i)*30)).isoformat(), "price": p, "vwap": 9.99}
                     for i, p in enumerate([10.02, 9.98, 10., 10.01, 10.02])]}


def view(*, now=NOW, p=None, q=None, h=None, disclosures=None, risk=None, enabled=True):
    quotes = {SYMBOL: quote(now, price=10.02)}
    return focus_view(p or plan(), disclosures if disclosures is not None else {"checked_at": now.isoformat(), "items": []},
                      q if q is not None else quotes, h if h is not None else history(now),
                      risk or {"configured": True, "selection_allowed": True}, market_clock(now), now, enabled=enabled)


def test_three_source_confirmations_with_actual_pullback_are_required():
    assert view()["rows"][0]["entry_ready"]
    assert not view(h={SYMBOL: history()[SYMBOL][-3:]})["rows"][0]["entry_ready"]
    monotonic = history()
    for i, point in enumerate(monotonic[SYMBOL]): point["price"] = 10 + i * .005
    assert not view(h=monotonic)["rows"][0]["entry_ready"]


@pytest.mark.parametrize("case", ["old_date", "pre945", "closed", "stale", "paused", "risk", "gap", "one_price", "bad_vwap", "new_announcement", "disclosure_stale", "review_blocked"])
def test_entry_gates_fail_closed(case):
    args = {}
    if case == "old_date": args["p"] = plan(session_date="2026-10-20")
    elif case == "pre945": args["now"] = NOW.replace(hour=9, minute=44)
    elif case == "closed": args["now"] = NOW.replace(hour=15)
    elif case == "stale": args["q"] = {SYMBOL: quote(NOW - timedelta(seconds=91))}
    elif case == "paused": args["enabled"] = False
    elif case == "risk": args["risk"] = {"configured": True, "selection_allowed": False}
    elif case == "gap": args["p"] = plan(); args["p"]["stocks"][0]["reference_close"] = 9
    elif case == "one_price": args["q"] = {SYMBOL: quote()}; args["q"][SYMBOL].update(high=10, low=10)
    elif case == "bad_vwap": args["q"] = {SYMBOL: quote()}; args["q"][SYMBOL]["vwap"] = .1
    elif case == "new_announcement": args["disclosures"] = {"checked_at": NOW.isoformat(), "items": [{"id": "new", "symbols": [SYMBOL]}]}
    elif case == "disclosure_stale": args["disclosures"] = {"checked_at": (NOW - timedelta(seconds=601)).isoformat()}
    elif case == "review_blocked": args["p"] = plan(); args["p"]["stocks"][0]["disclosure_status"] = "blocked"
    assert not view(**args)["rows"][0]["entry_ready"]


def test_account_missing_only_allows_explicitly_blocked_market_notice():
    output = view(risk={"configured": False, "selection_allowed": False})
    assert output["rows"][0]["market_ready"]
    assert not output["rows"][0]["entry_ready"]
    notice = next(c for c in output["conditions"] if c["key"].startswith("focus_ready_"))
    assert notice["severity"] == "info" and "不能作为买入确认" in notice["message"]


def test_duplicate_ticks_and_outage_gaps_do_not_count_as_confirmation():
    h = history()
    for p in h[SYMBOL]: p["as_of"] = NOW.isoformat()
    assert not view(h=h)["rows"][0]["entry_ready"]
    h = history()
    h[SYMBOL][1]["as_of"] = (NOW - timedelta(seconds=300)).isoformat()
    assert not view(h=h)["rows"][0]["entry_ready"]


def test_reference_break_requires_three_ticks_and_persists_plan_on_restart(tmp_path):
    h = history()
    for p in h[SYMBOL][-3:]: p["price"] = 9.9
    assert any(c["key"].startswith("focus_break_") for c in view(h=h)["conditions"])
    class FocusFeeds(Feeds):
        def disclosures(self, symbols, start, now): return {"checked_at": now.isoformat(), "items": []}
    svc = AshareService(tmp_path / "watch", feeds=FocusFeeds(), clock=lambda: NOW)
    svc.set_focus(FocusPlan.model_validate(plan()))
    svc.refresh()
    assert svc.snapshot()["focus"]["disclosures_fresh"]
    restored = AshareService(svc.root, feeds=FocusFeeds(), clock=lambda: NOW)
    assert restored.snapshot()["focus"]["rows"][0]["symbol"] == SYMBOL
    assert json.loads((svc.root / "relay.json").read_text())["focus"]["session_date"] == "2026-10-19"


def write_relay(root, *, now=NOW, ready=True):
    root.mkdir(exist_ok=True)
    alert = {"id": "focus-1", "key": "focus_ready_" + SYMBOL, "severity": "warning", "message": "测试条件", "created_at": now.isoformat(), "acknowledged_at": None}
    source = {"generated_at": now.isoformat(), "monitor_running": True, "focus": {"session_date": "2026-10-19", "rows": [{"symbol": SYMBOL, "market_ready": ready, "entry_ready": ready, "quote": {"as_of": now.isoformat()}}]}, "alerts": [alert]}
    (root / "relay.json").write_text(json.dumps(source))
    return source


def test_codex_relay_dedupes_and_rejects_stale_entry_conditions(tmp_path):
    write_relay(tmp_path)
    assert check_relay(tmp_path, "2026-10-19", now=NOW)["notify"]
    assert not check_relay(tmp_path, "2026-10-19", now=NOW)["notify"]
    value = check_relay(tmp_path, "2026-10-19", now=NOW + timedelta(minutes=3))
    assert value["notify"] and value["status"] == "monitor_unavailable"
    assert not check_relay(tmp_path, "2026-10-19", now=NOW + timedelta(minutes=4))["notify"]
    write_relay(tmp_path, now=NOW + timedelta(minutes=5), ready=False)
    assert check_relay(tmp_path, "2026-10-19", now=NOW + timedelta(minutes=5))["status"] == "recovered"


def test_relay_marks_prior_risk_period_as_history_and_keeps_active_basis(tmp_path):
    source = write_relay(tmp_path)
    source["alerts"] = [{"id": "old-risk", "key": "drawdown_critical", "severity": "critical",
        "message": "历史回撤25%", "active": False, "created_at": NOW.isoformat(), "acknowledged_at": None}]
    source["risk_policy"] = {"warning_pct": 12, "restrict_pct": 16, "max_drawdown_pct": 20,
        "drawdown_basis": "monitoring_period", "period_started_at": NOW.isoformat()}
    (tmp_path / "relay.json").write_text(json.dumps(source))
    report = check_relay(tmp_path, "2026-10-19", now=NOW)
    assert report["risk_policy"]["drawdown_basis"] == "monitoring_period"
    assert report["alerts"][0]["severity"] == "info"
    assert "已不作为当前阶段触发条件" in report["alerts"][0]["message"]


def test_codex_relay_marks_lapsed_observation_as_history_not_a_buy_notice(tmp_path):
    write_relay(tmp_path, ready=False)
    report = check_relay(tmp_path, "2026-10-19", now=NOW)
    assert report["notify"]
    assert report["alerts"][0]["severity"] == "info"
    assert "当前条件已失效" in report["alerts"][0]["message"]
    assert not check_relay(tmp_path, "2026-10-19", now=NOW - timedelta(days=1))["notify"]


def test_each_new_disclosure_is_notified_once_even_while_review_is_pending(tmp_path):
    svc = AshareService(tmp_path / "watch", feeds=Feeds(), clock=lambda: NOW)
    svc.set_focus(FocusPlan.model_validate(plan()))
    svc.state["disclosures"] = {"checked_at": NOW.isoformat(), "items": [
        {"id": "AN1", "symbols": [SYMBOL], "title": "第一份"}]}
    svc._evaluate(NOW)
    svc._evaluate(NOW + timedelta(seconds=30))
    svc.state["disclosures"]["items"].append({"id": "AN2", "symbols": [SYMBOL], "title": "第二份"})
    svc._evaluate(NOW + timedelta(seconds=60))
    alerts = [a for a in svc.state["alerts"] if a["key"].startswith("focus_disclosure_" + SYMBOL)]
    assert len(alerts) == 2
    assert {a["key"] for a in alerts} == {"focus_disclosure_" + SYMBOL + "_AN1", "focus_disclosure_" + SYMBOL + "_AN2"}


def test_relay_includes_focus_quote_and_pullback_risks_but_ignores_other_pool_quotes(tmp_path):
    source = write_relay(tmp_path)
    base = source["alerts"][0]
    keys = ["quote_" + SYMBOL, "pullback_" + SYMBOL, "quote_000001.SZ", "calendar"]
    source["alerts"] = [{**base, "id": key, "key": key} for key in keys]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    result = check_relay(tmp_path, "2026-10-19", now=NOW)
    assert {a["key"] for a in result["alerts"]} == set(keys) - {"quote_000001.SZ"}
    assert not check_relay(tmp_path, "2026-10-19", now=NOW)["notify"]


def test_relay_has_no_cash_or_position_quantities(tmp_path):
    svc = AshareService(tmp_path / "watch", feeds=Feeds(), clock=lambda: NOW)
    svc.refresh()
    svc.account(account_input(987654.32, positions=[{"symbol": SYMBOL, "quantity": 12345, "available_quantity": 2345}]))
    payload = json.dumps(svc.relay_snapshot(), ensure_ascii=False)
    assert "987654.32" not in payload and "12345" not in payload and "2345" not in payload
    assert "available_quantity" not in payload and '"cash"' not in payload
