"""News provenance, restrictive joint decisions, concurrency and notification tests."""
from copy import deepcopy
from datetime import timedelta
import json
import threading

import pytest

from src.ashare.calendar import market_clock
from src.ashare.focus import focus_view
from src.ashare.models import FocusPlan
from src.ashare.news import merge_items, news_view, version
from src.ashare.news_analysis import validate_analyses
from src.ashare.news_feeds import article, NewsFeeds
from src.ashare.service import AshareService
from tests.test_ashare_focus import plan, history, write_relay
from tests.test_ashare_monitor import NOW, SYMBOL, Feeds, quote, account_input
from src.ashare.relay import check_relay


def item(title="测试股票签署服务订单", now=NOW, stocks=None, content="订单金额及履约条件尚需阅读原文核实"):
    return article("财联社", title, content, now.isoformat(), "https://www.cls.cn/detail/123", NOW,
                   stocks or plan()["stocks"])


def analyzed(event, impact="positive"):
    return {"id": event["id"], "impact": impact, "summary": "仅归纳所给报道，尚需独立核验",
            "evidence": event["content"][:12], "version": version(event),
            "symbol_impacts": {r["symbol"]: impact for r in event["relations"]}}


def state(events=None, impact="positive"):
    events = events if events is not None else [item()]
    return {"sources": {k: {"checked_at": NOW.isoformat(), "error": None}
                        for k in ["cls", "eastmoney_" + SYMBOL]},
            "items": events, "analyses": {i["id"]: analyzed(i, impact) for i in events}}


def joint(news, *, risk=None, p=None, now=NOW):
    p = p or plan(news_enabled=True)
    return focus_view(p, {"checked_at": now.isoformat(), "items": []}, {SYMBOL: quote(now, price=10.02)},
                      history(now), risk or {"configured": True, "selection_allowed": True}, market_clock(now), now,
                      news=news_view(news, p, now))


def test_clean_html_direct_match_and_sector_never_becomes_company_order():
    stocks = [{"symbol": "000887.SZ", "name": "中鼎股份"}]
    direct = item("<em>中鼎股份</em>公告", stocks=stocks)
    assert "<em>" not in direct["title"] and direct["relations"][0]["scope"] == "direct"
    sector = item("人形机器人行业动态", stocks=stocks)
    assert sector["relations"][0]["scope"] == "sector"
    unrelated = item("代码10008870的其他公司", stocks=stocks, content="无关联内容")
    assert not unrelated["relations"]


@pytest.mark.parametrize("case", ["future", "no_date", "bad_url"])
def test_invalid_news_is_not_accepted_as_fresh(case):
    with pytest.raises(ValueError):
        article("source", "有效新闻标题", "有效正文", (NOW + timedelta(seconds=6)).isoformat() if case == "future" else "" if case == "no_date" else NOW.isoformat(),
                "javascript:alert(1)" if case == "bad_url" else "https://www.cls.cn/detail/1", NOW, plan()["stocks"])


def test_reposts_keep_first_seen_and_merge_sources_without_creating_new_event():
    event = item()
    copied = item(now=NOW - timedelta(minutes=1))
    copied.update(source="东方财富/转载", url="https://finance.eastmoney.com/a/example.html",
                  first_seen_at=(NOW + timedelta(minutes=1)).isoformat())
    merged = merge_items([event], [copied], NOW + timedelta(minutes=1))
    assert len(merged) == 1 and merged[0]["first_seen_at"] == event["first_seen_at"]
    assert len(merged[0]["source_urls"]) == 2


@pytest.mark.parametrize("restriction", ["missing_source", "source_error", "stale", "pending", "negative", "uncertain", "risk_words", "model_revision"])
def test_news_restrictions_cannot_be_overridden_by_good_price_action(restriction):
    news = state()
    event = news["items"][0]
    if restriction == "missing_source": news["sources"].pop("cls")
    elif restriction == "source_error": news["sources"]["cls"]["error"] = "failed"
    elif restriction == "stale": news["sources"]["cls"]["checked_at"] = (NOW - timedelta(seconds=181)).isoformat()
    elif restriction == "pending": news["analyses"] = {}
    elif restriction in {"negative", "uncertain"}: news["analyses"][event["id"]]["symbol_impacts"][SYMBOL] = restriction
    elif restriction == "risk_words":
        event["content"] = "公司发布减持计划"; news["analyses"][event["id"]] = analyzed(event)
    elif restriction == "model_revision": event["content"] += "更正：该订单并未签署"
    row = joint(news)["rows"][0]
    assert not row["entry_ready"]
    assert row["news"]["blocked"]


def test_positive_news_does_not_clear_announcement_or_account_limits():
    assert joint(state())["rows"][0]["entry_ready"]
    p = plan(news_enabled=True); p["stocks"][0]["disclosure_status"] = "blocked"
    assert not joint(state(), p=p)["rows"][0]["entry_ready"]
    for risk in [{"configured": False, "selection_allowed": False}, {"configured": True, "selection_allowed": False}]:
        assert not joint(state(), risk=risk)["rows"][0]["entry_ready"]


def test_old_news_not_fresh_catalyst_or_relabelled_as_todays_new_alert():
    event = item(now=NOW - timedelta(days=1))
    view = news_view(state([event]), plan(news_enabled=True), NOW)
    assert not view["stocks"][SYMBOL]["has_fresh_catalyst"]
    assert view["stocks"][SYMBOL]["events"][0]["reference_only"]
    assert not any(c["key"].startswith("focus_news_" + SYMBOL + "_") for c in view["conditions"])


def test_no_match_is_not_reported_as_a_verified_absence_of_risk():
    result = news_view(state([]), plan(news_enabled=True), NOW)
    assert result["stocks"][SYMBOL]["healthy"]
    assert not result["stocks"][SYMBOL]["events"]
    assert not result["stocks"][SYMBOL]["has_fresh_catalyst"]


def test_mechanical_market_table_is_reference_not_unresolved_company_event():
    event = item("营业部最青睐个股曝光 5股净买入超亿元")
    news = state([event], "uncertain")
    assert joint(news)["rows"][0]["entry_ready"]
    assert not news_view(news, plan(news_enabled=True), NOW)["stocks"][SYMBOL]["has_fresh_catalyst"]


def test_other_company_risk_word_far_from_focus_mention_does_not_block_focus():
    event = item("行业综合报道", content="测试股票发布了常规产品信息。" + "行业数据参考。" * 40 + "其他公司被立案调查")
    assert not news_view(state([event]), plan(news_enabled=True), NOW)["stocks"][SYMBOL]["blocked"]


@pytest.mark.parametrize("error", ["unknown_id", "invented_evidence", "extra_action", "missing_event", "duplicate"])
def test_model_output_requires_ids_and_verbatim_grounding(error):
    event = item(); output = analyzed(event); output.pop("version")
    if error == "unknown_id": output["id"] = "invented"
    elif error == "invented_evidence": output["evidence"] = "公司明日一定上涨"
    elif error == "extra_action": output["buy_quantity"] = 100
    values = [] if error == "missing_event" else [output, output] if error == "duplicate" else [output]
    with pytest.raises(ValueError):
        validate_analyses(json.dumps({"events": values}), [event])


def test_multi_company_article_has_separate_directions_and_rejects_unmapped_stock():
    event = item(content="测试股票收入下滑，中鼎股份收入增长",
                 stocks=[*plan()["stocks"], {"symbol": "000887.SZ", "name": "中鼎股份"}])
    output = analyzed(event); output.pop("version")
    output["symbol_impacts"] = {SYMBOL: "negative", "000887.SZ": "positive"}
    values = validate_analyses(json.dumps({"events": [output]}), [event])
    news = state([event]); news["analyses"][event["id"]] = values[event["id"]] | {"version": version(event)}
    assert not joint(news)["rows"][0]["entry_ready"]
    output["symbol_impacts"] = {"000887.SZ": "positive"}
    with pytest.raises(ValueError, match="逐股"):
        validate_analyses(json.dumps({"events": [output]}), [event])


def test_sector_only_news_cannot_become_a_confirmed_company_catalyst():
    event = item("人形机器人行业动态", stocks=[{"symbol": "000887.SZ", "name": "中鼎股份"}])
    output = analyzed(event); output.pop("version")
    with pytest.raises(ValueError, match="只有行业关联"):
        validate_analyses(json.dumps({"events": [output]}), [event])


def test_empty_em_list_differs_from_invalid_structure_and_cls_empty(monkeypatch):
    class Response:
        text = '{"result":{"cmsArticleWebOld":[]}}'
        def json(self): return {"data": {"roll_data": []}}
    from src.ashare import news_feeds
    monkeypatch.setattr(news_feeds, "_get", lambda *args, **kwargs: Response())
    feeds = NewsFeeds()
    assert feeds.eastmoney(plan()["stocks"][0], plan()["stocks"], NOW) == []
    with pytest.raises(ValueError, match="空列表"):
        feeds.cls(plan()["stocks"], NOW)


class News:
    def cls(self, stocks, now, latest=None): return [item()], NOW.timestamp()
    def eastmoney(self, stock, stocks, now): return [item()]


def test_model_wait_does_not_block_quote_refresh_and_restart_keeps_news(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def analyzer(events):
        entered.set()
        assert release.wait(3)
        return {i["id"]: analyzed(i) for i in events}
    svc = AshareService(tmp_path / "watch", feeds=Feeds(), clock=lambda: NOW, news_feeds=News(), news_analyzer=analyzer)
    svc.set_focus(FocusPlan.model_validate(plan(news_enabled=True)))
    svc.refresh_news()
    worker = threading.Thread(target=svc.analyze_pending_news)
    worker.start()
    try:
        assert entered.wait(1)
        assert svc.refresh()["quotes"]
        assert svc.snapshot()["news"]["analysis_running"]
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    restored = AshareService(svc.root, feeds=Feeds(), clock=lambda: NOW, news_feeds=News())
    assert restored.snapshot()["news"]["event_count"] == 1
    assert restored.snapshot()["news"]["pending_count"] == 0


def test_new_negative_news_invalidates_existing_observation_once(tmp_path):
    svc = AshareService(tmp_path / "watch", feeds=Feeds(), clock=lambda: NOW, news_feeds=News())
    svc.set_focus(FocusPlan.model_validate(plan(news_enabled=True)))
    svc.account(account_input())
    svc.state["news"] = state()
    svc.state["quotes"] = {SYMBOL: quote(NOW, price=10.02)}
    svc.state["history"] = history()
    svc.state["disclosures"] = {"checked_at": NOW.isoformat(), "items": []}
    svc._evaluate(NOW)
    assert "focus_ready_" + SYMBOL in svc.state["active_conditions"]
    event = item("测试股票被立案调查")
    svc.state["news"] = state([event], "negative")
    svc._evaluate(NOW + timedelta(seconds=30))
    svc._evaluate(NOW + timedelta(seconds=60))
    assert len([a for a in svc.state["alerts"] if a["key"] == "focus_invalid_" + SYMBOL]) == 1
    assert not svc.snapshot()["focus"]["rows"][0]["entry_ready"]


def test_relay_includes_news_and_conditions_without_account_detail(tmp_path):
    source = write_relay(tmp_path)
    row = source["focus"]["rows"][0]
    row.update(name="测试股票", news=news_view(state(), plan(news_enabled=True), NOW)["stocks"][SYMBOL],
               observation_conditions=["等待有效报价"], invalidation_conditions=["新闻未核验时失效"])
    source["alerts"][0]["key"] = "focus_news_" + SYMBOL
    (tmp_path / "relay.json").write_text(json.dumps(source))
    report = check_relay(tmp_path, "2026-10-19", now=NOW)
    assert report["observations"][0]["news"]["events"][0]["url"].startswith("https://")
    assert report["observations"][0]["invalidation_conditions"] == ["新闻未核验时失效"]
    assert not check_relay(tmp_path, "2026-10-19", now=NOW)["notify"]
