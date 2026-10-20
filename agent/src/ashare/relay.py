"""Read a local alert snapshot for an authorized Codex heartbeat, no network."""
import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path

from .calendar import SHANGHAI, market_clock, now_shanghai


def check_relay(root: Path, session: str | None = None, *, now=None, recurring=False, expected_plan=None):
    if recurring:
        from .recurring_relay import check_recurring_relay
        return check_recurring_relay(root, now=now, expected_plan=expected_plan)
    now = (now or now_shanghai()).astimezone(SHANGHAI)
    day = date.fromisoformat(session)
    minute = now.hour * 60 + now.minute
    if now.date() != day or not (540 <= minute <= 695 or 780 <= minute <= 910):
        return {"notify": False, "status": "outside_requested_window", "session_date": session}
    cursor_path = root / "codex-relay-cursor.json"
    try:
        cursor = json.loads(cursor_path.read_text()) if cursor_path.exists() else {"seen": [], "issue": None}
    except (ValueError, OSError):
        cursor = {"seen": [], "issue": None}
    issue, source = None, None
    try:
        source = json.loads((root / "relay.json").read_text())
        current_clock = market_clock(now)
        maximum_age = 120 if current_clock["live_window"] else 720
        age = (now - datetime.fromisoformat(source["generated_at"])).total_seconds()
        if not 0 <= age <= maximum_age or not source["monitor_running"]:
            issue = "本地行情监控快照已过期或后台未运行，不能确认当前行情与风险；请打开平台核对"
        elif source.get("focus", {}).get("session_date") != session:
            issue = "重点池的目标日期不匹配，不能沿用旧计划判断"
        if isinstance(source.get("server_pid"), int) and source["server_pid"] > 0:
            try:
                os.kill(source["server_pid"], 0)
            except ProcessLookupError:
                issue = "本地行情服务进程已停止，不能继续监控，请启动平台"
            except PermissionError:
                pass  # Snapshot age still fails closed when process lookup is unavailable.
    except (OSError, ValueError, KeyError, TypeError):
        issue = "无法读取本地行情监控快照，当前提醒链路不可用"
    result = {"notify": False, "status": "unchanged", "session_date": session, "checked_at": now.isoformat(), "alerts": []}
    if issue:
        result.update(status="monitor_unavailable", notify=issue != cursor.get("issue"), message=issue)
    else:
        seen = set(cursor.get("seen", []))
        focus = source["focus"]
        focus_rows = {r["symbol"]: r for r in focus.get("rows", [])}
        alerts = []
        for alert in reversed(source.get("alerts", [])):
            if alert["id"] in seen:
                continue
            seen.add(alert["id"])
            if alert.get("acknowledged_at") or alert["created_at"][:10] != session:
                continue
            key = alert["key"]
            if key.startswith("focus_ready_"):
                row = focus_rows.get(key.removeprefix("focus_ready_"))
                stamp = datetime.fromisoformat((row or {}).get("quote", {}).get("as_of", "1970-01-01T00:00:00+00:00")) if row and row.get("quote") else None
                current = row and row.get("market_ready") and stamp and 0 <= (now - stamp).total_seconds() <= 90
                current = current and (alert["severity"] == "info" or row.get("entry_ready"))
                if not current:
                    alert = {**alert, "severity": "info", "active": False,
                             "message": "历史观察事件，当前条件已失效，不能据此买入。原记录：" + alert["message"]}
            elif key.startswith("drawdown_") and alert.get("active") is False:
                alert = {**alert, "severity": "info", "message": "历史风险事件，已不作为当前阶段触发条件；历史亏损仍保留。原记录：" + alert["message"]}
            elif not (key.startswith(("focus_", "drawdown_", "risk_period_started_", "provider_", "account_data", "concentration_", "locked_"))
                      or key == "calendar"
                      or any(key == prefix + symbol for prefix in ("quote_", "pullback_") for symbol in focus_rows)):
                continue
            alerts.append(alert)
        alerts.sort(key=lambda a: (a["severity"] != "critical", a["created_at"]))
        result.update(notify=bool(alerts) or bool(cursor.get("issue")), status="new_alerts" if alerts else "recovered" if cursor.get("issue") else "unchanged", alerts=alerts[:12])
        if cursor.get("issue"):
            result["message"] = "本地监控快照已恢复，仍需核对数据源时间与账户资料"
        if result["notify"]:
            result["risk_policy"] = source.get("risk_policy", {"warning_pct": 3, "restrict_pct": 4, "max_drawdown_pct": 5})
            relevant = [row for symbol, row in focus_rows.items()
                        if any(symbol in a["key"] for a in alerts[:12])]
            result["observations"] = [
                {k: row.get(k) for k in ("symbol", "name", "status", "entry_ready", "reasons",
                                         "observation_conditions", "invalidation_conditions")} |
                {"quote": {k: row["quote"].get(k) for k in ("price", "vwap", "open", "as_of")} if row.get("quote") else None,
                 "news": {**(row.get("news") or {}), "events": (row.get("news") or {}).get("events", [])[:3]}}
                for row in relevant]
        # Preserve ordering, do not make random set iteration evict recent IDs.
        ordered = list(dict.fromkeys([*cursor.get("seen", []), *[a["id"] for a in reversed(source.get("alerts", []))]]))
        delivered = {a["id"] for a in alerts[:12]}
        pending = {a["id"] for a in alerts[12:]}
        cursor["seen"] = [value for value in ordered if value not in pending or value in delivered][-1000:]
    cursor["issue"] = issue
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = cursor_path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(cursor, handle, ensure_ascii=False)
    os.replace(tmp, cursor_path)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    window = parser.add_mutually_exclusive_group(required=True)
    window.add_argument("--session")
    window.add_argument("--recurring", action="store_true")
    parser.add_argument("--plan-id")
    parser.add_argument("--plan-version", type=int)
    parser.add_argument("--plan-hash")
    args = parser.parse_args()
    expected = {"plan_id": args.plan_id, "version": args.plan_version, "content_hash": args.plan_hash}
    if any(v is not None for v in expected.values()) and not (args.recurring and all(v is not None for v in expected.values())):
        parser.error("计划校验需要--recurring及完整的编号、版本、校验值")
    print(json.dumps(check_relay(args.root, args.session, recurring=args.recurring,
                                expected_plan=expected if args.plan_id else None), ensure_ascii=False))


if __name__ == "__main__":
    main()
