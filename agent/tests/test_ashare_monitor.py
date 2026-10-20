"""Offline market-quality, accounting and manual-only monitoring regressions."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
import stat

import pytest
from pydantic import ValidationError

from src.ashare.calendar import SHANGHAI, is_session, market_clock, quote_quality
from src.ashare.feeds import PublicFeeds, parse_tencent, shape_billboard
from src.ashare.models import AccountInput, Holding, RiskPeriodInput, WatchSettings, normalize_symbol
from src.ashare.policy import observations, risk_assessment
from src.ashare.service import AshareService


NOW = datetime(2026, 10, 19, 10, 0, tzinfo=SHANGHAI)
SYMBOL = "600000.SH"


def quote(now=NOW, price=10, **kw):
    return dict(symbol=SYMBOL, name="测试股票", kind="stock", price=price, previous_close=9.8,
                open=9.9, high=10.1, low=9.8, vwap=9.95, change_pct=2.04,
                amount_yuan=500_000_000, turnover_pct=4, limit_up=10.78, limit_down=8.82,
                as_of=now.isoformat(), received_at=now.isoformat(), source="test", **kw)


def account(cash=1_000_000, **kw):
    return dict(submission_id="fixture-001", cash=cash, positions=[], historical_peak_equity=1_000_000,
                peak_as_of=(NOW - timedelta(days=3)).isoformat(), confirmed_at=NOW.isoformat(), external_flow=0) | kw


def account_input(cash=1_000_000, **kw):
    value = account(cash, **kw)
    value.pop("confirmed_at")
    return AccountInput.model_validate(value)


def assess(value, *, ledger=None, quotes=None, now=NOW):
    return risk_assessment(value, quotes or {}, ledger or {}, WatchSettings().model_dump(), market_clock(now), now)


@pytest.mark.parametrize("raw, expected", [("600000", SYMBOL), ("sh600000", SYMBOL), ("000001", "000001.SZ"), ("920001", "920001.BJ")])
def test_stock_normalization(raw, expected):
    assert normalize_symbol(raw) == expected


@pytest.mark.parametrize("raw", ["000001.SH", "510300.SH", "https://host/600000", "999999.SZ"])
def test_non_stock_or_url_rejected(raw):
    with pytest.raises(ValueError):
        normalize_symbol(raw)


@pytest.mark.parametrize("quantity,available", [(100, 101), (100.5, 0), (100, -1)])
def test_manual_available_quantity_validation(quantity, available):
    with pytest.raises(ValidationError):
        Holding(symbol=SYMBOL, quantity=quantity, available_quantity=available)


def test_no_nonfinite_or_duplicate_account_values():
    with pytest.raises(ValidationError):
        account_input(float("inf"))
    p = dict(symbol=SYMBOL, quantity=100, available_quantity=0)
    with pytest.raises(ValidationError):
        account_input(positions=[p, p])


@pytest.mark.parametrize("day, expected", [("2026-10-01", False), ("2026-10-10", False), ("2026-10-19", True), ("2027-01-04", None)])
def test_exchange_calendar_does_not_use_makeup_workdays(day, expected):
    assert is_session(date.fromisoformat(day)) is expected


@pytest.mark.parametrize("hour,minute,phase", [(9, 20, "opening_auction"), (9, 26, "auction_pause"), (10, 0, "continuous"), (11, 30, "lunch"), (14, 58, "closing_auction"), (15, 0, "after_close")])
def test_session_phases(hour, minute, phase):
    assert market_clock(NOW.replace(hour=hour, minute=minute))["phase"] == phase


def test_quote_time_is_source_time_and_stale_fails_closed():
    clock = market_clock(NOW)
    assert quote_quality(quote(), clock, NOW) == "fresh"
    assert quote_quality(quote(NOW - timedelta(seconds=91)), clock, NOW) == "stale"
    assert quote_quality(quote(NOW + timedelta(seconds=6)), clock, NOW) == "future_timestamp"
    assert quote_quality(None, clock, NOW) == "missing"
    weekend = datetime(2026, 10, 18, 12, tzinfo=SHANGHAI)
    friday = datetime(2026, 10, 16, 16, 15, tzinfo=SHANGHAI)
    assert quote_quality(quote(friday), market_clock(weekend), weekend) == "reference"
    assert market_clock(weekend)["latest_completed_session"] == "2026-10-16"


def wire(fields):
    return 'v_sh600000="' + "~".join(fields) + '";'


def quote_fields():
    fields = [""] * 60
    for i, value in {1: "测试股票", 2: "600000", 3: "10", 4: "9.8", 5: "9.9", 6: "1000",
                     30: "20261019100000", 33: "10.1", 34: "9.8", 37: "100", 38: "4", 47: "10.78", 48: "8.82"}.items():
        fields[i] = value
    return fields


def test_tencent_units_and_source_timestamp():
    q = parse_tencent(wire(quote_fields()), {"sh600000": SYMBOL}, NOW + timedelta(seconds=10))[SYMBOL]
    assert q["volume_shares"] == 100_000
    assert q["amount_yuan"] == 1_000_000
    assert q["vwap"] == 10
    assert q["as_of"] == NOW.isoformat()
    assert q["received_at"] != q["as_of"]
    assert q["adjustment"] == "unadjusted"
    assert not q["latency_guaranteed"]


@pytest.mark.parametrize("field,value", [(3, "nan"), (3, "0"), (30, ""), (2, "600001")])
def test_bad_wire_quote_omitted(field, value):
    fields = quote_fields()
    fields[field] = value
    assert not parse_tencent(wire(fields), {"sh600000": SYMBOL}, NOW)


def raw_board(symbol=SYMBOL, amount=1e9, **kw):
    return dict(SECUCODE=symbol, TRADE_DATE="2026-10-16 00:00:00", SECURITY_NAME_ABBR="测试股票",
                BILLBOARD_DEAL_AMT=amount, BILLBOARD_NET_AMT=3e8, EXPLANATION="日涨幅偏离") | kw


def test_board_duplicate_reasons_do_not_double_count_amounts():
    board = shape_billboard([raw_board(amount=2e9), raw_board(amount=1e9, EXPLANATION="三日偏离"),
                             raw_board("000001.SH"), raw_board(TRADE_DATE="2026-10-19")], "2026-10-16", NOW)
    assert len(board["rows"]) == 1
    row = board["rows"][0]
    assert row["billboard_amount"] == 2e9
    assert len(row["reasons"]) == 2
    assert "D1" not in json.dumps(row)


def test_truncated_board_is_rejected(monkeypatch):
    feeds = PublicFeeds()
    responses = iter([dict(data=[raw_board()]), dict(data=[raw_board()], pages=2, count=1001)])
    monkeypatch.setattr(feeds, "_billboard", lambda *args: next(responses))
    with pytest.raises(ValueError, match="不完整"):
        feeds.billboard(NOW, "2026-10-16")


@pytest.mark.parametrize("equity,level,latched", [(1e6, "normal", False), (970000, "warning", False), (960000, "restrict", False), (950000, "critical", True)])
def test_exact_drawdown_thresholds(equity, level, latched):
    _, risk = assess(account(equity))
    assert risk["level"] == level
    assert risk["latched"] is latched
    assert risk["selection_allowed"] is (level in {"normal", "warning"})


@pytest.mark.parametrize("values", [dict(warning_drawdown_pct=4), dict(restrict_drawdown_pct=5),
    dict(max_drawdown_pct=100), dict(max_drawdown_pct=float("nan")), dict(warning_drawdown_pct=0)])
def test_invalid_operator_drawdown_thresholds_rejected(values):
    with pytest.raises(ValidationError):
        WatchSettings(**values)


@pytest.mark.parametrize("equity,level", [(900000, "normal"), (880000, "warning"), (840000, "restrict"), (800000, "critical")])
def test_custom_drawdown_policy_controls_actual_edges_not_only_labels(equity, level):
    settings = WatchSettings(warning_drawdown_pct=12, restrict_drawdown_pct=16, max_drawdown_pct=20).model_dump()
    _, risk = risk_assessment(account(equity), {}, {}, settings, market_clock(NOW), NOW)
    assert risk["level"] == level and risk["max_drawdown_pct"] == 20
    assert risk["selection_allowed"] is (level in {"normal", "warning"})
    assert risk["budget_to_limit"] == pytest.approx(max(0, equity - 800000))


def test_larger_threshold_does_not_erase_high_water_or_breach():
    ledger, _ = assess(account(950000))
    settings = WatchSettings(warning_drawdown_pct=12, restrict_drawdown_pct=16, max_drawdown_pct=20).model_dump()
    changed, risk = risk_assessment(account(950000), {}, ledger, settings, market_clock(NOW), NOW)
    assert risk["latched"] and not risk["selection_allowed"]
    assert changed["peak_nav"] == ledger["peak_nav"] and changed["units"] == ledger["units"]
    assert risk["drawdown_pct"] == pytest.approx(5)


def test_policy_edits_are_audited_and_survive_restart(tmp_path):
    service = AshareService(tmp_path, clock=lambda: NOW)
    value = WatchSettings(warning_drawdown_pct=12, restrict_drawdown_pct=16, max_drawdown_pct=20)
    service.settings(value)
    service.settings(value)  # Duplicate save is not another change.
    reloaded = AshareService(tmp_path, clock=lambda: NOW)
    assert reloaded.snapshot()["risk"]["max_drawdown_pct"] == 20
    assert len(reloaded.state["risk_policy_audit"]) == 1
    audit = reloaded.state["risk_policy_audit"][0]
    assert audit["before"]["max_drawdown_pct"] == 5 and audit["after"]["max_drawdown_pct"] == 20
    assert reloaded.state["account"] is None and reloaded.state["settings"]["max_single_position_pct"] == 20


def test_data_missing_never_reports_zero_drawdown_or_unlocks_shares():
    value = account(999000, positions=[dict(symbol=SYMBOL, quantity=100, available_quantity=0, cost_price=9)])
    _, risk = assess(value)
    assert not risk["complete"] and risk["drawdown_pct"] is None
    _, risk = assess(value, quotes={SYMBOL: quote()})
    assert risk["holdings"][0]["locked_quantity"] == 100
    assert risk["holdings"][0]["available_quantity"] == 0
    _, old = assess(value, quotes={SYMBOL: quote(NOW - timedelta(minutes=2))})
    assert not old["complete"] and not old["selection_allowed"]
    _, old_account = assess(value | {"confirmed_at": "2026-10-16T16:00:00+08:00"}, quotes={SYMBOL: quote()})
    assert not old_account["complete"]


@pytest.mark.parametrize("flow", [100000, -100000])
def test_cash_flows_preserve_unit_drawdown(flow):
    ledger, before = assess(account(970000))
    ledger["pending_flow"] = flow
    after_ledger, after = assess(account(970000 + flow), ledger=ledger)
    assert after["drawdown_pct"] == pytest.approx(before["drawdown_pct"])
    assert after_ledger["pending_flow"] == 0
    # Re-evaluating a saved ledger cannot apply the flow twice.
    _, repeated = assess(account(970000 + flow), ledger=after_ledger)
    assert repeated["drawdown_pct"] == pytest.approx(3)


def test_new_high_recorded_and_five_percent_latch_survives_recovery():
    ledger, _ = assess(account())
    ledger, _ = assess(account(1100000), ledger=ledger)
    ledger, risk = assess(account(1045000), ledger=ledger)
    assert risk["latched"] and risk["drawdown_pct"] == pytest.approx(5)
    _, recovery = assess(account(1100000), ledger=ledger)
    assert recovery["latched"] and not recovery["selection_allowed"]


def board():
    return shape_billboard([raw_board()], "2026-10-16", NOW)


def test_market_candidate_is_observation_and_blocks_bad_data():
    pool, q = board(), {SYMBOL: quote()}
    assert observations(pool, q, {}, market_clock(NOW), NOW, pool_valid=True)[0]["candidate"]
    assert not observations(pool, q, {}, market_clock(NOW), NOW, pool_valid=False)[0]["candidate"]
    q[SYMBOL]["name"] = "*ST测试"
    assert not observations(pool, q, {}, market_clock(NOW), NOW, pool_valid=True)[0]["candidate"]


def test_five_minute_change_needs_real_history_not_poll_count():
    history = {SYMBOL: [dict(as_of=(NOW - timedelta(seconds=300)).isoformat(), price=9.9)]}
    result = observations(board(), {SYMBOL: quote()}, history, market_clock(NOW), NOW, pool_valid=True)[0]
    assert result["five_minute_change_pct"] == pytest.approx((10 / 9.9 - 1) * 100)
    history[SYMBOL][0]["as_of"] = (NOW - timedelta(seconds=400)).isoformat()
    assert observations(board(), {SYMBOL: quote()}, history, market_clock(NOW), NOW, pool_valid=True)[0]["five_minute_change_pct"] is None


class Feeds:
    fail = False
    boards = 0

    def billboard(self, now, completed):
        self.boards += 1
        return board()

    def quotes(self, symbols, now):
        if self.fail:
            raise TimeoutError("offline")
        return {SYMBOL: quote(now)}


@pytest.fixture
def service(tmp_path):
    now = [NOW]
    feeds = Feeds()
    svc = AshareService(tmp_path / "ashare", feeds=feeds, clock=lambda: now[0])
    svc.test_clock = now
    svc.refresh()
    return svc


def test_saved_highwater_latch_dedupe_ack_and_manual_resume(service):
    view = service.account(account_input(950000))
    assert view["risk"]["latched"]
    count = len(view["alerts"])
    service._evaluate(NOW)
    assert len(service.snapshot()["alerts"]) == count
    service.acknowledge([a["id"] for a in view["alerts"]])
    assert service.snapshot()["unread_alerts"] == 0
    assert service.snapshot()["risk"]["latched"]
    with pytest.raises(ValueError, match="低于 3%"):
        service.resume()
    service.account(account_input(990000, submission_id="fixture-002"))
    assert service.snapshot()["risk"]["latched"]
    assert not service.resume()["risk"]["latched"]


def period_input(account_id="fixture-001", request_id="period-001"):
    return RiskPeriodInput(submission_id=request_id, expected_account_submission_id=account_id,
                           confirm_account_complete=True)


def start_period(service):
    service.settings(WatchSettings(warning_drawdown_pct=12, restrict_drawdown_pct=16, max_drawdown_pct=20))
    service.account(account_input(750000))
    return service.start_risk_period(period_input())


def test_new_period_preserves_account_historical_loss_latch_and_old_alert(service):
    start_period(service)
    before = deepcopy(service.state["account"])
    view = service.snapshot()
    risk = view["risk"]
    assert risk["drawdown_pct"] == 0 and not risk["latched"]
    assert risk["historical_drawdown_pct"] == 25 and risk["historical_latched"]
    assert risk["historical_gap_to_peak"] == 250000
    assert risk["monitoring_period"]["start_equity"] == 750000
    assert risk["monitoring_period"]["history_at_start"]["drawdown_pct"] == 25
    assert risk["budget_to_limit"] == 150000
    assert risk["selection_allowed"]
    assert view["settings"]["max_single_position_pct"] == 20
    old = [a for a in view["alerts"] if a["key"] == "drawdown_critical"]
    assert old and old[0]["active"] is False
    repeated = service.start_risk_period(period_input())
    assert repeated["risk"]["monitoring_period"] == risk["monitoring_period"]
    assert service.state["account"] == before
    assert len([a for a in repeated["alerts"] if a["key"].startswith("risk_period_started_")]) == 1
    loaded = AshareService(service.root, feeds=service.feeds, clock=lambda: NOW)
    assert loaded.snapshot()["risk"] == risk


@pytest.mark.parametrize("cash,level", [(660000, "warning"), (630000, "restrict"), (600000, "critical")])
def test_period_thresholds_use_new_peak_and_keep_historical_peak(service, cash, level):
    start_period(service)
    view = service.account(account_input(cash, submission_id="account-after-period"))
    assert view["risk"]["level"] == level
    assert view["risk"]["historical_peak_equity_equivalent"] == 1000000
    assert view["risk"]["peak_equity_equivalent"] == 750000
    assert view["risk"]["selection_allowed"] is (level == "warning")


@pytest.mark.parametrize("flow", [100000, -100000])
def test_period_flows_and_retries_never_rebase_or_erase_loss(service, flow):
    start_period(service)
    service.account(account_input(660000, submission_id="loss-before-flow"))
    result = service.account(account_input(660000 + flow, external_flow=flow, submission_id="flow-after-period"))
    assert result["risk"]["drawdown_pct"] == pytest.approx(12)
    assert result["risk"]["historical_drawdown_pct"] == pytest.approx(34)
    assert result["risk"]["monitoring_period"]["start_equity"] == 750000
    units = service.state["ledger"]["units"]
    service.start_risk_period(period_input())
    assert service.state["ledger"]["units"] == units
    assert service.snapshot()["risk"]["drawdown_pct"] == pytest.approx(12)


def test_period_peak_rises_and_manual_resume_never_clears_history(service):
    start_period(service)
    service.account(account_input(800000, submission_id="new-stage-high"))
    assert service.snapshot()["risk"]["peak_equity_equivalent"] == 800000
    service.account(account_input(640000, submission_id="stage-breached"))
    assert service.snapshot()["risk"]["latched"]
    service.account(account_input(780000, submission_id="stage-recovered"))
    view = service.resume()
    assert not view["risk"]["latched"]
    assert view["risk"]["historical_latched"]
    assert view["risk"]["historical_drawdown_pct"] == pytest.approx(22)
    assert view["risk"]["drawdown_pct"] == pytest.approx(2.5)


def test_period_rejects_missing_stale_or_changed_account_and_missing_quotes(service):
    with pytest.raises(ValueError, match="尚未保存"):
        service.start_risk_period(period_input())
    service.account(account_input())
    with pytest.raises(ValueError, match="快照已变化"):
        service.start_risk_period(period_input("other-account"))
    service.state["account"]["confirmed_at"] = (NOW - timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="今天"):
        service.start_risk_period(period_input())
    service.state["account"]["confirmed_at"] = NOW.isoformat()
    service.state["account"]["positions"] = [dict(symbol="000001.SZ", quantity=100, available_quantity=0)]
    with pytest.raises(ValueError, match="估值不完整"):
        service.start_risk_period(period_input())
    assert "monitoring_period" not in service.state["ledger"]
    with pytest.raises(ValidationError):
        RiskPeriodInput(submission_id="period-001", expected_account_submission_id="fixture-001", confirm_account_complete=False)


def test_replacing_period_archives_old_and_old_request_cannot_reset_current(service):
    start_period(service)
    service.account(account_input(650000, submission_id="later-account"))
    view = service.start_risk_period(period_input("later-account", "period-002"))
    archive = service.state["risk_period_history"][0]
    assert archive["start_equity"] == 750000
    assert archive["end_equity"] == 650000
    assert archive["end_drawdown_pct"] == pytest.approx(100 * (1 - 650000 / 750000))
    assert view["risk"]["historical_drawdown_pct"] == 35
    assert service.start_risk_period(period_input())["risk"]["monitoring_period"]["id"] == "period-002"


def test_period_private_values_stay_out_of_model_and_relay(service):
    start_period(service)
    public = json.dumps(service.public_snapshot())
    relay = json.dumps(service.relay_snapshot())
    for private in ("start_equity", "start_nav", "history_at_start", "account_submission_id", "750000", "1000000"):
        assert private not in public and private not in relay
    assert "monitoring_period" in relay and "period_started_at" in relay


def test_period_values_real_holdings_and_preserves_manual_sellability(service):
    service.settings(WatchSettings(warning_drawdown_pct=12, restrict_drawdown_pct=16, max_drawdown_pct=20))
    holding = dict(symbol=SYMBOL, quantity=75000, available_quantity=20000, cost_price=12)
    service.account(account_input(0, positions=[holding]))
    view = service.start_risk_period(period_input())
    assert view["risk"]["monitoring_period"]["start_equity"] == 750000
    assert view["risk"]["monitoring_period"]["quote_as_of"][SYMBOL] == NOW.isoformat()
    service.state["quotes"][SYMBOL] = quote(price=8.8)
    service._evaluate(NOW)
    risk = service.snapshot()["risk"]
    assert risk["level"] == "warning"
    assert risk["drawdown_pct"] == pytest.approx(12)
    assert risk["historical_drawdown_pct"] == pytest.approx(34)
    assert risk["holdings"][0]["available_quantity"] == 20000
    assert risk["holdings"][0]["locked_quantity"] == 55000
    assert service.state["account"]["positions"] == [holding]
    restored = AshareService(service.root, feeds=Feeds(), clock=lambda: NOW)
    assert restored.state["ledger"]["peak_nav"] == service.state["ledger"]["peak_nav"]
    assert stat.S_IMODE(service.path.stat().st_mode) == 0o600


def test_service_deposit_idempotency_peak_cannot_be_reset(service):
    service.account(account_input(970000))
    deposit = account_input(1070000, external_flow=100000, submission_id="deposit-001")
    after = service.account(deposit)
    assert after["risk"]["drawdown_pct"] == pytest.approx(3)
    units = service.state["ledger"]["units"]
    service.account(deposit)
    assert service.state["ledger"]["units"] == units
    with pytest.raises(ValueError, match="不可重设"):
        service.account(account_input(historical_peak_equity=950000, submission_id="fixture-003"))
    with pytest.raises(ValueError, match="出入金与净值不一致"):
        service.account(account_input(100, external_flow=1000, submission_id="deposit-bad"))
    assert service.state["account"]["submission_id"] == "deposit-001"


def test_refresh_failure_clears_quotes_and_pool_size_refetches(service):
    service.settings(WatchSettings(pool_size=5))
    service.refresh()
    assert service.feeds.boards == 2
    service.test_clock[0] += timedelta(seconds=31)
    service.feeds.fail = True
    view = service.refresh()
    assert not view["quotes"] and "quotes" in view["errors"]
    assert not any(row["candidate"] for row in view["rows"])


def test_tool_snapshot_contains_no_account_cash_or_private_drawdown(service, monkeypatch):
    service.account(account_input(987654.32, positions=[dict(symbol=SYMBOL, quantity=123, available_quantity=23)]))
    public = service.public_snapshot()
    text = json.dumps(public)
    for private in ("987654.32", "available_quantity", "historical_peak_equity", "confirmed_at", "drawdown_pct", "cash"):
        assert private not in text
    import src.tools.ashare_intraday_tool as tool
    monkeypatch.setattr(tool, "get_ashare_service", lambda: service)
    assert json.loads(tool.AshareIntradayTool().execute()) == public


def test_paused_monitor_never_generates_candidates(service):
    view = service.settings(WatchSettings(enabled=False))
    assert not view["risk"]["selection_allowed"]
    assert not any(row["candidate"] for row in view["rows"])


def test_pending_flow_can_be_corrected_without_double_counting(service):
    service.account(account_input(970000))
    service.state["quotes"] = {}
    missing_position = [dict(symbol=SYMBOL, quantity=1000, available_quantity=0)]
    pending = service.account(account_input(1060000, positions=missing_position,
                                          external_flow=100000, submission_id="pending-001"))
    assert not pending["risk"]["complete"]
    with pytest.raises(ValueError, match="保留同一笔"):
        service.account(account_input(1070000, external_flow=0, submission_id="pending-002"))
    service.state["quotes"] = {SYMBOL: quote()}
    result = service.account(account_input(1060000, positions=missing_position,
                                          external_flow=100000, submission_id="pending-003"))
    assert result["risk"]["drawdown_pct"] == pytest.approx(3)
    assert service.state["ledger"]["pending_flow"] == 0


def test_analysis_uses_public_tool_without_account_json(service, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import src.api.ashare_routes as routes
    import src.api.state as api_state
    import src.api.security as security
    monkeypatch.setattr(routes, "get_ashare_service", lambda: service)
    monkeypatch.setattr(security, "_configured_api_key", lambda: "fixture-api-key")
    session_service = SimpleNamespace(create_session=Mock(return_value=SimpleNamespace(session_id="public-test")), send_message=AsyncMock())
    monkeypatch.setattr(api_state, "_get_session_service", lambda: session_service)
    app = FastAPI()
    routes.register_ashare_routes(app)
    client = TestClient(app, client=("127.0.0.1", 50000))
    response = client.post("/api/ashare/analysis", headers={"Authorization": "Bearer fixture-api-key"})
    assert response.status_code == 200
    arguments = session_service.send_message.call_args.kwargs
    assert "get_a_share_intraday" in arguments["content"]
    assert len(arguments["content"]) < 1500
    assert "historical_peak_equity" not in arguments["content"]
    assert arguments["include_shell_tools"] is False
    service.state["mainboard"] = {"enabled": True, "scan": {"session_date": NOW.date().isoformat(), "rows": [quote()]}}
    response = client.post("/api/ashare/analysis?scope=mainboard", headers={"Authorization": "Bearer fixture-api-key"})
    assert response.status_code == 200
    assert "mainboard中的沪深主板候选" in session_service.send_message.call_args.kwargs["content"]
    assert "entry_ready始终为false" in session_service.send_message.call_args.kwargs["content"]


def test_api_auth_validation_and_no_order_routes(service, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import src.api.ashare_routes as routes
    import src.api.security as security
    monkeypatch.setattr(routes, "get_ashare_service", lambda: service)
    monkeypatch.setattr(security, "_configured_api_key", lambda: "fixture-api-key")
    app = FastAPI()
    routes.register_ashare_routes(app)
    client = TestClient(app, client=("127.0.0.1", 50000))
    assert client.get("/api/ashare").status_code == 401
    assert client.post("/api/ashare/mainboard/scan").status_code == 401
    assert client.put("/api/ashare/news-monitoring", json={"enabled": True}).status_code == 401
    period_body = period_input().model_dump()
    assert client.post("/api/ashare/risk-period", json=period_body).status_code == 401
    headers = {"Authorization": "Bearer fixture-api-key"}
    assert client.get("/api/ashare", headers=headers).status_code == 200
    assert client.put("/api/ashare/news-monitoring", json={"enabled": True}, headers=headers | {"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.put("/api/ashare/news-monitoring", json={"enabled": True}, headers=headers).status_code == 200
    assert service.state["settings"]["recurring_news_enabled"]
    assert client.post("/api/ashare/risk-period", json=period_body, headers=headers).status_code == 409
    assert client.post("/api/ashare/risk-period", json=period_body, headers=headers | {"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    service.account(account_input())
    assert client.post("/api/ashare/risk-period", json=period_body, headers=headers).status_code == 200
    assert client.post("/api/ashare/mainboard/scan", headers=headers | {"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    bad = account_input().model_dump(mode="json")
    bad["positions"] = [dict(symbol=SYMBOL, quantity=100, available_quantity=101)]
    assert client.put("/api/ashare/account", json=bad, headers=headers).status_code == 422
    service.state["account"] = None
    assert client.post("/api/ashare/resume", headers=headers).status_code == 409
    assert client.put("/api/ashare/settings", json=WatchSettings().model_dump(), headers=headers | {"Origin": "https://untrusted.example", "Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert not any("order" in route.path for route in app.routes)
