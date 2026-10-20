"""Recurring public-news monitoring across days, with no account/broker access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from src.ashare.calendar import SHANGHAI, market_clock
from src.ashare.focus import focus_view
from src.ashare.models import FocusPlan, NewsMonitoringInput
from src.ashare.news import news_view
from src.ashare.news_feeds import article
from src.ashare.recurring_relay import SYMBOLS
from src.ashare.relay import check_relay
from src.ashare.schedule import news_monitoring
from src.ashare.service import AshareService
from tests.test_ashare_focus import plan
from tests.test_ashare_monitor import NOW, Feeds, account_input, quote
from tests.test_ashare_news import analyzed

S = "000887.SZ"
NAMES = {S: "中鼎股份", "600699.SH": "均胜电子", "001696.SZ": "宗申动力", "600319.SH": "亚星化学", "002531.SZ": "天顺风能"}


def five_plan():
    return plan(news_enabled=True, stocks=[dict(symbol=s, name=NAMES[s], reference_close=9.9,
        reference_vwap=10.1, disclosure_status="blocked" if s == "001696.SZ" else "reviewed", reviewed_ids=[]) for s in sorted(SYMBOLS)])


def event(now, title="中鼎股份发布业务公告", content="事项进展仍以公告原文为准"):
    return article("财联社", title, content, now.isoformat(), "https://www.cls.cn/detail/fixture", now, five_plan()["stocks"])


def alert(now=NOW, key="focus_disclosure_" + S + "_AN1", id="alert-1", **fields):
    return dict(key=key, id=id, severity="warning", message="新增未核验公告，请阅读原文", created_at=now.isoformat(), acknowledged_at=None) | fields


def write_source(root, now=NOW, alerts=None, **fields):
    rows = [{"symbol": s, "name": NAMES[s], "quote": quote(now, price=10), "quality": "fresh",
        "reference_vwap": 10.1, "market_ready": False, "entry_ready": False, "status": "待核验", "reasons": [],
        "news": {"events": [], "blocked": [], "healthy": True}, "announcements": [],
        "observation_conditions": ["旧参考位"], "invalidation_conditions": []} for s in sorted(SYMBOLS)]
    for row in rows:
        row["quote"].update(symbol=row["symbol"], high=10.4)
    value = {"generated_at": now.isoformat(), "monitor_running": True,
        "news_monitoring": news_monitoring(five_plan(), {"recurring_news_enabled": True}, now),
        "market": market_clock(now), "focus": {"session_date": "2026-10-19", "rows": rows},
        "risk_policy": {"warning_pct": 12, "restrict_pct": 16, "max_drawdown_pct": 20, "drawdown_basis": "historical_high"},
        "health": {"news": True, "disclosures": True, "quotes": True}, "alerts": alerts if alerts is not None else [alert(now)]} | fields
    (root / "relay.json").write_text(json.dumps(value))
    return value


@pytest.mark.parametrize("stamp,active", [("2026-10-19T08:59", False), ("2026-10-19T09:00", True),
    ("2026-10-19T12:00", True), ("2026-10-19T20:59", True), ("2026-10-19T21:00", True),
    ("2026-10-19T21:01", False), ("2026-10-20T10:00", True), ("2026-10-24T10:00", False),
    ("2026-10-01T10:00", False), ("2027-01-04T10:00", False)])
def test_recurring_window_uses_exchange_days_and_beijing_time(stamp, active):
    now = datetime.fromisoformat(stamp).replace(tzinfo=SHANGHAI)
    assert news_monitoring(five_plan(), {"recurring_news_enabled": True}, now)["active"] is active
    assert news_monitoring(five_plan(), {"recurring_news_enabled": True}, now.astimezone(timezone.utc))["active"] is active


def test_next_day_news_and_announcements_continue_without_rolling_trading_plan(tmp_path):
    now = NOW + timedelta(days=1)
    class News:
        def cls(self, stocks, stamp, previous): return [event(stamp)], stamp.timestamp()
        def eastmoney(self, stock, stocks, stamp): return []
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: now, news_feeds=News(),
        news_analyzer=lambda events: {e["id"]: analyzed(e, "neutral") for e in events})
    svc.set_focus(FocusPlan.model_validate(five_plan()))
    before = deepcopy(svc.state["focus"])
    svc.set_news_monitoring(NewsMonitoringInput(enabled=True))
    svc.refresh_news()
    svc.analyze_pending_news()
    svc.state["quotes"] = {S: quote(now)}
    svc.state["disclosures"] = {"checked_at": now.isoformat(), "items": [{"id": "AN-new", "symbols": [S], "title": "新公告", "source_url": "https://data.eastmoney.com/notices/detail/1"}]}
    svc._evaluate(now)
    view = svc.snapshot()
    assert svc.state["focus"] == before
    assert view["news"]["monitoring"]["active"]
    assert not view["news"]["monitoring"]["trading_plan_current"]
    assert all(not r["entry_ready"] for r in view["focus"]["rows"])
    assert any(a["key"].startswith("focus_news_" + S) for a in view["alerts"])
    assert any(a["key"] == "focus_disclosure_" + S + "_AN-new" for a in view["alerts"])
    public = json.dumps(svc.public_snapshot())
    relay = json.dumps(svc.relay_snapshot())
    for private in ("historical_peak_equity", "available_quantity", "start_equity", '"cash"'):
        assert private not in public and private not in relay


@pytest.mark.parametrize("stamp", ["2026-10-19T21:01", "2026-10-24T10:00", "2027-01-04T10:00"])
def test_no_network_or_model_jobs_outside_verified_schedule(tmp_path, stamp):
    now = datetime.fromisoformat(stamp).replace(tzinfo=SHANGHAI)
    class NoNews:
        def cls(self, *args): pytest.fail("outside-window network call")
        def eastmoney(self, *args): pytest.fail("outside-window network call")
    def no_model(*args): pytest.fail("outside-window model call")
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: now, news_feeds=NoNews(), news_analyzer=no_model)
    svc.set_focus(FocusPlan.model_validate(five_plan()))
    svc.set_news_monitoring(NewsMonitoringInput(enabled=True))
    svc.refresh_news()
    svc.analyze_pending_news()
    assert not svc.state["news"]["sources"]


def test_monitoring_toggle_preserves_account_thresholds_and_focus_on_restart(tmp_path):
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: NOW)
    svc.set_focus(FocusPlan.model_validate(five_plan()))
    svc.account(account_input())
    account, ledger, focus, settings = [deepcopy(svc.state[k]) for k in ("account", "ledger", "focus", "settings")]
    svc.set_news_monitoring(NewsMonitoringInput(enabled=True))
    assert svc.state["account"] == account and svc.state["ledger"] == ledger and svc.state["focus"] == focus
    expected = settings | {"recurring_news_enabled": True}
    assert svc.state["settings"] == expected
    restored = AshareService(tmp_path, feeds=Feeds(), clock=lambda: NOW)
    assert restored.state["settings"] == expected
    assert len(restored.state["news_monitoring_audit"]) == 1
    restored.set_news_monitoring(NewsMonitoringInput(enabled=False))
    assert not restored.state["settings"]["recurring_news_enabled"]
    assert restored.state["account"] == account


def test_recurring_relay_reads_only_reduced_snapshot_and_cursor(tmp_path, monkeypatch):
    write_source(tmp_path)
    read = Path.read_text
    def permitted(path, *args, **kwargs):
        assert path.name in {"relay.json", "codex-relay-cursor.json"}
        return read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", permitted)
    assert check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]


def test_repeated_fault_reports_once_and_verified_recovery_once(tmp_path):
    source = write_source(tmp_path, alerts=[], health={"news": False, "disclosures": True, "quotes": True})
    first = check_relay(tmp_path, recurring=True, now=NOW)
    assert first["alerts"][0]["key"] == "health_news"
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    source["health"]["news"] = True
    (tmp_path / "relay.json").write_text(json.dumps(source))
    recovered = check_relay(tmp_path, recurring=True, now=NOW)
    assert recovered["alerts"][0]["severity"] == "info" and "恢复" in recovered["alerts"][0]["message"]
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]


def test_same_condition_ids_are_suppressed_until_observed_price_recovery(tmp_path):
    key = "pullback_" + S
    source = write_source(tmp_path, alerts=[alert(key=key)])
    assert check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    source["alerts"] = [alert(key=key, id="repeat")]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    row = next(r for r in source["focus"]["rows"] if r["symbol"] == S)
    row["quote"]["price"] = 10.3
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    row["quote"]["price"] = 10
    source["alerts"] = [alert(key=key, id="recurred")]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert check_relay(tmp_path, recurring=True, now=NOW)["notify"]


def test_next_day_expired_plan_warns_once_and_never_exposes_old_buy_levels(tmp_path):
    now = NOW + timedelta(days=1)
    source = write_source(tmp_path, now=now)
    for row in source["focus"]["rows"]:
        row.update(entry_ready=True, market_ready=True)  # Even a misleading retained source is fenced.
    (tmp_path / "relay.json").write_text(json.dumps(source))
    result = check_relay(tmp_path, recurring=True, now=now)
    assert any(a["key"] == "plan_expired" for a in result["alerts"])
    assert not result["observations"][0]["entry_ready"]
    assert "旧参考位" not in json.dumps(result["observations"], ensure_ascii=False)
    assert not check_relay(tmp_path, recurring=True, now=now)["notify"]


def test_news_original_and_direction_change_are_delivered_even_if_not_in_top_three(tmp_path):
    e = {"id": "evt-one", "title": "消息", "url": "https://www.cls.cn/detail/123", "published_at": NOW.isoformat(),
        "snippet": "【消息】测试公司的经营情况以原文为准", "analysis": {"impact": "neutral", "summary": "中性", "evidence": "经营情况以原文为准"}}
    source = write_source(tmp_path, alerts=[alert(key="focus_news_" + S + "_evt_neutral", news_event=e)])
    first = check_relay(tmp_path, recurring=True, now=NOW)
    assert first["observations"][0]["news"]["events"][0]["url"] == e["url"]
    e["title"] = "标题编辑"; e["snippet"] = "【标题编辑】测试公司的经营情况以原文为准"
    source["alerts"] = [alert(key="focus_news_" + S + "_evt_neutral", id="renamed", news_event=e)]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    e["analysis"]["impact"] = "negative"
    source["alerts"] = [alert(key="focus_news_" + S + "_evt_negative", id="direction-change", news_event=e)]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert check_relay(tmp_path, recurring=True, now=NOW)["notify"]


def test_large_batch_tail_is_not_consumed(tmp_path):
    records = [alert(key="focus_disclosure_" + S + "_AN" + str(n), id="id" + str(n)) for n in range(15)]
    write_source(tmp_path, alerts=records)
    first = check_relay(tmp_path, recurring=True, now=NOW)
    second = check_relay(tmp_path, recurring=True, now=NOW)
    assert len(first["alerts"]) == 12 and len(second["alerts"]) == 3
    assert len({a["id"] for a in first["alerts"] + second["alerts"]}) == 15


def test_unknown_calendar_fault_once_and_holiday_no_cursor_write(tmp_path):
    holiday = datetime(2026, 10, 1, 10, tzinfo=SHANGHAI)
    assert not check_relay(tmp_path, recurring=True, now=holiday)["notify"]
    assert not (tmp_path / "codex-relay-cursor.json").exists()
    unknown = datetime(2027, 1, 4, 10, tzinfo=SHANGHAI)
    assert check_relay(tmp_path, recurring=True, now=unknown)["notify"]
    assert not check_relay(tmp_path, recurring=True, now=unknown)["notify"]


def test_recurring_relay_after_close_is_reference_only_and_pauses_without_notifications(tmp_path):
    now = NOW.replace(hour=18)
    source = write_source(tmp_path, now=now, alerts=[alert(now, key="focus_ready_" + S)])
    for row in source["focus"]["rows"]:
        row.update(entry_ready=True, market_ready=True)
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert not check_relay(tmp_path, recurring=True, now=now)["notify"]
    source["news_monitoring"]["enabled"] = False
    source["alerts"] = [alert(now, id="unread")]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert check_relay(tmp_path, recurring=True, now=now)["status"] == "paused"
    assert not check_relay(tmp_path, recurring=True, now=now.replace(hour=21, minute=1))["notify"]


def test_resume_and_next_day_cls_checkpoints_never_force_an_overnight_backfill(tmp_path):
    clock = [NOW.replace(hour=18)]
    checkpoints = []
    class News:
        def cls(self, stocks, stamp, previous):
            checkpoints.append(previous)
            return [], stamp.timestamp()
        def eastmoney(self, *args): return []
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: clock[0], news_feeds=News())
    svc.set_focus(FocusPlan.model_validate(five_plan()))
    svc.state["news"]["sources"]["cls"] = {"checked_at": NOW.isoformat(), "latest_timestamp": NOW.timestamp()}
    svc.set_news_monitoring(NewsMonitoringInput(enabled=True))
    svc.refresh_news()
    clock[0] += timedelta(seconds=31)
    svc.refresh_news()
    clock[0] = NOW + timedelta(days=1)
    svc.refresh_news()
    assert checkpoints == [None, NOW.replace(hour=18).timestamp(), None]
