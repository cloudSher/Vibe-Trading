"""All-main-board discovery, restrictive judgments and scoped read-only alerts."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

import pytest

from src.ashare.market_news import CompanyResolver, discovery_rows, market_news_view, validated_directory
from src.ashare.models import FocusPlan, NewsMonitoringInput
from src.ashare.news_feeds import article
from src.ashare.news import version
from src.ashare.relay import check_relay
from src.ashare.service import AshareService
from tests.test_ashare_monitor import NOW, SYMBOL, Feeds, account_input, quote
from tests.test_ashare_recurring import five_plan, write_source, alert
from tests.test_ashare_news import analyzed

STOCK = {"symbol": SYMBOL, "name": "测试股票"}
SETTINGS = {"enabled": True, "market_news_enabled": True, "recurring_news_enabled": True}
RISK = {"configured": True, "complete": True, "selection_allowed": True}


def directory(stocks=None):
    stocks = stocks if stocks is not None else [STOCK]
    return {"origin": "eastmoney_public_mainboard", "as_of": NOW.isoformat(), "total": len(stocks), "stocks": stocks}


def setup(now=NOW, impact="positive", title="测试股票签署新订单"):
    item = article("财联社", title, "测试股票公告签署新订单，履约条件尚需核实", now.isoformat(), "https://www.cls.cn/detail/123", now, [])
    item = CompanyResolver([STOCK]).attach(item)
    news = {"items": [item], "analyses": {item["id"]: analyzed(item, impact)},
            "sources": {"cls": {"checked_at": now.isoformat()}}}
    radar = {"directory": directory(), "reviews": {SYMBOL: {"checked_at": now.isoformat(), "announcements": [], "title_flags": []}}, "tracked": {}}
    return radar, news


@pytest.mark.parametrize("change", ["duplicate", "foreign", "partial", "stale", "future", "bad_origin"])
def test_directory_cannot_claim_partial_or_unverified_coverage(change):
    data = directory()
    if change == "duplicate": data["stocks"] *= 2; data["total"] = 2
    if change == "foreign": data["stocks"] = [{"symbol": "300001.SZ", "name": "创业板公司"}]
    if change == "partial": data["total"] += 1
    if change == "stale": data["as_of"] = (NOW - timedelta(days=1)).isoformat()
    if change == "future": data["as_of"] = (NOW + timedelta(seconds=1)).isoformat()
    if change == "bad_origin": data["origin"] = "model_generated"
    with pytest.raises(ValueError): validated_directory(data, NOW)


def test_resolver_does_not_guess_beneficiaries_codes_amounts_or_ambiguous_names():
    stocks = [STOCK, {"symbol": "000002.SZ", "name": "重复公司"}, {"symbol": "000003.SZ", "name": "重复公司"},
              {"symbol": "000004.SZ", "name": "*ST测试"}]
    resolver = CompanyResolver(stocks)
    def symbols(text):
        item = article("财联社", text, "行业背景", NOW.isoformat(), "https://www.cls.cn/detail/1", NOW, [])
        return {r["symbol"] for r in resolver.attach(item)["relations"]}
    assert symbols("测试股票签订订单") == {SYMBOL}
    assert symbols("证券代码：600000 公告") == {SYMBOL}
    assert symbols("600000.SH发布公告") == {SYMBOL}
    assert not symbols("订单金额600000万元；人形机器人产业利好；重复公司、*ST测试")
    assert not symbols("创业板公司300001.SZ获批")


def test_new_relations_invalidate_a_previous_model_version():
    item = article("财联社", "测试股票增长", "公开消息原文", NOW.isoformat(), "https://www.cls.cn/detail/1", NOW, [])
    assert version(item) != version(CompanyResolver([STOCK]).attach(item))


def test_original_five_analyses_keep_their_compatible_versions():
    item = article("财联社", "中鼎股份公告", "公开业务信息", NOW.isoformat(), "https://www.cls.cn/detail/1", NOW, five_plan()["stocks"])
    legacy = hashlib.sha256(("per-symbol-v2|" + item["title"] + "|" + item["content"]).encode()).hexdigest()[:16]
    assert version(item) == legacy


@pytest.mark.parametrize("title", ["21股获杠杆资金净买入超5000万元", "10月19日连板股分析：连板高度降至3板", "收评：沪指低开高走", "投资日历：资本市场大事", "30只股上午收盘涨停(附股)"])
def test_market_tables_are_not_company_catalysts(title):
    radar, news = setup(title=title)
    assert not discovery_rows(radar, news, NOW)


def test_radar_announcements_are_forwarded_with_original_links(tmp_path):
    radar, news = setup()
    notice = {"id": "AN-fixture", "date": "2026-10-19", "title": "控股股东减持计划", "source_url": "https://data.eastmoney.com/notices/detail/600000/AN-fixture.html"}
    radar["reviews"][SYMBOL].update(announcements=[notice], new_disclosure_ids=[notice["id"]], title_flags=[notice["title"]])
    view = market_news_view(radar, news, {SYMBOL: quote()}, RISK, SETTINGS, five_plan(), NOW)
    conditions = [c for c in view["conditions"] if c["key"].startswith("radar_disclosure_")]
    assert len(conditions) == 1 and conditions[0]["announcement"] == notice
    write_source(tmp_path, alerts=[alert(id="notice", **conditions[0])], market_news=view)
    result = check_relay(tmp_path, recurring=True, now=NOW)
    assert result["observations"][0]["announcements"][0]["source_url"] == notice["source_url"]


def test_radar_valid_price_can_realert_only_after_observed_invalidation(tmp_path):
    radar, news = setup()
    view = market_news_view(radar, news, {SYMBOL: quote()}, RISK, SETTINGS, five_plan(), NOW)
    key = "radar_price_" + SYMBOL
    source = write_source(tmp_path, alerts=[alert(key=key)], market_news=view)
    assert check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    source["alerts"] = [alert(key=key, id="duplicate")]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    source["market_news"]["rows"][0]["market_ready"] = False
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    source["market_news"]["rows"][0]["market_ready"] = True
    source["alerts"] = [alert(key=key, id="recovered")]
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert check_relay(tmp_path, recurring=True, now=NOW)["notify"]


def test_positive_outside_original_five_has_provenance_and_never_bypasses_review():
    radar, news = setup()
    result = market_news_view(radar, news, {SYMBOL: quote()}, RISK, SETTINGS, five_plan(), NOW)
    row = result["rows"][0]
    assert row["symbol"] == SYMBOL and row["market_ready"] and row["entry_ready"] is False
    assert "公告原文仍待核验" in row["status"]
    event = next(c for c in result["conditions"] if c.get("news_event"))["news_event"]
    assert event["analysis"]["impact"] == "positive" and event["relation"]["scope"] == "direct"
    assert event["url"] == "https://www.cls.cn/detail/123"


@pytest.mark.parametrize("case", ["old", "after_close", "stale_quote", "source_error", "negative", "pending", "announcement_risk", "account_incomplete", "account_restrict"])
def test_observation_gates_survive_positive_news(case):
    radar, news = setup()
    now, q, risk = NOW, quote(), dict(RISK)
    if case == "old": now += timedelta(minutes=31); q = quote(now)
    if case == "after_close": now = NOW.replace(hour=16); q = quote(now.replace(hour=15))
    if case == "stale_quote": q = quote(NOW - timedelta(seconds=91))
    if case == "source_error": news["sources"]["cls"]["error"] = "failure"
    if case == "negative": news["analyses"][news["items"][0]["id"]] = analyzed(news["items"][0], "negative"); radar["tracked"][SYMBOL] = NOW.isoformat()
    if case == "pending": news["analyses"] = {}
    if case == "announcement_risk": radar["reviews"][SYMBOL]["title_flags"] = ["股东减持计划"]
    if case == "account_incomplete": risk["complete"] = False
    if case == "account_restrict": risk["selection_allowed"] = False
    row = market_news_view(radar, news, {SYMBOL: q}, risk, SETTINGS, five_plan(), now)["rows"][0]
    assert row["entry_ready"] is False and row["reasons"]
    if case not in {"account_incomplete", "account_restrict", "announcement_risk"}:
        assert not row["market_ready"]


def test_risk_and_announcement_restrictions_are_not_labelled_full_confirmation():
    radar, news = setup()
    result = market_news_view(radar, news, {SYMBOL: quote()}, {**RISK, "selection_allowed": False}, SETTINGS, five_plan(), NOW)
    assert "账户" in "；".join(result["rows"][0]["reasons"])
    assert all(c["severity"] != "critical" for c in result["conditions"])


def test_sector_only_positive_does_not_create_a_stock_candidate():
    radar, news = setup()
    news["items"][0]["relations"][0]["scope"] = "sector"
    assert not discovery_rows(radar, news, NOW)


def test_new_company_reversal_remains_visible_and_warning(tmp_path):
    radar, news = setup()
    radar["tracked"][SYMBOL] = NOW.isoformat()
    news["analyses"][news["items"][0]["id"]] = analyzed(news["items"][0], "negative")
    result = market_news_view(radar, news, {SYMBOL: quote()}, RISK, SETTINGS, five_plan(), NOW)
    assert result["rows"][0]["entry_ready"] is False
    assert result["conditions"][0]["severity"] == "warning"


def test_relay_expansion_allows_only_valid_mainboard_candidates_and_dedupes(tmp_path):
    radar, news = setup()
    view = market_news_view(radar, news, {SYMBOL: quote()}, RISK, SETTINGS, five_plan(), NOW)
    event = news["items"][0]
    source = write_source(tmp_path, alerts=[alert(key=c["key"], id=str(i), severity=c["severity"], message=c["message"], **({"news_event": c["news_event"]} if c.get("news_event") else {})) for i, c in enumerate(view["conditions"])], market_news=view)
    result = check_relay(tmp_path, recurring=True, now=NOW)
    assert result["notify"] and any(o["symbol"] == SYMBOL for o in result["observations"])
    row = next(o for o in result["observations"] if o["symbol"] == SYMBOL)
    assert row["entry_ready"] is False and row["news"]["events"][0]["url"] == event["url"]
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]
    source["alerts"] = [alert(key="radar_news_300001.SZ_forged", id="foreign")]
    source["market_news"]["rows"].append({"symbol": "300001.SZ"})
    (tmp_path / "relay.json").write_text(json.dumps(source))
    assert not check_relay(tmp_path, recurring=True, now=NOW)["notify"]


def test_scope_toggle_and_public_model_input_preserve_account_and_focus(tmp_path):
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: NOW)
    svc.set_focus(FocusPlan.model_validate(five_plan()))
    svc.account(account_input())
    before = deepcopy(svc.state)
    svc.set_news_monitoring(NewsMonitoringInput(enabled=True, market_wide=True))
    for key in ("account", "focus", "ledger"):
        assert svc.state[key] == before[key]
    for key in ("warning_drawdown_pct", "restrict_drawdown_pct", "max_drawdown_pct"):
        assert svc.state["settings"][key] == before["settings"][key]
    for private in ('"cash"', '"positions"', '"historical_peak_equity"'):
        assert private not in json.dumps(svc.public_snapshot())
        assert private not in json.dumps(svc.relay_snapshot())


def test_daily_directory_rematches_saved_public_news_and_reviews_at_most_five(tmp_path):
    class News:
        def eastmoney(self, *args): return []
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: NOW, news_feeds=News())
    svc.set_focus(FocusPlan.model_validate(five_plan()))
    svc.set_news_monitoring(NewsMonitoringInput(enabled=True, market_wide=True))
    (tmp_path / "mainboard-universe.json").write_text(json.dumps(directory()))
    item = article("财联社", "测试股票签署订单", "公开订单及其履约条件", NOW.isoformat(), "https://www.cls.cn/detail/1", NOW, [])
    svc.state["news"]["items"] = [item]
    svc.refresh_market_news()
    assert svc.state["news"]["items"][0]["relations"][0]["symbol"] == SYMBOL
    assert len(svc.state["market_news"]["reviews"]) == 1
    assert svc.state["focus"]["stocks"] == five_plan()["stocks"]


@pytest.mark.parametrize("now", [NOW.replace(hour=22), NOW + timedelta(days=5)])
def test_radar_skips_outside_window_without_network(tmp_path, monkeypatch, now):
    svc = AshareService(tmp_path, feeds=Feeds(), clock=lambda: now)
    svc.set_focus(FocusPlan.model_validate(five_plan()))
    svc.set_news_monitoring(NewsMonitoringInput(enabled=True, market_wide=True))
    monkeypatch.setattr("src.ashare.service.scan_mainboard", lambda **kw: pytest.fail("network outside window"))
    svc.refresh_market_news()
    assert not svc.state["market_news"].get("directory_attempt_at")
