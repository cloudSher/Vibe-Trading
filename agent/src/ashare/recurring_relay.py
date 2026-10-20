"""Read only the reduced relay; write notification cursors, never account state."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime

from .calendar import SHANGHAI, is_session, now_shanghai
from .mainboard import MAINBOARD
from .plan import authorized_symbols

SYMBOLS = frozenset({"000887.SZ", "600699.SH", "001696.SZ", "600319.SH", "002531.SZ"})
HEALTH = {"news": "新闻来源检查", "disclosures": "公告来源检查", "quotes": "行情来源检查"}


def _signature(alert):
    event = alert.get("news_event")
    if event:
        # A provider may edit a headline without changing the underlying report.
        body = re.sub(r"^【[^】]*】", "", event.get("snippet", ""))
        body = re.sub(r"\W", "", body)[:180]
        value = [alert["key"][11:20], event["url"], event["published_at"], body,
                 (event.get("analysis") or {}).get("impact", "pending")]
    else:
        value = [alert["key"], alert["severity"], alert["message"], alert.get("risk_period_id")]
        if alert["key"].startswith(("radar_price_", "radar_invalid_")):
            value.append(alert["created_at"][:10])
    if alert["key"].startswith("focus_"):
        value.append(alert.get("plan_identity"))
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _scope(alert, radar_symbols=frozenset(), focus_symbols=SYMBOLS):
    key = alert["key"]
    if key.startswith(('paper_candidate_','paper_switch_','paper_fill_')):
        return alert.get('simulation') is True
    if key.startswith("radar_"):
        return any(symbol in key for symbol in radar_symbols)
    if any(symbol in key for symbol in focus_symbols):
        return True
    return key in {"calendar", "account_data", "focus_disclosure_feed", "focus_news_feed", "focus_account_missing"} or key.startswith(("drawdown_", "provider_", "risk_period_started_"))


def _current_price_condition(key, row, source, now):
    quote = (row or {}).get("quote") or {}
    try:
        age = (now - datetime.fromisoformat(quote["as_of"])).total_seconds()
    except (ValueError, KeyError, TypeError):
        return None
    if not source.get("market", {}).get("live_window") or not 0 <= age <= 90:
        return None
    if key.startswith("radar_price_"):
        return bool(row.get("market_ready"))
    if key.startswith("pullback_"):
        high = quote.get("high")
        return quote["price"] / high <= .97 if high else None
    if key.startswith("focus_break_"):
        if source["focus"].get("session_date") != now.date().isoformat():
            return None
        if source["focus"].get("plan_id"):
            return bool((row or {}).get("price_evidence", {}).get("invalidated"))
        reference = (row or {}).get("reference_vwap")
        return quote["price"] < reference if reference else None
    return None


def _observations(rows, alerts, source, now):
    result = []
    current_plan = source["focus"].get("session_date") == now.date().isoformat()
    for symbol, row in rows.items():
        relevant = [a for a in alerts if symbol in a["key"]]
        if not relevant:
            continue
        news = row.get("news") or {}
        matched = [a["news_event"] for a in relevant if a.get("news_event")]
        events = list({e["id"]: e for e in [*matched, *news.get("events", [])]}.values())[:4]
        announcements = [a for a in row.get("announcements", []) if any(a["id"] in alert["key"] or a["id"] in alert.get("announcement_ids", []) for alert in relevant)][:5]
        result.append({k: row.get(k) for k in ("symbol", "name", "reasons", "quality")} | {
            "plan_identity": {k: source["focus"].get(k) for k in ("plan_id", "version", "content_hash")},
            "risk_status": row.get("risk_status"), "risk_blockers": row.get("risk_blockers", []),
            "price_evidence": row.get("price_evidence"), "reason": row.get("reason"),
            "research": row.get("research"),
            "risk_points": row.get("risk_points", []), "source_urls": row.get("source_urls", []),
            "trading_plan_current": current_plan,
            "entry_ready": bool(current_plan and source.get("market", {}).get("continuous") and row.get("entry_ready")),
            "status": row.get("status") if current_plan else "今日交易预案待核验；仅做资讯与行情评估",
            "observation_conditions": row.get("observation_conditions", []) if current_plan else [
                "今日交易预案、上日参考位及公告审阅范围需重新核验，不能沿用旧日期买点",
                "仅连续竞价且报价有效、新闻公告核验和本地账户风控均通过时才可评估买入观察"],
            "invalidation_conditions": row.get("invalidation_conditions", []) if current_plan else [
                "旧日期交易预案已到期，当前不能据此作买入确认", "公告、新闻与账户限制继续有效"],
            "quote": {k: row["quote"].get(k) for k in ("price", "vwap", "open", "high", "as_of")} if row.get("quote") else None,
            "news": {**news, "events": events}, "announcements": announcements})
    for row in source.get("market_news", {}).get("rows", []):
        relevant = [a for a in alerts if a["key"].startswith("radar_") and row["symbol"] in a["key"]]
        if not relevant or not MAINBOARD.fullmatch(row["symbol"]):
            continue
        matched = [a["news_event"] for a in relevant if a.get("news_event")]
        public = {k: row.get(k) for k in ("symbol", "name", "quality", "quote", "status", "reasons", "announcements", "observation_conditions", "invalidation_conditions", "review")}
        public.update(scope="sh_sz_mainboard", entry_ready=False, trading_plan_current=False,
                      news={"events": list({e["id"]: e for e in [*matched, *row.get("news", {}).get("events", [])]}.values())[:4]})
        public["announcements"] = list({a["id"]: a for a in [*[a["announcement"] for a in relevant if a.get("announcement")], *row.get("announcements", [])]}.values())[:5]
        result.append(public)
    return result


def check_recurring_relay(root, *, now=None, expected_plan=None):
    now = (now or now_shanghai()).astimezone(SHANGHAI)
    day, minute = now.date().isoformat(), now.hour * 60 + now.minute
    session = is_session(now.date())
    if not 540 <= minute <= 1260 or session is False:
        return {"notify": False, "status": "outside_requested_window" if session is not False else "non_trading_day", "session_date": day}
    path = root / "codex-relay-cursor.json"
    try:
        cursor = json.loads(path.read_text()) if path.exists() else {"seen": [], "issue": None}
    except (ValueError, OSError):
        cursor = {"seen": [], "issue": None}
    source, issue, focus_symbols = None, None, SYMBOLS
    if session is None:
        issue = "本年度交易日历尚未核验，已暂停交易日资讯提醒；请更新并核验交易所日历"
    else:
        try:
            source = json.loads((root / "relay.json").read_text())
            monitor = source.get("news_monitoring", {})
            if monitor.get("recurring") is False or monitor.get("enabled") is False:
                return {"notify": False, "status": "paused", "session_date": day}
            age = (now - datetime.fromisoformat(source["generated_at"])).total_seconds()
            if not 0 <= age <= 180 or not source["monitor_running"]:
                issue = "本地资讯监控快照已过期或后台未运行，无法确认最新资讯与风险；请打开本地平台核对"
            elif not monitor.get("recurring") or monitor.get("session_date") != day:
                issue = "后台尚未启用当日持续资讯模式，请检查本地服务与监控设置"
            elif source["focus"].get("plan_id") or source["focus"].get("manifest"):
                try:
                    focus_symbols = authorized_symbols(source["focus"])
                except ValueError:
                    issue = "本地发布计划的编号、版本、名单或内容校验失败，暂停转发；请核对观察计划"
            elif {r["symbol"] for r in source["focus"].get("rows", [])} != SYMBOLS:
                issue = "本地重点池与已授权五股不一致，暂停转发；请核对重点池范围"
            if expected_plan and any(source.get("focus", {}).get(k) != v for k, v in expected_plan.items()):
                issue = "本地观察计划与Codex提醒授权的编号、版本或内容不一致，暂停转发；请同步计划授权"
            if isinstance(source.get("server_pid"), int) and source["server_pid"] > 0:
                try:
                    os.kill(source["server_pid"], 0)
                except ProcessLookupError:
                    issue = "本地服务进程已停止，无法继续采集与提醒；请启动平台"
                except PermissionError:
                    pass
        except (OSError, ValueError, KeyError, TypeError):
            issue = "无法读取本地资讯提醒快照，当前提醒链路不可用"
    result = {"notify": False, "status": "unchanged", "session_date": day, "checked_at": now.isoformat(), "alerts": []}
    if issue:
        result.update(notify=issue != cursor.get("issue"), status="monitor_unavailable", message=issue)
    else:
        rows = {r["symbol"]: r for r in source["focus"]["rows"]}
        identity = {k: source["focus"][k] for k in ("plan_id", "version", "content_hash")} if source["focus"].get("plan_id") else None
        result["plan_identity"] = identity
        radar = source.get("market_news", {})
        radar_symbols = {r["symbol"] for r in radar.get("rows", []) if MAINBOARD.fullmatch(r["symbol"])} if radar.get("enabled") and radar.get("scope") == "sh_sz_mainboard" and radar.get("directory_valid") else set()
        price_rows = rows | {r["symbol"]: r for r in radar.get("rows", []) if r["symbol"] in radar_symbols}
        seen = set(cursor.get("seen", []))
        delivered_keys = {a["key"] for a in source.get("alerts", []) if a["id"] in seen}
        signatures = cursor.get("signatures", [])
        # Seed the new semantic cursor from previously delivered legacy records.
        if "signatures" not in cursor:
            signatures = [_signature(a) for a in source.get("alerts", []) if a["id"] in seen]
        known = set(signatures)
        condition_signatures = cursor.get("condition_signatures", {})
        for key in list(condition_signatures):
            row = next((r for s, r in price_rows.items() if s in key), None)
            if _current_price_condition(key, row, source, now) is False:
                known.discard(condition_signatures.pop(key))
        alerts, considered = [], []
        # Keep only the newest record of an unchanged condition within a batch.
        batch = {}
        for original in source.get("alerts", []):
            if original["id"] in seen or original["created_at"][:10] != day or original.get("acknowledged_at") or not _scope(original, radar_symbols, focus_symbols):
                continue
            if identity and original["key"].startswith("focus_") and original.get("plan_identity") != identity:
                continue
            if identity and original['key'].startswith('paper_') and original.get('plan_identity')!=identity:
                continue
            considered.append(original["id"])
            group = (original["key"].split("_")[2], original["news_event"]["url"], original["news_event"]["published_at"]) if original.get("news_event") else original["key"]
            prior = batch.get(group)
            if prior is None or original["created_at"] > prior["created_at"]:
                batch[group] = original
        for original in batch.values():
            alert = dict(original)
            key = alert["key"]
            if key.startswith('paper_') and alert.get('active') is False:
                continue
            if key.startswith(("radar_price_", "radar_invalid_", "focus_invalid_", "focus_disclosure_", "focus_bars_")) and alert.get("active") is False:
                continue
            if key.startswith("radar_price_"):
                radar_row = next((r for r in radar.get("rows", []) if r["symbol"] in key), None)
                if _current_price_condition(key, radar_row, source, now) is not True:
                    continue
                condition_signatures[key] = _signature(alert)
            signature = _signature(alert)
            if alert.get("news_event") and alert.get("active") is False:
                continue
            if "signatures" not in cursor and key in delivered_keys:
                known.add(signature)
                signatures.append(signature)
                continue
            row = next((r for symbol, r in rows.items() if symbol in key), None)
            price_condition = _current_price_condition(key, row, source, now)
            if key.startswith(("pullback_", "focus_break_")):
                if price_condition is not True:
                    continue  # Ended/unknown price events do not become new current warnings.
                condition_signatures[key] = signature
            if key.startswith("focus_ready_"):
                stamp = datetime.fromisoformat((row or {}).get("quote", {}).get("as_of", "1970-01-01T00:00:00+00:00"))
                valid = row and source["focus"].get("session_date") == day and source.get("market", {}).get("continuous")
                valid = valid and row.get("market_ready") and 0 <= (now - stamp).total_seconds() <= 90
                valid = valid and (alert["severity"] == "info" or row.get("entry_ready"))
                if not valid:
                    continue
            if key.startswith("drawdown_") and alert.get("active") is False:
                continue
            if key in {"focus_news_feed", "focus_disclosure_feed"}:
                continue  # Current health below handles failure/recovery, not a stale alert.
            if signature in known:
                continue
            known.add(signature)
            signatures.append(signature)
            alerts.append(alert)
        health = dict(source.get("health", {}))
        health_labels = dict(HEALTH)
        if "focus_m5" in health:
            health_labels["focus_m5"] = "计划5分钟K线检查"
        if 'paper_simulation' in health:
            health_labels['paper_simulation']='本地模拟计算与保存'
        if radar.get("enabled"):
            health["market_news"] = bool(radar.get("directory_valid") and radar.get("source_fresh"))
            health_labels["market_news"] = "主板资讯名录与快讯发现"
            health["market_news_model"] = False if radar.get("analysis_error") else True if radar.get("analysis_at") else None
            health_labels["market_news_model"] = "主板资讯模型整理"
            for row in radar.get("rows", []):
                if row.get("review", {}).get("checked_at") and row["symbol"] in radar_symbols:
                    name = "radar_review_" + row["symbol"]
                    health[name] = not bool(row["review"].get("error"))
                    health_labels[name] = row["name"] + " 个股新闻与公告索引"
        prior_health = cursor.get("health", {})
        for name, label in health_labels.items():
            value = health.get(name)
            if value is False and prior_health.get(name) is not False or value is True and prior_health.get(name) is False:
                alerts.append({"id": "health_" + name + "_" + now.isoformat(), "key": "health_" + name,
                    "severity": "warning" if value is False else "info", "created_at": now.isoformat(),
                    "message": label + ("失败或过期，暂停相关观察确认" if value is False else "已恢复；仍需核对公告、新闻及账户限制"),
                    "active": value is False})
        current_plan = source["focus"].get("session_date") == day
        if not current_plan and cursor.get("plan_notice_date") != day:
            alerts.append({"id": "plan_expired_" + day, "key": "plan_expired", "severity": "warning",
                "created_at": now.isoformat(), "message": "今日交易预案尚未核验：资讯继续跟踪，旧日期参考位及买入观察条件已到期，不能沿用为今日买点"})
            cursor["plan_notice_date"] = day
        alerts.sort(key=lambda a: (a["severity"] != "critical", not a["key"].startswith(("health_", "plan_expired")), a["created_at"]))
        delivered, pending = alerts[:12], alerts[12:]
        pending_ids = {a["id"] for a in pending}
        # Do not consume the tail of a large notification batch.
        for alert in pending:
            sig = _signature(alert)
            known.discard(sig)
        cursor["seen"] = list(dict.fromkeys([*cursor.get("seen", []), *[i for i in considered if i not in pending_ids]]))[-1500:]
        cursor["signatures"] = list(dict.fromkeys(s for s in [*signatures, *known] if s in known))[-2000:]
        cursor["condition_signatures"] = condition_signatures
        cursor["health"] = health
        recovered = bool(cursor.get("issue"))
        result.update(notify=bool(delivered) or recovered, status="new_alerts" if delivered else "recovered" if recovered else "unchanged", alerts=delivered)
        if recovered:
            result["message"] = "本地资讯提醒快照已恢复；恢复不表示交易条件或账户风控已通过"
        if result["notify"]:
            result.update(risk_policy=source.get("risk_policy", {}), monitoring=source["news_monitoring"],
                market=source.get("market", {}), observations=_observations(rows, delivered, source, now),
                paper_rotation=source.get('paper_rotation'),
                plan={k: source["focus"].get(k) for k in ("plan_id", "version", "content_hash", "session_date", "research_cutoff", "published_at")})
    cursor["issue"] = issue
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(cursor, handle, ensure_ascii=False, allow_nan=False)
    os.replace(temp, path)
    return result
