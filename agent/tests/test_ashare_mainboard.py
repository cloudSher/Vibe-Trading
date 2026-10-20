"""Coverage, board boundaries and fail-closed discovery; isolated account fixtures."""
from copy import deepcopy
from datetime import timedelta

import pytest
import requests

from src.ashare import mainboard
from src.ashare.service import AshareService
from tests.test_ashare_monitor import NOW, quote, account


def raw(code="600000", market=1, **changes):
    return {"f12": code, "f13": market, "f14": "测试公司", "f2": 10, "f3": 2.04,
            "f5": 200000, "f6": 199_000_000, "f8": 3, "f10": 2,
            "f15": 10.1, "f16": 9.8, "f17": 9.9, "f18": 9.8, "f124": NOW.timestamp()} | changes


@pytest.mark.parametrize("symbol,allowed", [("600000.SH", True), ("601100.SH", True), ("603000.SH", True),
    ("605000.SH", True), ("000001.SZ", True), ("001309.SZ", True), ("002475.SZ", True),
    ("003000.SZ", True), ("300001.SZ", False), ("301000.SZ", False), ("688001.SH", False),
    ("689009.SH", False), ("920001.BJ", False), ("600000.SZ", False)])
def test_main_board_only(symbol, allowed):
    assert bool(mainboard.MAINBOARD.fullmatch(symbol)) is allowed


@pytest.mark.parametrize("changes,reason", [({"f14": "*ST测试"}, "ST/退市"), ({"f14": "N测试"}, "ST/退市"),
    ({"f124": (NOW - timedelta(seconds=91)).timestamp()}, "时效"), ({"f124": None}, "时效"),
    ({"f6": 1e6}, "均价单位异常"), ({"f3": 7}, "涨幅"), ({"f8": 21}, "换手"),
    ({"f17": 10.2}, "未站上"), ({"f18": 9.5}, "高开"), ({"f15": 11}, "回落")])
def test_risky_or_incomplete_quote_rejected(changes, reason):
    reasons = mainboard.exclusions(mainboard.parse_row(raw(**changes), NOW), NOW)
    assert any(reason in r for r in reasons)


def test_units_and_source_stamp_are_independent_of_retrieval_time():
    q = mainboard.parse_row(raw(), NOW + timedelta(seconds=10))
    assert q["volume_shares"] == 20_000_000
    assert q["vwap"] == 9.95 and q["as_of"] == NOW.isoformat()
    assert q["as_of"] != q["received_at"]
    assert not mainboard.exclusions(q, NOW)


def install_pages(monkeypatch, pages):
    records = iter(pages)
    class Response:
        def json(self):
            return next(records)
    monkeypatch.setattr(mainboard, "_get", lambda *a, **kw: Response())


def test_complete_code_pagination_and_ranking(monkeypatch):
    rows = [raw(f"600{i:03d}") for i in range(100)]
    install_pages(monkeypatch, [{"rc": 0, "data": {"total": 101, "diff": rows}},
        {"rc": 0, "data": {"total": 101, "diff": [raw("600100")]}}])
    result = mainboard.scan_mainboard(clock=lambda: NOW)
    assert result["coverage_complete"] and result["received_total"] == 101
    assert result["qualifying_total"] == 101 and len(result["rows"]) == 20
    assert len(result["raw_rows"]) == 101


def test_known_public_endpoint_fallback_is_bounded(monkeypatch):
    urls = []
    class Response:
        def json(self):
            return {"rc": 0, "data": {"total": 1, "diff": [raw()]}}
    def get(url, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            raise requests.ConnectionError("source disconnected")
        return Response()
    monkeypatch.setattr(mainboard, "_get", get)
    result = mainboard.scan_mainboard(clock=lambda: NOW)
    assert len(urls) == 2 and result["coverage_complete"]
    assert result["request_hosts"] == ["82.push2.eastmoney.com"]


def test_exhausted_network_retries_fail_with_page_not_partial_success(monkeypatch):
    calls = []
    def get(*a, **kw):
        calls.append(1)
        raise requests.ConnectionError("source disconnected")
    monkeypatch.setattr(mainboard, "_get", get)
    with pytest.raises(ValueError, match="第1页网络读取失败"):
        mainboard.scan_mainboard(clock=lambda: NOW)
    assert len(calls) == 3


@pytest.mark.parametrize("failure", ["truncated", "duplicate", "changed_total"])
def test_partial_or_unstable_market_cannot_be_called_full_coverage(monkeypatch, failure):
    rows = [raw(f"600{i:03d}") for i in range(100)]
    second = {"rc": 0, "data": {"total": 101, "diff": [raw("600100")]}}
    if failure == "truncated":
        rows.pop()
    if failure == "duplicate":
        second["data"]["diff"] = [rows[0]]
    if failure == "changed_total":
        second["data"]["total"] = 102
    install_pages(monkeypatch, [{"rc": 0, "data": {"total": 101, "diff": rows}}, second])
    with pytest.raises(ValueError):
        mainboard.scan_mainboard(clock=lambda: NOW)


def board():
    return {"enabled": True, "session_date": NOW.date().isoformat(), "scan": {
        "completed_at": NOW.isoformat(), "session_date": NOW.date().isoformat(), "coverage_complete": True,
        "rows": [mainboard.parse_row(raw(), NOW)]}}


def test_stale_missing_and_lost_scan_cannot_use_a_cached_buy_signal():
    state = board()
    view = mainboard.mainboard_view(state, {"600000.SH": quote()}, NOW, enabled=True)
    assert view["valid"] and view["rows"][0]["market_ready"]
    assert view["rows"][0]["entry_ready"] is False
    assert view["rows"][0]["disclosure_status"] == "pending"
    assert not mainboard.mainboard_view(state, {}, NOW, enabled=True)["rows"][0]["market_ready"]
    assert not mainboard.mainboard_view(state, {"600000.SH": quote()}, NOW+timedelta(seconds=91), enabled=True)["rows"][0]["market_ready"]
    state["error"] = "failed"
    assert not mainboard.mainboard_view(state, {"600000.SH": quote()}, NOW, enabled=True)["rows"][0]["market_ready"]
    assert not mainboard.mainboard_view(board(), {"600000.SH": quote()}, NOW, enabled=False)["rows"][0]["market_ready"]


def test_scan_worker_preserves_account_and_focus_and_expires_at_date_boundary(tmp_path, monkeypatch):
    from src.ashare import service
    now = [NOW]
    monitor = AshareService(tmp_path, clock=lambda: now[0])
    monitor.state["account"] = account()
    monitor.state["ledger"] = {"breached": True, "peak_nav": 1.1}
    # Keep an opaque focus fixture: the scanner must never edit it.
    monitor.state["focus"] = None
    protected = deepcopy({k: monitor.state[k] for k in ("account", "ledger", "focus", "settings")})
    calls = []
    monkeypatch.setattr(service, "scan_mainboard", lambda **kw: calls.append(1) or deepcopy(board()["scan"]))
    monkeypatch.setattr(service, "review_candidates", lambda *a, **kw: None)
    monkeypatch.setattr(monitor, "_save", lambda: None)
    monitor.state["mainboard"].update(enabled=True, session_date=NOW.date().isoformat(), requested=True)
    monitor.refresh_mainboard()
    assert {k: monitor.state[k] for k in protected} == protected
    assert len(calls) == 1 and not monitor.state["mainboard"]["running"]
    monitor.refresh_mainboard()
    assert len(calls) == 1  # No repeated scan before interval.
    now[0] += timedelta(days=1)
    monitor.refresh_mainboard()
    assert len(calls) == 1  # Never auto-renew into the next trading day.


def test_disclosure_title_flags_never_mark_original_reviewed():
    scan = {"rows": [mainboard.parse_row(raw(), NOW)]}
    class Feeds:
        def disclosures(self, *args):
            return {"error": None, "items": [{"id": "x", "symbols": ["600000.SH"], "date": "2026-10-18",
                "title": "关于股东减持计划的公告", "source_url": "https://data.eastmoney.com/example"}]}
    class News:
        def eastmoney(self, *args):
            return []
    mainboard.review_candidates(scan, feeds=Feeds(), news_feeds=News(), clock=lambda: NOW)
    review = scan["rows"][0]["review"]
    assert "减持" in review["title_flags"][0]
    assert review["status"] == "pending_original_review"


def test_same_day_universe_fallback_uses_new_quotes_and_counts_missing(tmp_path):
    scan = board()["scan"] | {"universe_total": 1, "raw_rows": [mainboard.parse_row(raw(), NOW)]}
    path = tmp_path / "universe.json"
    mainboard.cache_universe(scan, path)
    class Feeds:
        def quotes(self, symbols, now):
            assert symbols == ["600000.SH"]
            return {"600000.SH": quote(now=now)}
    result = mainboard.scan_cached_universe(path, feeds=Feeds(), clock=lambda: NOW + timedelta(minutes=5))
    assert result["universe_as_of"] == NOW.isoformat()
    assert result["rows"][0]["as_of"] == (NOW + timedelta(minutes=5)).isoformat()
    assert result["source"] == "tencent_quotes_cached_mainboard_universe"
    assert result["quote_received_total"] == 1 and result["missing_quote_total"] == 0
    with pytest.raises(ValueError, match="同日"):
        mainboard.scan_cached_universe(path, feeds=Feeds(), clock=lambda: NOW + timedelta(days=1))


def test_cached_universe_rejects_duplicate_codes_and_wrong_board(tmp_path):
    import json
    path = tmp_path / "universe.json"
    for symbols in [["600000.SH", "600000.SH"], ["300001.SZ"]]:
        path.write_text(json.dumps({"origin": "eastmoney_public_mainboard", "as_of": NOW.isoformat(),
            "total": len(symbols), "stocks": [{"symbol": s, "name": "测试"} for s in symbols]}))
        with pytest.raises(ValueError, match="同日完整"):
            mainboard.scan_cached_universe(path, feeds=None, clock=lambda: NOW)
