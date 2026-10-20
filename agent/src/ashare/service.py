"""Persistent local monitor, separate from model inference and all order APIs."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import traceback
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from .calendar import market_clock, now_shanghai, quote_quality
from .feeds import INDEX_CODES, PublicFeeds
from .models import AccountInput, FocusPlan, NewsMonitoringInput, RiskPeriodInput, WatchSettings
from .focus import focus_view
from .news import news_view, merge_items, pending_items, version
from .news_feeds import NewsFeeds
from .news_analysis import analyze_news
from .policy import observations, risk_assessment, drawdown_thresholds
from .mainboard import mainboard_view, scan_mainboard, scan_cached_universe, cache_universe, review_candidates
from .schedule import news_monitoring
from .market_news import CompanyResolver, CATALYST, discovery_rows, market_news_view, validated_directory
from .plan import digest
from . import paper
from . import rotation

logger = logging.getLogger(__name__)


class AshareService:
    def __init__(self, root: Path, *, feeds=None, clock=now_shanghai, news_feeds=None, news_analyzer=None):
        self.root = root
        self.path = root / "state.json"
        self.feeds = feeds or PublicFeeds()
        self.now = clock
        self.news_feeds = news_feeds or NewsFeeds()
        self.news_analyzer = news_analyzer or analyze_news
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._news_lock = threading.Lock()
        self._analysis_lock = threading.Lock()
        self._mainboard_lock = threading.Lock()
        self._radar_lock = threading.Lock()
        self._task = None
        self._news_task = None
        self._analysis_task = None
        self._mainboard_task = None
        self._radar_task = None
        self._research_task = None
        self._research_lock = threading.Lock()
        self._public_signatures = {}
        self._research_job = {'running': False, 'status': 'idle'}
        self._research_generation_lock = threading.Lock()
        self._closing = False
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            if self.state.get("schema_version") != 1:
                raise ValueError("A 股风控状态版本无效，停止读取以保护历史高点")
            self.state["settings"] = WatchSettings.model_validate(self.state["settings"]).model_dump()
            if self.state.get("focus"):
                FocusPlan.model_validate(self.state["focus"])
        else:
            self.state = {"schema_version": 1, "settings": WatchSettings().model_dump(),
                          "account": None, "ledger": {}, "quotes": {}, "history": {},
                          "pool": {}, "alerts": [], "active_conditions": {}, "errors": {},
                          "refreshed_at": None, "pool_attempt_at": None}
        self.state.setdefault("focus", None)
        self.state.setdefault("disclosures", {})
        self.state.setdefault("focus_bars", {})
        self.state.setdefault("focus_plan_history", [])
        self.state.setdefault("news", {"sources": {}, "items": [], "analyses": {}})
        self.state["news"]["analysis_running"] = False
        self.state.setdefault("mainboard", {"enabled": False, "session_date": None})
        self.state["mainboard"]["running"] = False
        self.state.setdefault("market_news", {"reviews": {}, "tracked": {}})

        self._paper_path = root / 'paper-portfolio.json'
        self._paper_error = None
        self._paper = None
        self._paper_journal_signature = None
        if self._paper_path.exists():
            try:
                value = json.loads(self._paper_path.read_text())
                if value.get('mode') != 'local_simulation_only' or value.get('schema_version') != 1:
                    raise ValueError('模拟文件版本无效')
                FocusPlan.model_validate(value['plan'])
                if digest(value['plan']) != value['plan_identity']['content_hash']:
                    raise ValueError('模拟计划校验失败')
                self._paper = value
            except (ValueError, KeyError, TypeError, OSError):
                self._paper_error = '模拟记录不可用，已停止模拟；原文件保留，禁止自动重置'

    def _paper_radar(self, now, value=None):
        value=value or self._paper
        metrics=paper.metrics(value,self.state['quotes'],now)
        risk={'configured':True,'complete':metrics['complete'],'selection_allowed':metrics['level'] in {'normal','warning'}}
        return market_news_view(self.state['market_news'],self.state['news'],self.state['quotes'],risk,
            self.state['settings'],self.state['focus'],now)

    def _paper_focus(self, now, value=None, radar=None):
        value=value or self._paper
        metrics = paper.metrics(value, self.state['quotes'], now)
        risk = value['policy'] | {'selection_allowed': metrics['level'] in {'normal', 'warning'}}
        news = news_view(self.state['news'], value['plan'], now, settings=self.state['settings'])
        view=focus_view(value['plan'], self.state['disclosures'], self.state['quotes'],
            self.state['history'], risk, market_clock(now), now, enabled=self.state['settings']['enabled'],
            news=news, bars=self.state['focus_bars'])
        symbols={x['symbol'] for x in rotation.stocks(value)}
        view['rows']=[x for x in view['rows'] if x['symbol'] in symbols]
        view['rows'].extend(rotation.dynamic_rows(value,radar or self._paper_radar(now,value),self.state['focus_bars'],now,metrics['level']))
        return view

    def _advance_paper(self, now):
        if self._paper is None:
            return
        try:
            radar=self._paper_radar(now)
            if paper.identity(self.state['focus'] or {})!=self._paper['plan_identity']:radar['active']=False
            archived={}
            for p in [self.state['focus'],*[h['plan'] for h in self.state['focus_plan_history']]]:
                for stock in (p or {}).get('stocks',[]):
                    if stock['disclosure_status'] in {'pending','blocked'}:archived[stock['symbol']]=stock.get('disclosure_summary','待核验')
            updated=rotation.evaluate(self._paper,radar,self._paper_focus(now,radar=radar)['rows'],self.state['focus_bars'],now,
                archived_blocks=archived,allow_entries=paper.metrics(self._paper,self.state['quotes'],now)['level'] in {'normal','warning'})
            updated = paper.advance(updated, self._paper_focus(now,updated,radar), self.state['quotes'], now,
                monitoring_enabled=self.state['settings']['enabled'],
                plan_matches=paper.identity(self.state['focus'] or {}) == self._paper['plan_identity'])
            paper.save(self._paper_path, updated)
            self._paper = updated
            self._journal_paper_rotation(now)
            self._paper_error = None
        except Exception:
            self._paper_error = '模拟计算或保存失败，暂停模拟更新；保留上次记录'
            logger.exception('Local virtual portfolio update failed')

    def _journal_paper_rotation(self, now):
        r=self._paper.get('rotation',{})
        rows=[{k:r['proposals'][symbol].get(k) for k in ('symbol','name','rank','score','source_hash','eligible','blockers','evidence','events','replace_symbol','reason')}
              for symbol in r.get('ranking',[])[:8]]
        body={'plan_identity':self._paper['plan_identity'],'mode':r.get('mode'),
            'active_symbols':[x['symbol'] for x in rotation.stocks(self._paper)],'candidates':rows,
            'switch_count':len(r.get('history',[])),'simulation_only':True}
        signature=rotation.key(body)
        if signature==self._paper_journal_signature:return
        path=self.root/'paper-rotation-journal'/f'{now.date().isoformat()}.jsonl'
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(path,os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)
        with os.fdopen(fd,'a') as handle:
            handle.write(json.dumps({'at':now.isoformat(),**body},ensure_ascii=False,allow_nan=False)+'\n')
        self._paper_journal_signature=signature

    def paper_snapshot(self):
        with self._lock:
            return {'portfolio': paper.snapshot(self._paper, self.state['quotes'], self.now(),
                plan_matches=paper.identity(self.state['focus'] or {}) == self._paper['plan_identity'])
                if self._paper else None, 'error': self._paper_error}

    def start_paper(self, value):
        with self._lock:
            if self._paper_error:
                raise ValueError(self._paper_error)
            plan = self.state['focus'] or {}
            if plan.get('content_hash') != value.expected_plan_hash:
                raise ValueError('待模拟计划与当前发布版本不一致')
            if self._paper:
                if (self._paper['plan_identity'] != paper.identity(plan)
                    or self._paper['initial_cash'] != value.initial_cash):
                    raise ValueError('已有模拟组合，禁止覆盖历史或自动重置')
                return self.paper_snapshot()
            initial = paper.create(plan, value.initial_cash, drawdown_thresholds(self.state['settings']), self.now())
            paper.save(self._paper_path, initial)
            self._paper = initial
            self._advance_paper(self.now())
            self._save_relay()
            return self.paper_snapshot()

    def set_paper_status(self, enabled):
        with self._lock:
            if not self._paper or self._paper_error:
                raise ValueError('没有可用的模拟组合')
            updated = deepcopy(self._paper) | {'enabled': enabled}
            paper.save(self._paper_path, updated)
            self._paper = updated
            self._save_relay()
            return self.paper_snapshot()

    def set_paper_rotation(self, value):
        with self._lock:
            if not self._paper or self._paper_error:raise ValueError('模拟组合不可用')
            if self._paper['plan_identity']['content_hash']!=value.expected_plan_hash:raise ValueError('模拟计划身份不一致')
            updated=deepcopy(self._paper);updated.setdefault('rotation',rotation.configuration())['mode']=value.mode
            paper.save(self._paper_path,updated);self._paper=updated
            self._advance_paper(self.now())
            self._save_relay()
            return self.paper_snapshot()

    def paper_candidate(self, value):
        with self._lock:
            if not self._paper or self._paper_error:raise ValueError('模拟组合不可用')
            self._advance_paper(self.now())
            if self._paper_error:raise ValueError(self._paper_error)
            p=self._paper.get('rotation',{}).get('proposals',{}).get(value.symbol)
            if not p or p['source_hash']!=value.source_hash:raise ValueError('候选来源已改变，必须重新读取')
            if not p.get('stock'):raise ValueError('缺少冻结参考位，不能核对或确认替换')
            updated=deepcopy(self._paper);r=updated['rotation']
            if value.action=='review':r['approvals'][value.symbol]={'source_hash':value.source_hash,'at':self.now().isoformat()}
            elif not p.get('eligible'):raise ValueError('当前价格、来源、风控或持续性条件未通过，不能确认替换')
            else:r['confirmations'][value.symbol]=value.source_hash
            paper.save(self._paper_path,updated);self._paper=updated
            self._advance_paper(self.now())
            self._save_relay()
            return self.paper_snapshot()

    def _save(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        temporary = self.path.with_name(".state-" + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(self.state, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self._save_relay()

    def _save_relay(self):
        # A local, reduced relay lets Codex check alerts without network or
        # reading private holdings. Stale relay timestamps remain detectable.
        relay_tmp = self.root / (".relay-" + uuid.uuid4().hex + ".tmp")
        try:
            fd = os.open(relay_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(self.relay_snapshot(), handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(relay_tmp, self.root / "relay.json")
            self._journal_public_focus()
        except Exception:
            logger.exception("A-share alert relay write failed")
        finally:
            relay_tmp.unlink(missing_ok=True)

    def _view(self, now: datetime) -> dict:
        clock = market_clock(now)
        settings = self.state["settings"]
        _, risk = risk_assessment(self.state["account"], self.state["quotes"], self.state["ledger"], settings, clock, now)
        if not settings["enabled"]:
            risk["selection_allowed"] = False
            risk["guidance"] = "监控已暂停；仅显示留存快照，暂停新增仓位判断。" + risk["guidance"]
        pool = self.state["pool"]
        pool_valid = bool(pool.get("date") == clock["latest_completed_session"] and not self.state["errors"].get("billboard"))
        rows = observations(pool, self.state["quotes"], self.state["history"], clock, now, pool_valid=pool_valid and settings["enabled"])
        if not settings["enabled"]:
            for row in rows:
                row["label"] = "监控暂停"
        quotes = [{**q, "quality": quote_quality(q, clock, now)} for q in self.state["quotes"].values()]
        news = news_view(self.state["news"], self.state["focus"], now, settings=settings)
        news.pop("conditions", None)
        focus = focus_view(self.state["focus"], self.state["disclosures"], self.state["quotes"],
                           self.state["history"], risk, clock, now, enabled=settings["enabled"], news=news,
                           disclosure_monitoring=news_monitoring(self.state["focus"], settings, now)["active"] and settings["recurring_news_enabled"],
                           bars=self.state["focus_bars"])
        focus_conditions = {c["key"]: c for c in focus.pop("conditions", [])}
        radar = market_news_view(self.state["market_news"], self.state["news"], self.state["quotes"], risk, settings, self.state["focus"], now)
        radar.pop("conditions", None)
        alerts = deepcopy(self.state["alerts"][-100:][::-1])
        focus_rows = {row["symbol"]: row for row in focus["rows"]}
        radar_rows = {row["symbol"]: row for row in radar["rows"]}
        for alert in alerts:
            if alert['key'].startswith('paper_'):
                matching=alert.get('simulation') is True and alert.get('plan_identity')=={k:focus.get(k) for k in ('plan_id','version','content_hash')}
                alert['active']=bool(matching and alert['created_at'][:10]==now.date().isoformat())
                if alert['key'].startswith('paper_candidate_'):
                    proposal=alert.get('rotation_proposal',{});r=(self._paper or {}).get('rotation',{})
                    current=r.get('proposals',{}).get(proposal.get('symbol'),{})
                    checked=current.get('checked_at')
                    alert['active']=bool(alert['active'] and proposal.get('symbol') in r.get('ranking',[])
                        and current.get('source_hash')==proposal.get('source_hash') and checked
                        and alert['key']==rotation.proposal_notice_key(current) and not current.get('active_slot')
                        and 0<=(now-datetime.fromisoformat(checked)).total_seconds()<=180)
                continue
            if alert["key"].startswith("focus_") and focus.get("plan_id") and alert.get("plan_identity") != {
                    k: focus[k] for k in ("plan_id", "version", "content_hash")}:
                alert["active"] = False
                continue
            if focus.get("plan_id") and alert["key"].startswith(("focus_disclosure_", "focus_bars_")):
                current = focus_conditions.get(alert["key"])
                alert["active"] = current is not None
                if current:
                    alert.update(current)
            if alert.get("news_event"):
                alert["active"] = alert["key"] in self.state["active_conditions"]
            if alert["key"].startswith("drawdown_"):
                alert["active"] = (alert.get("risk_period_id") == (risk.get("monitoring_period") or {}).get("id")
                                   and alert["key"] == "drawdown_" + risk["level"])
            if alert["key"].startswith("focus_ready_"):
                row = focus_rows.get(alert["key"].removeprefix("focus_ready_")) or {}
                alert["active"] = bool(row.get("market_ready") and (alert["severity"] == "info" or row.get("entry_ready")))
            if alert["key"].startswith("focus_invalid_"):
                row = focus_rows.get(alert["key"].removeprefix("focus_invalid_")) or {}
                alert["active"] = bool(row and not row.get("market_ready") and focus.get("session_date") == now.date().isoformat())
            if alert["key"].startswith(("radar_price_", "radar_invalid_")):
                row = next((r for s, r in radar_rows.items() if s in alert["key"]), {})
                alert["active"] = bool(row and (row.get("market_ready") if alert["key"].startswith("radar_price_") else not row.get("market_ready")))
        return {"settings": deepcopy(settings), "market": clock, "monitor_running": self._task is not None and not self._task.done(),
                "refreshed_at": self.state["refreshed_at"], "errors": deepcopy(self.state["errors"]),
                "billboard": {k: v for k, v in pool.items() if k != "rows"} | {"pool_valid": pool_valid, "selected": len(rows)},
                "rows": rows, "quotes": quotes, "risk": risk, "focus": focus, "news": news, "market_news": radar,
                "risk_policy_audit": deepcopy(self.state.get("risk_policy_audit", [])[-10:]),
                "risk_period_history": deepcopy(self.state.get("risk_period_history", [])),
                "mainboard": mainboard_view(self.state["mainboard"], self.state["quotes"], now, enabled=settings["enabled"]),
                "account": deepcopy(self.state["account"]),
                "alerts": alerts,
                "unread_alerts": sum(not a.get("acknowledged_at") for a in self.state["alerts"]),
                "execution_mode": "manual_only", "rule_version": "ashare-observation-v1",
                "notice": "公开网页报价按间隔轮询，不保证交易所级延迟；回撤阈值用于提醒，不能保证损失不越线。服务停止或电脑休眠会中断采集。"}

    def snapshot(self) -> dict:
        with self._lock:
            return self._view(self.now())

    def request_mainboard(self) -> dict:
        with self._lock:
            now = self.now()
            board = self.state["mainboard"]
            attempted = board.get("attempted_at")
            if not board.get("running") and (not attempted or (now - datetime.fromisoformat(attempted)).total_seconds() >= 60):
                board.update(enabled=True, session_date=now.date().isoformat(), requested=True)
                self._save()
            return self._view(now)

    def refresh_mainboard(self):
        if not self._mainboard_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                now = self.now()
                board = self.state["mainboard"]
                if self._closing or not self.state["settings"]["enabled"] or not board.get("enabled"):
                    return
                requested = board.get("requested")
                if not requested and (board.get("session_date") != now.date().isoformat() or not market_clock(now)["continuous"]):
                    return
                attempted = board.get("attempted_at")
                if not requested and attempted and (now - datetime.fromisoformat(attempted)).total_seconds() < 300:
                    return
                board.update(running=True, requested=False, attempted_at=now.isoformat(), progress={})
            def progress(value):
                with self._lock:
                    self.state["mainboard"]["progress"] = value
            try:
                try:
                    scan = scan_mainboard(clock=self.now, progress=progress)
                    cache_universe(scan, self.root / "mainboard-universe.json")
                    source_warning = None
                except Exception:
                    # Same-day symbol directory only; no EM price cache is reused.
                    scan = scan_cached_universe(self.root / "mainboard-universe.json", feeds=self.feeds, clock=self.now, progress=progress)
                    source_warning = "东方财富全量行情暂不可用；当前使用同日已验证主板名录与腾讯逐批报价，名录时间见下方。"
                scan.pop("raw_rows", None)
                review_candidates(scan, feeds=self.feeds, news_feeds=self.news_feeds, clock=self.now)
                with self._lock:
                    self.state["mainboard"].update(scan=scan, error=None, source_warning=source_warning)
                    # The quote worker will verify the 20 shortlisted names next.
                    self.state["refreshed_at"] = None
            except Exception as exc:
                with self._lock:
                    self.state["mainboard"]["error"] = "主板扫描失败，保留旧结果作参考：" + (str(exc)[:180] if isinstance(exc, ValueError) else type(exc).__name__)
            finally:
                with self._lock:
                    self.state["mainboard"]["running"] = False
                    self._save()
        finally:
            self._mainboard_lock.release()

    def settings(self, value: WatchSettings) -> dict:
        with self._refresh_lock, self._lock:
            if value.pool_size != self.state["settings"]["pool_size"]:
                self.state["pool_attempt_at"] = None
            before = drawdown_thresholds(self.state["settings"])
            after = drawdown_thresholds(value.model_dump())
            if before != after:
                self.state.setdefault("risk_policy_audit", []).append({
                    "id": uuid.uuid4().hex, "changed_at": self.now().isoformat(),
                    "before": before, "after": after, "source": "local_operator_settings"})
            self.state["settings"] = value.model_dump()
            self.state["refreshed_at"] = None
            self._evaluate(self.now())
            self._save()
            return self._view(self.now())

    def set_focus(self, value: FocusPlan) -> dict:
        if value.plan_id:
            return self.publish_focus(value)
        now = self.now()
        if value.reviewed_at > now + timedelta(seconds=60):
            raise ValueError("公告核验时间不能在未来")
        with self._refresh_lock, self._lock:
            self.state["focus"] = value.model_dump(mode="json")
            self.state["disclosures"] = {}
            self.state["refreshed_at"] = None
            self._evaluate(now)
            self._save()
            return self._view(now)

    def publish_focus(self, value: FocusPlan) -> dict:
        """Publish one canonical plan; account, ledger and thresholds are untouched."""
        now = self.now()
        if not value.plan_id or value.reviewed_at > now + timedelta(seconds=60):
            raise ValueError("需要有编号的完整计划，核验时间不能在未来")
        proposed = value.model_dump(mode="json")
        checksum = digest(proposed)
        with self._refresh_lock, self._lock:
            current = self.state.get("focus")
            prior = [*(p["plan"] for p in self.state["focus_plan_history"]), *([current] if current else [])]
            matching = [p for p in prior if p.get("plan_id") == value.plan_id and p.get("version") == value.version]
            if matching:
                if any(digest(p) != checksum for p in matching):
                    raise ValueError("相同计划版本内容不可覆盖，请发布新版本")
                if current and current.get("plan_id") == value.plan_id and current.get("version") == value.version:
                    return self.relay_snapshot()
                raise ValueError("已归档计划不能通过重试重新激活")
            versions = [p["version"] for p in prior if p.get("plan_id") == value.plan_id]
            if value.version != max(versions, default=0) + 1:
                raise ValueError("计划版本必须连续递增")
            if current:
                self.state["focus_plan_history"].append({"archived_at": now.isoformat(), "plan": deepcopy(current)})
            protected = {k: deepcopy(self.state[k]) for k in ("account", "ledger", "settings")}
            proposed.update(published_at=now.isoformat(), content_hash=checksum)
            self.state["focus"] = proposed
            if proposed.get('screening'):
                for candidate in proposed['screening']['rows']:
                    if candidate['selected'] and candidate.get('quote'):
                        self.state['quotes'].setdefault(candidate['symbol'], deepcopy(candidate['quote']))
            self.state["disclosures"] = {}
            self.state["focus_bars"] = {}
            self.state["refreshed_at"] = None
            self.state["active_conditions"] = {k: v for k, v in self.state["active_conditions"].items() if not k.startswith("focus_")}
            self.state.setdefault("focus_publication_audit", []).append({"plan_id": value.plan_id, "version": value.version,
                "content_hash": checksum, "published_at": now.isoformat(),
                "protected_fields_unchanged": {k: self.state[k] == v for k, v in protected.items()}})
            self._save()
            return self.relay_snapshot()

    def plan_snapshot(self) -> dict:
        with self._lock:
            relay = self.relay_snapshot()
            return {"focus": relay["focus"], "risk_policy": relay["risk_policy"], "risk_level": relay["risk_level"],
                    "publication_audit": deepcopy(self.state.get("focus_publication_audit", [])[-10:]),
                    "archives": [{"archived_at": p["archived_at"], "plan_id": p["plan"].get("plan_id"),
                        "session_date": p["plan"]["session_date"], "stocks": [{k: s[k] for k in
                            ("symbol", "name", "disclosure_status", "disclosure_summary")} for s in p["plan"]["stocks"]]}
                        for p in self.state["focus_plan_history"]]}

    def _research_outcome_path(self, plan):
        return self.root / 'research-outcomes' / f"{plan['plan_id']}-v{plan['version']}.json"

    def research_snapshot(self):
        """Public, immutable rankings and a separate outcome file; no account data."""
        with self._lock:
            plan = deepcopy(self.state.get('focus') or {})
        audit = plan.get('screening')
        draft = None
        error = None
        path = self.root / 'research-draft.json'
        if path.exists():
            try:
                value = FocusPlan.model_validate(json.loads(path.read_text()))
                if value.screening is None: raise ValueError('研究草案缺少冻结候选')
                draft = {'plan': value.model_dump(mode='json'), 'content_hash': digest(value.model_dump(mode='json'))}
                if draft['content_hash'] == plan.get('content_hash'):
                    draft = None
            except (ValueError, KeyError, TypeError, OSError) as exc:
                error = '研究草案校验失败：' + str(exc)[:200]
        outcome = None
        if audit:
            outcome_path = self._research_outcome_path(plan)
            if outcome_path.exists():
                try:
                    value = json.loads(outcome_path.read_text())
                    if value.get('research_hash') == audit['content_hash']: outcome = value
                except (OSError, ValueError): error = '复盘结果文件不可用，不能推断收益'
        return {'plan_identity': {k: plan.get(k) for k in ('plan_id','version','content_hash')},
                'published': audit, 'draft': draft, 'outcome': outcome, 'error': error,
                'job': deepcopy(self._research_job),
                'execution': 'manual_only'}

    def request_research_draft(self):
        from .calendar import is_session
        now=self.now()
        if not market_clock(now)['is_session'] or now.hour < 15:
            raise ValueError('收盘后才能冻结下一交易日研究，盘中只更新观察状态')
        target=now.date()+timedelta(days=1)
        for _ in range(35):
            if is_session(target) is True: break
            if is_session(target) is None: raise ValueError('目标年度交易日历未知')
            target+=timedelta(days=1)
        if not self._research_generation_lock.acquire(blocking=False):
            return self.research_snapshot()
        self._research_job={'running':True,'status':'running','target_date':str(target),'started_at':now.isoformat(),'progress':'研究任务排队'}
        def worker():
            from .research_workflow import generate_local_draft
            directory=self.root.parent/'workspace'/('research-'+str(target)+'-'+uuid.uuid4().hex[:8])
            try:
                result=generate_local_draft(self.root,str(target),directory,
                    progress=lambda message:self._research_job.update(progress=message))
                self._research_job.update(status='complete',progress='草案已生成，等待核对和发布',result=result)
            except Exception as exc:
                self._research_job.update(status='failed',progress='采集或模型核验未完成，已发布计划保留',error=type(exc).__name__+': '+str(exc)[:250])
            finally:
                self._research_job.update(running=False,finished_at=self.now().isoformat())
                self._research_generation_lock.release()
        threading.Thread(target=worker,name='ashare-research-generation',daemon=True).start()
        return self.research_snapshot()

    def publish_research_draft(self, expected_hash):
        path = self.root / 'research-draft.json'
        if not path.exists(): raise ValueError('没有本地研究草案')
        value = FocusPlan.model_validate(json.loads(path.read_text()))
        if value.screening is None or digest(value.model_dump(mode='json')) != expected_hash:
            raise ValueError('研究草案已改变，请重新核对内容')
        return self.publish_focus(value)

    def _journal_public_focus(self):
        """Retain observed signal transitions for replay, without private holdings."""
        plan = self.state.get('focus') or {}
        if not plan.get('screening'): return
        relay = self.relay_snapshot()
        identity = {k: relay['focus'][k] for k in ('plan_id','version','content_hash')}
        for row in relay['focus']['rows']:
            event = {'at': relay['generated_at'], 'plan_identity': identity, 'symbol': row['symbol'],
                     'status': row['status'], 'entry_ready': row['entry_ready'],
                     'market_ready': row['market_ready'], 'risk_blockers': row.get('risk_blockers', []),
                     'price_evidence': row.get('price_evidence'), 'quote': row.get('quote')}
            signature = json.dumps([identity, event['status'], event['entry_ready'], event['market_ready'],
                event['risk_blockers'], {k:(event['price_evidence'] or {}).get(k) for k in
                    ('stage','ready','invalidated','breakout_at','retest_at')}], sort_keys=True)
            if self._public_signatures.get(row['symbol']) == signature: continue
            directory = self.root / 'public-signal-journal'
            directory.mkdir(exist_ok=True)
            with (directory / (relay['generated_at'][:10]+'.jsonl')).open('a') as handle:
                handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False)+'\n')
            self._public_signatures[row['symbol']] = signature

    def refresh_research_outcomes(self):
        from .screening import evaluate_outcome
        if not self._research_lock.acquire(blocking=False): return
        try:
            now = self.now()
            with self._lock:
                plan = deepcopy(self.state.get('focus') or {})
                active = self.state['settings']['enabled']
            if (not active or not plan.get('screening') or plan['session_date'] != now.date().isoformat()
                or now.hour < 15 or now.hour*60+now.minute > 1260 or not market_clock(now)['is_session']): return
            path = self._research_outcome_path(plan)
            if path.exists():
                try:
                    previous = json.loads(path.read_text())
                    if previous.get('research_hash') == plan['screening']['content_hash'] and previous.get('candidate_coverage') == len(plan['screening']['rows']): return
                except (OSError, ValueError): pass
            quotes = self.feeds.quotes([r['symbol'] for r in plan['screening']['rows']], now)
            # Require an actual closing source time, never a 14:55 snapshot.
            quotes = {s:q for s,q in quotes.items() if q.get('as_of','')[11:16] >= '15:00'}
            result = evaluate_outcome(plan['screening'], quotes) | {'generated_at': now.isoformat()}
            path.parent.mkdir(exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False))
            os.replace(temporary, path)
        finally:
            self._research_lock.release()

    def set_news_monitoring(self, value: NewsMonitoringInput) -> dict:
        with self._refresh_lock, self._lock:
            before = self.state["settings"]["recurring_news_enabled"]
            self.state["settings"]["recurring_news_enabled"] = value.enabled
            if value.market_wide is not None:
                prior_scope = self.state["settings"]["market_news_enabled"]
                self.state["settings"]["market_news_enabled"] = value.market_wide
                if prior_scope != value.market_wide:
                    self.state.setdefault("news_scope_audit", []).append({"changed_at": self.now().isoformat(),
                        "before": prior_scope, "after": value.market_wide, "scope": "sh_sz_mainboard", "source": "local_operator"})
            if before != value.enabled:
                self.state.setdefault("news_monitoring_audit", []).append({"changed_at": self.now().isoformat(),
                    "before": before, "after": value.enabled, "source": "local_operator"})
                if value.enabled:
                    self.state["news"]["recurring_started_at"] = self.now().isoformat()
            self._evaluate(self.now())
            self._save()
            return self.relay_snapshot()

    def relay_snapshot(self) -> dict:
        """Notifications without sending cash, position quantities or account IDs."""
        with self._lock:
            view = self._view(self.now())
            focus = view["focus"]
            alerts = deepcopy(view["alerts"])
            for alert in alerts:
                if alert["key"].startswith("locked_"):
                    alert["message"] = "持仓存在录入的不可卖部分，请在本地账户页核对T+1及券商可卖数量"
                elif alert["key"].startswith("concentration_"):
                    alert["message"] = "持仓触发单股仓位比例提醒，具体比例请在本地账户页核对"
            return {"generated_at": self.now().isoformat(), "server_pid": os.getpid(), "market": view["market"], "monitor_running": view["monitor_running"],
                    "refreshed_at": view["refreshed_at"], "focus": focus, "market_news": view["market_news"],
                    "account_configured": view["risk"]["configured"],
                    "account_complete": view["risk"]["complete"], "risk_level": view["risk"]["level"],
                    "risk_policy": drawdown_thresholds(view["settings"]) | {
                        "drawdown_basis": view["risk"]["drawdown_basis"],
                        "period_started_at": (view["risk"].get("monitoring_period") or {}).get("started_at")},
                    "news_monitoring": news_monitoring(self.state["focus"], view["settings"], self.now()),
                    "health": {"news": bool(view["news"].get("sources")) and all(s["fresh"] for s in view["news"].get("sources", {}).values()),
                               "disclosures": bool(focus.get("disclosures_fresh")),
                               "quotes": not bool(view["errors"].get("quotes")) and bool(focus.get("rows")) and
                                   all(r.get("quality") in {"fresh", "reference"} for r in focus.get("rows", [])),
                               **({"focus_m5": all(r.get("price_evidence", {}).get("stage") != "data_missing" for r in focus["rows"])}
                                  if focus.get("plan_id") and view["market"]["continuous"] else {}),
                               **({'paper_simulation':not bool(self._paper_error)} if self._paper or self._paper_error else {})},
                    "paper_rotation": rotation.public_summary(self._paper,self._paper_error),
                    "errors": view["errors"], "alerts": alerts}

    def start_risk_period(self, value: RiskPeriodInput) -> dict:
        """Explicit operator rebase; keep the historical ledger and account intact."""
        with self._refresh_lock, self._lock:
            now = self.now()
            periods = self.state.get("risk_period_history", [])
            current = self.state["ledger"].get("monitoring_period")
            if any(p["id"] == value.submission_id for p in [*periods, *([current] if current else [])]):
                return self._view(now)  # Retrying even an archived request cannot start another period.
            account = self.state["account"]
            if not account or account["submission_id"] != value.expected_account_submission_id:
                raise ValueError("账户快照已变化或尚未保存，请重新核对全部现金和持仓")
            if account["confirmed_at"][:10] != now.date().isoformat():
                raise ValueError("新阶段需要今天重新确认的全部现金与持仓")
            ledger, risk = risk_assessment(account, self.state["quotes"], self.state["ledger"],
                                           self.state["settings"], market_clock(now), now)
            if not risk["complete"]:
                raise ValueError("账户估值不完整，不能建立新基准：" + risk["guidance"])
            if current:
                self.state.setdefault("risk_period_history", []).append(deepcopy(ledger["monitoring_period"]) | {
                    "ended_at": now.isoformat(), "end_nav": ledger["last_nav"],
                    "end_equity": risk["equity"], "end_drawdown_pct": risk["drawdown_pct"]})
            ledger["monitoring_period"] = {
                "id": value.submission_id, "started_at": now.isoformat(),
                "account_submission_id": account["submission_id"], "account_confirmed_at": account["confirmed_at"],
                "start_equity": risk["equity"], "start_nav": ledger["last_nav"],
                "peak_nav": ledger["last_nav"], "breached": False, "valuation_basis": risk["basis"],
                "quote_as_of": {p["symbol"]: self.state["quotes"][p["symbol"]]["as_of"] for p in account["positions"]},
                "history_at_start": {"drawdown_pct": risk["historical_drawdown_pct"],
                    "peak_nav": ledger["peak_nav"], "peak_equity_equivalent": risk["historical_peak_equity_equivalent"],
                    "breached": ledger.get("breached", False)},
                "source": "local_operator_confirmed_account"}
            self.state["ledger"] = ledger
            self.state["alerts"].append({"id": uuid.uuid4().hex, "key": "risk_period_started_" + value.submission_id,
                "severity": "info", "created_at": now.isoformat(), "acknowledged_at": None,
                "message": "已按今天确认的账户估值建立新监测阶段；历史高点、亏损与旧风险记录继续保留。阈值未改变，公告限制仍有效。"})
            self._evaluate(now)
            self._save()
            return self._view(now)

    def account(self, value: AccountInput) -> dict:
        now = self.now()
        if value.peak_as_of > now + timedelta(seconds=60):
            raise ValueError("净值高点时间不能在未来")
        with self._refresh_lock, self._lock:
            prior = self.state["account"]
            if prior and prior["submission_id"] == value.submission_id:
                return self._view(now)
            ledger = self.state["ledger"]
            if ledger.get("units") is None and value.external_flow:
                raise ValueError("首次建立基准请填当前现金和历史高点；出入金字段从下次更新开始使用")
            if ledger.get("units") and prior and value.historical_peak_equity != prior["historical_peak_equity"]:
                raise ValueError("初始历史高点不可重设；后续高点由份额化净值持续记录")
            if ledger.get("pending_flow") and value.external_flow != ledger["pending_flow"]:
                raise ValueError("上次出入金尚未完成估值；修正账户时请保留同一笔净出入金金额，勿再次计入新资金")
            proposed = value.model_dump(mode="json") | {"confirmed_at": now.isoformat()}
            proposed_ledger = deepcopy(ledger) | {"pending_flow": value.external_flow}
            _, preview = risk_assessment(proposed, self.state["quotes"], proposed_ledger, self.state["settings"], market_clock(now), now)
            if preview["guidance"] in {"出入金与净值不一致，无法进行份额化调整", "账户净值必须大于零"}:
                raise ValueError(preview["guidance"])
            self.state["account"] = proposed
            self.state["ledger"] = proposed_ledger
            self.state["refreshed_at"] = None
            self._evaluate(now)
            self._save()
            return self._view(now)

    def _evaluate(self, now: datetime):
        clock = market_clock(now)
        ledger, risk = risk_assessment(self.state["account"], self.state["quotes"], self.state["ledger"], self.state["settings"], clock, now)
        self.state["ledger"] = ledger
        conditions = list(risk["conditions"])
        news = news_view(self.state["news"], self.state["focus"], now, settings=self.state["settings"])
        focus = focus_view(self.state["focus"], self.state["disclosures"], self.state["quotes"],
                           self.state["history"], risk, clock, now,
                           enabled=self.state["settings"]["enabled"], news=news,
                           disclosure_monitoring=news_monitoring(self.state["focus"], self.state["settings"], now)["active"] and self.state["settings"]["recurring_news_enabled"],
                           bars=self.state["focus_bars"])
        conditions.extend(focus["conditions"])
        self._advance_paper(now)
        if self._paper and not self._paper_error and self.state['settings']['enabled']:
            conditions.extend(rotation.notifications(self._paper,now))
        if self.state["settings"]["enabled"]:
            conditions.extend(news["conditions"])
            radar = market_news_view(self.state["market_news"], self.state["news"], self.state["quotes"], risk,
                                     self.state["settings"], self.state["focus"], now)
            conditions.extend(radar["conditions"])
            for row in radar["rows"]:
                key = "radar_price_" + row["symbol"]
                if key in self.state["active_conditions"] and not row["market_ready"]:
                    conditions.append({"key": "radar_invalid_" + row["symbol"], "severity": "warning",
                        "message": f"{row['name']} {row['symbol']} 原利好量价观察已失效：{'；'.join(row['reasons'][:3])}；不能沿用历史观察条件"})
        current_keys = {c["key"] for c in conditions}
        for row in focus["rows"]:
            if "focus_ready_" + row["symbol"] in self.state["active_conditions"] and "focus_ready_" + row["symbol"] not in current_keys:
                conditions.append({"key": "focus_invalid_" + row["symbol"], "severity": "warning",
                                   "message": f"{row['name']} {row['symbol']} 原观察条件已失效：{'；'.join(row['reasons'][:5])}。不能沿用历史提醒操作；请核对T+1可卖数量"})
        if not clock["calendar_verified"]:
            conditions.append({"key": "calendar", "severity": "critical", "message": "本年度交易日历尚未核验，暂停盘中候选判断"})
        for provider, error in self.state["errors"].items():
            conditions.append({"key": "provider_" + provider, "severity": "warning", "message": error})
        # Missing and stale quotes remain visible even when the account is not configured.
        symbols = set(self.state["settings"]["watchlist"]) | {r["symbol"] for r in self.state["pool"].get("rows", [])}
        for symbol in sorted(symbols):
            quote = self.state["quotes"].get(symbol)
            quality = quote_quality(quote, clock, now)
            if quality not in {"fresh", "reference"}:
                conditions.append({"key": "quote_" + symbol, "severity": "warning", "message": f"{symbol} 行情不可用于盘中判断：{quality}"})
            if quote and quality == "fresh":
                high = quote.get("high") or quote["price"]
                if high > 0 and quote["price"] / high <= 0.97:
                    conditions.append({"key": "pullback_" + symbol, "severity": "warning", "message": f"{symbol} 从日内高点回落至少 3%，请核对可卖股数及波动风险"})
        active = {}
        for condition in conditions:
            key = condition["key"]
            if key.startswith("focus_") and focus.get("plan_id"):
                condition = condition | {"plan_identity": {k: focus[k] for k in ("plan_id", "version", "content_hash")}}
            active[key] = condition["severity"]
            if self.state["active_conditions"].get(key) == condition["severity"]:
                continue
            if key.startswith("focus_") and any(a["key"] == key and a["severity"] == condition["severity"] and
                    a.get("plan_identity") == condition.get("plan_identity") and
                    (now - datetime.fromisoformat(a["created_at"])).total_seconds() < 900 for a in self.state["alerts"]):
                continue
            self.state["alerts"].append({**condition, "id": uuid.uuid4().hex, "created_at": now.isoformat(), "acknowledged_at": None})
        self.state["active_conditions"] = active
        self.state["alerts"] = self.state["alerts"][-300:]

    def refresh(self, *, refresh_pool: bool = False) -> dict:
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot() | {"refresh_busy": True}
        try:
            now = self.now()
            with self._lock:
                settings = deepcopy(self.state["settings"])
                last = self.state["refreshed_at"]
                if last and (now - datetime.fromisoformat(last)).total_seconds() < 10:
                    return self._view(now) | {"refresh_throttled": True}
                pool_attempt = self.state["pool_attempt_at"]
                pool_due = not pool_attempt or (now - datetime.fromisoformat(pool_attempt)).total_seconds() >= (60 if refresh_pool else 900)
            clock = market_clock(now)
            with self._lock:
                focus = deepcopy(self.state["focus"])
                checked = self.state["disclosures"].get("checked_at")
            if focus and focus["enabled"] and (not checked or (now - datetime.fromisoformat(checked)).total_seconds() >= 300):
                try:
                    disclosures = self.feeds.disclosures([s["symbol"] for s in focus["stocks"]], focus["review_since"], now)
                    with self._lock:
                        self.state["disclosures"] = disclosures
                except Exception as exc:
                    with self._lock:
                        self.state["disclosures"]["checked_at"] = now.isoformat()
                        self.state["disclosures"]["error"] = "公告获取失败：" + type(exc).__name__
            if focus and focus.get("plan_id") and focus["enabled"] and focus["session_date"] == now.date().isoformat() and clock["continuous"]:
                with self._lock:
                    extras=[*rotation.stocks(self._paper),*discovery_rows(self.state['market_news'],self.state['news'],self.now())[:3]] if self._paper else []
                    bar_stocks={s['symbol']:s for s in [*focus['stocks'],*extras]}
                for stock in bar_stocks.values():
                    symbol = stock["symbol"]
                    checked = self.state["focus_bars"].get(symbol, {}).get("checked_at")
                    if checked and (self.now() - datetime.fromisoformat(checked)).total_seconds() < 60:
                        continue
                    try:
                        value = self.feeds.five_minute_bars(symbol, self.now())
                    except Exception as exc:
                        value = {"checked_at": self.now().isoformat(), "items": [], "error": "5分钟K线获取失败：" + type(exc).__name__}
                    with self._lock:
                        self.state["focus_bars"][symbol] = value
            if pool_due:
                try:
                    pool = self.feeds.billboard(now, clock["latest_completed_session"])
                    pool["total_symbols"] = len(pool["rows"])
                    pool["rows"] = pool["rows"][:settings["pool_size"]]
                    with self._lock:
                        self.state["pool"] = pool
                        self.state["errors"].pop("billboard", None)
                except Exception as exc:
                    logger.warning("A-share billboard refresh failed: %s", type(exc).__name__)
                    with self._lock:
                        self.state["errors"]["billboard"] = "龙虎榜获取失败；旧榜单仅供参考，候选筛选暂停（" + type(exc).__name__ + "）"
                finally:
                    with self._lock:
                        self.state["pool_attempt_at"] = now.isoformat()
            with self._lock:
                symbols = list(dict.fromkeys([*INDEX_CODES, *settings["watchlist"],
                    *[row["symbol"] for row in self.state["pool"].get("rows", [])],
                    *[stock["symbol"] for stock in (focus or {}).get("stocks", [])],
                    *list((self._paper or {}).get('positions', {})),
                    *([s['symbol'] for s in rotation.stocks(self._paper)] if self._paper else []),
                    *[r["symbol"] for r in discovery_rows(self.state["market_news"], self.state["news"], now)
                      if settings.get("market_news_enabled")],
                    *[q["symbol"] for q in self.state["mainboard"].get("scan", {}).get("rows", [])
                      if self.state["mainboard"].get("enabled") and self.state["mainboard"].get("session_date") == now.date().isoformat()],
                    *[p["symbol"] for p in (self.state["account"] or {}).get("positions", [])]]))
            try:
                fetched = self.feeds.quotes(symbols, self.now())
                if not fetched:
                    raise ValueError("empty quote response")
                with self._lock:
                    # Replace, never silently reuse an omitted symbol as fresh.
                    self.state["quotes"] = fetched
                    self.state["errors"].pop("quotes", None)
                    for symbol, quote in fetched.items():
                        history = self.state["history"].setdefault(symbol, [])
                        if not history or history[-1]["as_of"] < quote["as_of"]:
                            history.append({"as_of": quote["as_of"], "price": quote["price"], "vwap": quote.get("vwap")})
                        self.state["history"][symbol] = history[-720:]
            except Exception as exc:
                logger.warning("A-share quote refresh failed: %s", type(exc).__name__)
                with self._lock:
                    self.state["errors"]["quotes"] = "报价请求失败；暂停新增仓位判断（" + type(exc).__name__ + "）"
                    # An outage cannot be disguised as a fresh cached quote.
                    self.state["quotes"] = {}
            with self._lock:
                completed = self.now()
                self.state["refreshed_at"] = completed.isoformat()
                self._evaluate(completed)
                self._save()
                return self._view(completed)
        finally:
            self._refresh_lock.release()

    def acknowledge(self, ids: list[str]) -> dict:
        with self._lock:
            for alert in self.state["alerts"]:
                if alert["id"] in ids and not alert["acknowledged_at"]:
                    alert["acknowledged_at"] = self.now().isoformat()
            self._save()
            return {"status": "ok"}

    def resume(self) -> dict:
        with self._refresh_lock, self._lock:
            view = self._view(self.now())
            risk = view["risk"]
            if not (risk["complete"] and view["market"]["live_window"] and risk["drawdown_pct"] < risk["warning_pct"]):
                raise ValueError(f"恢复观察需要盘中有效报价、已确认账户资料，并且回撤低于 {risk['warning_pct']:g}%")
            active_ledger = self.state["ledger"].get("monitoring_period") or self.state["ledger"]
            active_ledger["breached"] = False
            self._evaluate(self.now())
            self._save()
            return self._view(self.now())

    def public_snapshot(self) -> dict:
        view = self.snapshot()
        # Financial account input and personal drawdown never enter model context.
        public_focus = deepcopy(view["focus"])
        # The full immutable research input stays in /plan and /screening.
        # Avoid resending hundreds of public source documents to every AI call.
        public_focus.pop('manifest', None)
        public_focus.pop("risk_policy", None)
        for row in public_focus["rows"]:
            row.pop("entry_ready", None)
            row.pop("risk_status", None)
            row.pop("risk_blockers", None)
            row["reasons"] = [s for s in row["reasons"] if "账户" not in s]
            row["status"] = "公开行情观察；个人风控由本地规则判断"
            if row.get("news"):
                row["news"]["events"] = [{k: e.get(k) for k in
                    ("id", "title", "source", "url", "published_at", "relation", "analysis", "reference_only")}
                    for e in row["news"]["events"][:3]]
        public_news = {k: v for k, v in view["news"].items() if k != "stocks"}
        return {"market": view["market"], "billboard": view["billboard"],
                "focus": public_focus, "news": public_news, "mainboard": view["mainboard"],
                "market_news": {**view["market_news"], "rows": [{**r, "reasons": [s for s in r["reasons"] if "账户" not in s]}
                    for r in view["market_news"]["rows"]]},
                "monitor_enabled": view["settings"]["enabled"],
                "refreshed_at": view["refreshed_at"], "errors": view["errors"],
                "rows": [{key: row[key] for key in ("symbol", "name", "billboard_date", "net_buy_amount", "quality", "score", "candidate", "label", "flags", "five_minute_change_pct")} |
                         {"reasons": [reason[:160] for reason in row["reasons"][:2]],
                          "quote": {key: row["quote"].get(key) for key in ("price", "change_pct", "open", "high", "vwap", "amount_yuan", "turnover_pct", "as_of", "source")} if row["quote"] else None}
                         for row in view["rows"][:20]],
                "indices": [q for q in view["quotes"] if q["kind"] == "index"],
                "method": "龙虎榜成交额去重排序 + 盘中量价条件；观察分不是收益预测", "execution": "manual_only"}

    async def start(self):
        if self._task is None or self._task.done():
            self._closing = False
            self._task = asyncio.create_task(self._run(), name="ashare-observation")
            self._news_task = asyncio.create_task(self._run_news(), name="ashare-news")
            self._analysis_task = asyncio.create_task(self._run_analysis(), name="ashare-news-analysis")
            self._mainboard_task = asyncio.create_task(self._run_mainboard(), name="ashare-mainboard")
            self._radar_task = asyncio.create_task(self._run_market_news(), name="ashare-market-news")
            self._research_task = asyncio.create_task(self._run_research_outcomes(), name="ashare-research-outcomes")

    def refresh_market_news(self):
        """Daily verified universe and bounded reviews; never alters a focus plan or account."""
        if not self._radar_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                settings = deepcopy(self.state["settings"])
                monitor = news_monitoring(self.state["focus"], settings, self.now())
                if self._closing or not settings.get("market_news_enabled") or not monitor["recurring"] or not monitor["active"]:
                    return
                radar = self.state["market_news"]
                directory = deepcopy(radar.get("directory", {}))
            try:
                validated_directory(directory, self.now())
            except (ValueError, KeyError, TypeError):
                with self._lock:
                    previous = radar.get("directory_attempt_at")
                    if previous and (self.now() - datetime.fromisoformat(previous)).total_seconds() < 300:
                        return
                    radar["directory_attempt_at"] = self.now().isoformat()
                try:
                    path = self.root / "mainboard-universe.json"
                    try:
                        directory = validated_directory(json.loads(path.read_text()), self.now())
                    except (OSError, ValueError, KeyError, TypeError):
                        scan = scan_mainboard(clock=self.now)
                        cache_universe(scan, path)
                        directory = validated_directory(json.loads(path.read_text()), self.now())
                    resolver = CompanyResolver(directory["stocks"])
                    with self._lock:
                        radar.update(directory=directory, directory_error=None)
                        # Retained public reports may predate scope activation. Never relabel their source time.
                        self.state["news"]["items"] = [resolver.attach(i) for i in self.state["news"]["items"]]
                        self._evaluate(self.now())
                        self._save()
                except Exception as exc:
                    with self._lock:
                        radar["directory_error"] = "主板资讯名录获取失败，暂停新增发现：" + type(exc).__name__
                        self._save()
                    return
            with self._lock:
                candidates = deepcopy(discovery_rows(radar, self.state["news"], self.now()))
                jobs = [r for r in candidates if not radar["reviews"].get(r["symbol"], {}).get("attempted_at") or
                    (self.now() - datetime.fromisoformat(radar["reviews"][r["symbol"]]["attempted_at"])).total_seconds() >= 300][:5]
                for r in candidates:
                    if r["positive"]:
                        radar["tracked"][r["symbol"]] = self.now().isoformat()
                radar["tracked"] = {s: t for s, t in radar["tracked"].items() if t[:10] == self.now().date().isoformat()}
                radar["reviews"] = {s: v for s, v in radar["reviews"].items() if s in {r["symbol"] for r in candidates}}
            for stock in jobs:
                with self._lock:
                    if self._closing or not self.state["settings"].get("market_news_enabled") or not news_monitoring(self.state["focus"], self.state["settings"], self.now())["active"]:
                        return
                    review = radar["reviews"].get(stock["symbol"], {}) | {"attempted_at": self.now().isoformat()}
                errors = []
                try:
                    result = self.feeds.quotes([stock["symbol"]], self.now())
                    q = result.get(stock["symbol"])
                    if not q or q.get("symbol") != stock["symbol"]:
                        raise ValueError("未返回匹配报价")
                    review["quote"] = q
                except Exception as exc:
                    review.pop("quote", None)
                    errors.append("报价检查失败：" + type(exc).__name__)
                try:
                    start = (self.now() - timedelta(days=30)).date().isoformat()
                    data = self.feeds.disclosures([stock["symbol"]], start, self.now())
                    if data.get("error"):
                        raise ValueError("公告索引失败")
                    old_ids = {i["id"] for i in review.get("announcements", [])}
                    review["new_disclosure_ids"] = [i["id"] for i in data["items"] if i["id"] not in old_ids
                        and (review.get("checked_at") or i["date"] == self.now().date().isoformat())]
                    review["announcements"] = data["items"]
                    from .news import RISK_WORDS
                    review["title_flags"] = [i["title"] for i in data["items"] if RISK_WORDS.search(i["title"])][:20]
                    review["announcement_since"] = start
                except Exception as exc:
                    errors.append("公告索引失败：" + type(exc).__name__)
                try:
                    items = self.news_feeds.eastmoney(stock, [stock], self.now())
                    resolver = CompanyResolver(directory["stocks"])
                    items = [resolver.attach(i) for i in items]
                    with self._lock:
                        self.state["news"]["items"] = merge_items(self.state["news"]["items"], items, self.now())
                    review["news_checked_at"] = self.now().isoformat()
                except Exception as exc:
                    errors.append("个股新闻失败：" + type(exc).__name__)
                review.update(checked_at=self.now().isoformat(), error="；".join(errors) or None)
                with self._lock:
                    radar["reviews"][stock["symbol"]] = review
                    self._evaluate(self.now())
                    self._save()
        finally:
            self._radar_lock.release()

    async def _run_market_news(self):
        while not self._closing:
            try:
                await asyncio.to_thread(self.refresh_market_news)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("A-share market-news cycle failed")
            await asyncio.sleep(5)

    async def _run_mainboard(self):
        while not self._closing:
            try:
                await asyncio.to_thread(self.refresh_mainboard)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("A-share main-board cycle failed")
            await asyncio.sleep(5)

    def refresh_news(self):
        if not self._news_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                plan = deepcopy(self.state["focus"])
                news = deepcopy(self.state["news"])
                settings = deepcopy(self.state["settings"])
            monitor = news_monitoring(plan, settings, self.now())
            if not monitor["enabled"]:
                return
            if monitor["recurring"] and not monitor["active"]:
                return
            if not monitor["recurring"] and self.now().date().isoformat() > plan["session_date"]:
                return
            now = self.now()
            if not monitor["recurring"] and now.date().isoformat() == plan["session_date"] and now.hour * 60 + now.minute > 910:
                return
            incoming = []
            requested_window = monitor["active"]
            jobs = [("cls", 30 if requested_window else 600)] + [("eastmoney_" + s["symbol"], 300 if requested_window else 1800) for s in plan["stocks"]]
            for key, interval in jobs:
                with self._lock:
                    current_monitor = news_monitoring(self.state["focus"], self.state["settings"], self.now())
                    if not current_monitor["enabled"] or current_monitor["recurring"] and not current_monitor["active"]:
                        return
                prior = news["sources"].get(key, {})
                attempted = prior.get("attempted_at")
                if attempted and (now - datetime.fromisoformat(attempted)).total_seconds() < interval:
                    continue
                result = prior | {"attempted_at": self.now().isoformat()}
                try:
                    if key == "cls":
                        # Overnight/weekend gaps are outside the requested window. Do not
                        # force bounded pagination to span an unmonitored night.
                        checked_at = str(prior.get("checked_at", ""))
                        same_day = checked_at[:10] == self.now().date().isoformat()
                        if monitor["recurring"] and checked_at < news.get("recurring_started_at", ""):
                            same_day = False
                        items, latest = self.news_feeds.cls(plan["stocks"], self.now(), prior.get("latest_timestamp") if same_day else None)
                        result["latest_timestamp"] = latest
                    else:
                        stock = next(s for s in plan["stocks"] if key == "eastmoney_" + s["symbol"])
                        items = self.news_feeds.eastmoney(stock, plan["stocks"], self.now())
                    if settings.get("market_news_enabled"):
                        with self._lock:
                            directory = deepcopy(self.state["market_news"].get("directory", {}))
                        try:
                            validated_directory(directory, self.now())
                            resolver = CompanyResolver(directory["stocks"])
                            items = [resolver.attach(i) for i in items]
                        except (KeyError, ValueError, TypeError):
                            pass  # No guesses while the public universe is unavailable.
                    incoming.extend(items)
                    result.update(checked_at=self.now().isoformat(), error=None, count=len(items),
                                  latest_published_at=max((i["published_at"] for i in items), default=None))
                except Exception as exc:
                    result["error"] = "新闻采集失败：" + (str(exc)[:150] if isinstance(exc, ValueError) else type(exc).__name__)
                news["sources"][key] = result
                # Publish every provider result promptly; never hold the quote lock during HTTP.
                with self._lock:
                    if self.state["focus"] != plan:
                        return
                    self.state["news"]["sources"][key] = result
                    if not result.get("error"):
                        self.state["news"]["items"] = merge_items(self.state["news"]["items"], items, self.now())
                    self._evaluate(self.now())
                    self._save()
            with self._lock:
                if self.state["focus"] != plan:
                    return
                current = self.state["news"]
                current["items"] = merge_items(current["items"], incoming, self.now())
                kept = {i["id"] for i in current["items"]}
                current["analyses"] = {k: v for k, v in current["analyses"].items() if k in kept}
                self._evaluate(self.now())
                self._save()
        finally:
            self._news_lock.release()

    def analyze_pending_news(self):
        if not self._analysis_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                plan = self.state["focus"]
                monitor = news_monitoring(plan, self.state["settings"], self.now())
                if not monitor["enabled"] or monitor["recurring"] and not monitor["active"]:
                    return
                if not monitor["recurring"] and (self.now().date().isoformat() > plan["session_date"] or self.now().date().isoformat() == plan["session_date"] and self.now().hour * 60 + self.now().minute > 910):
                    return
                state = self.state["news"]
                previous = state.get("analysis_attempted_at")
                if previous and (self.now() - datetime.fromisoformat(previous)).total_seconds() < 300:
                    return
                pending = pending_items(state, self.now())
                if self.state["settings"].get("market_news_enabled"):
                    # Fresh potential catalysts and reversals share the same existing model budget.
                    pending.sort(key=lambda i: (i["published_at"][:10] != self.now().date().isoformat(),
                        not bool(CATALYST.search(i["title"] + " " + i["content"])), -datetime.fromisoformat(i["published_at"]).timestamp()))
                batch = deepcopy(pending[:12])
                if not batch:
                    return
                state.update(analysis_attempted_at=self.now().isoformat(), analysis_running=True)
                self._save()
            try:
                analyses = self.news_analyzer(batch)
                with self._lock:
                    for item in batch:
                        self.state["news"]["analyses"][item["id"]] = analyses[item["id"]] | {"version": version(item)}
                        if self.state["settings"].get("market_news_enabled"):
                            for symbol, impact in analyses[item["id"]].get("symbol_impacts", {}).items():
                                if impact == "positive" and item["published_at"][:10] == self.now().date().isoformat():
                                    self.state["market_news"]["tracked"][symbol] = self.now().isoformat()
                    self.state["news"].update(analysis_at=self.now().isoformat(), analysis_error=None)
            except Exception as exc:
                frames = traceback.extract_tb(exc.__traceback__)
                location = (Path(frames[-1].filename).name + ":" + str(frames[-1].lineno)) if frames else "unknown"
                logger.warning("A-share news model failed: %s at %s", type(exc).__name__, location)
                with self._lock:
                    self.state["news"]["analysis_error"] = "模型新闻整理未完成：" + type(exc).__name__ + "（" + location + "）"
            finally:
                with self._lock:
                    self.state["news"]["analysis_running"] = False
                    self._evaluate(self.now())
                    self._save()
        finally:
            self._analysis_lock.release()

    async def _run_news(self):
        while not self._closing:
            try:
                await asyncio.to_thread(self.refresh_news)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("A-share news cycle failed")
            await asyncio.sleep(5)

    async def _run_analysis(self):
        while not self._closing:
            try:
                await asyncio.to_thread(self.analyze_pending_news)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("A-share news analysis cycle failed")
            await asyncio.sleep(5)

    async def _run(self):
        while not self._closing:
            try:
                view = self.snapshot()
                last = view["refreshed_at"]
                monitor = news_monitoring(self.state["focus"], view["settings"], self.now())
                interval = view["settings"]["poll_seconds"] if view["market"]["live_window"] else 300 if monitor["recurring"] and monitor["active"] else 600
                due = last is None or (self.now() - datetime.fromisoformat(last)).total_seconds() >= interval
                in_window = not monitor["recurring"] or monitor["active"]
                if view["settings"]["enabled"] and due and in_window:
                    await asyncio.to_thread(self.refresh)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("A-share observation cycle failed")
            await asyncio.sleep(2)

    async def _run_research_outcomes(self):
        while not self._closing:
            try:
                await asyncio.to_thread(self.refresh_research_outcomes)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('A-share frozen research outcome collection failed')
            await asyncio.sleep(60)

    async def stop(self):
        self._closing = True
        for task in (self._task, self._news_task, self._analysis_task, self._mainboard_task, self._radar_task, self._research_task):
            if task:
                task.cancel()
        for task in (self._task, self._news_task, self._analysis_task, self._mainboard_task, self._radar_task, self._research_task):
            if not task:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = self._news_task = self._analysis_task = self._mainboard_task = self._radar_task = None
        # Cancellation of to_thread does not stop its worker. Drain the writer
        # before another server can reuse the same persisted risk ledger.
        def drain():
            with self._refresh_lock:
                pass
            with self._news_lock:
                pass
            with self._analysis_lock:
                pass
            with self._mainboard_lock:
                pass
            with self._radar_lock:
                pass
        await asyncio.to_thread(drain)


_INSTANCE = None
_INSTANCE_LOCK = threading.Lock()


def get_ashare_service() -> AshareService:
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            from src.config.paths import get_runtime_root
            _INSTANCE = AshareService(get_runtime_root() / "ashare")
        return _INSTANCE
