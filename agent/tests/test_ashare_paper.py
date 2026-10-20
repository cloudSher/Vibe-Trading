from copy import deepcopy
from datetime import timedelta
import json

import pytest
from src.ashare import paper
from src.ashare.models import PaperStartInput
from src.ashare.plan import digest
from src.ashare.service import AshareService
from tests.test_ashare_plan import TIME, plan, view
from tests.test_ashare_monitor import Feeds

POLICY={'warning_pct':12,'restrict_pct':16,'max_drawdown_pct':20}

def setup():
    p=plan().model_dump(mode='json');p.update(published_at=TIME.replace(hour=8).isoformat(),content_hash=digest(p))
    state=paper.create(p,900000,POLICY,TIME.replace(hour=8))
    v=view(); q=deepcopy(v['rows'][0]['quote']);q.update(limit_up=10.99,limit_down=8.99)
    return state,v,{q['symbol']:q}


def test_only_current_evidence_creates_virtual_fill_with_lots_costs_and_t1():
    s,v,q=setup();original=deepcopy(s)
    out=paper.advance(s,v,q,TIME);symbol=next(iter(q));p=out['positions'][symbol]
    assert s==original
    assert p['quantity']%100==0 and p['cost_total']<=90000
    assert out['cash']>=810000 and p['entry_price']>q[symbol]['price']
    assert p['sellable_date']=='2026-10-21' and len(out['trades'])==1
    assert p['evidence']['retest_at']==v['rows'][0]['price_evidence']['retest_at']
    repeated=paper.advance(out,v,q,TIME)
    assert repeated['trades']==out['trades']


@pytest.mark.parametrize('case',['preopen','stale','news','bars','paused','mismatch','old_signal','expired','limit','risk'])
def test_no_virtual_fill_for_unmet_or_unverifiable_conditions(case):
    s,v,q=setup();now=TIME;kwargs={};symbol=next(iter(q))
    if case=='preopen':now=TIME.replace(hour=9,minute=20)
    if case=='stale':q[symbol]['as_of']=(TIME-timedelta(minutes=2)).isoformat()
    if case=='news':v['rows'][0].update(entry_ready=False,risk_blockers=['未核验公告'],reasons=['未核验公告'])
    if case=='bars':v['rows'][0]['price_evidence']={}
    if case=='paused':s['enabled']=False
    if case=='mismatch':kwargs['plan_matches']=False
    if case=='old_signal':s['created_at']=TIME.isoformat()
    if case=='expired':now=TIME+timedelta(days=1)
    if case=='limit':q[symbol]['limit_up']=q[symbol]['price']
    if case=='risk':s['risk_latched']=True
    out=paper.advance(s,v,q,now,**kwargs)
    assert not out['trades'] and out['cash']==900000


def test_same_day_invalidation_remains_pending_next_session_exits_without_refill():
    s,v,q=setup();s=paper.advance(s,v,q,TIME);symbol=next(iter(q))
    now=TIME+timedelta(minutes=1);q[symbol].update(price=10.05,as_of=now.isoformat())
    blocked=paper.advance(s,v,q,now)
    assert blocked['positions'][symbol]['pending_exit'] and len(blocked['trades'])==1
    assert paper.snapshot(blocked,q,now)['risk']['holdings'][0]['available_quantity']==0
    next_day=now+timedelta(days=1);q[symbol]['as_of']=next_day.isoformat()
    sold=paper.advance(blocked,v,q,next_day)
    assert not sold['positions'] and len(sold['trades'])==2
    assert sold['trades'][-1]['side']=='sell' and sold['trades'][-1]['realized_pnl']<0
    assert sold['cash']<900000


def test_missing_holding_quote_is_unknown_and_limit_down_does_not_pretend_exit():
    s,v,q=setup();s=paper.advance(s,v,q,TIME);symbol=next(iter(q))
    m=paper.metrics(s,{},TIME)
    assert not m['complete'] and m['equity'] is None and m['pnl'] is None
    s['positions'][symbol]['pending_exit']={'reason':'测试退出','at':TIME.isoformat()}
    now=TIME+timedelta(days=1);q[symbol].update(price=8.99,as_of=now.isoformat())
    out=paper.advance(s,v,q,now)
    assert out['positions'] and len(out['trades'])==1


def test_equity_drawdown_latches_and_five_budgets_keep_cash_reserve():
    s,v,q=setup();stocks=[];rows=[];quotes={}
    for i in range(5):
        symbol=f'60010{i}.SH';stock=deepcopy(s['plan']['stocks'][0]);stock['symbol']=symbol;stocks.append(stock)
        row=deepcopy(v['rows'][0]);row['symbol']=symbol;row['quote']['symbol']=symbol;rows.append(row)
        quotes[symbol]=deepcopy(next(iter(q.values())))|{'symbol':symbol}
    s['plan']['stocks']=stocks;v['rows']=rows
    out=paper.advance(s,v,quotes,TIME)
    assert len(out['positions'])==5 and out['cash']>=450000
    s=out;s['peak_equity']=1200000
    out=paper.advance(s,v,quotes,TIME)
    assert out['risk_latched']
    assert all(p.get('pending_exit') for p in out['positions'].values())


def test_service_storage_is_separate_restart_safe_idempotent_and_never_changes_real_fields(tmp_path):
    svc=AshareService(tmp_path,feeds=Feeds(),clock=lambda:TIME.replace(hour=8))
    svc.state['settings'].update(warning_drawdown_pct=12,restrict_drawdown_pct=16,max_drawdown_pct=20)
    svc.publish_focus(plan());before=deepcopy(svc.state)
    value=PaperStartInput(expected_plan_hash=svc.state['focus']['content_hash'],initial_cash=900000)
    first=svc.start_paper(value);again=svc.start_paper(value)
    assert first==again and svc.state==before
    assert first['portfolio']['cash']==900000 and first['portfolio']['risk']['pnl']==0
    restored=AshareService(tmp_path,feeds=Feeds(),clock=lambda:TIME.replace(hour=8))
    assert restored.paper_snapshot()==first
    assert not restored.paper_snapshot()['portfolio']['trades']
    with pytest.raises(ValueError,match='覆盖'):
        restored.start_paper(value.model_copy(update={'initial_cash':800000}))
    restored.set_paper_status(False)
    assert restored.state==before and not restored.paper_snapshot()['portfolio']['enabled']
    (tmp_path/'paper-portfolio.json').write_text('{bad')
    bad=AshareService(tmp_path,feeds=Feeds(),clock=lambda:TIME)
    assert bad.paper_snapshot()['error']
    with pytest.raises(ValueError):bad.start_paper(value)
    assert (tmp_path/'paper-portfolio.json').read_text()=='{bad'


def test_create_cannot_backfill_expired_plan_or_accept_changed_hash():
    s,_,_=setup()
    with pytest.raises(ValueError,match='到期'):paper.create(s['plan'],900000,POLICY,TIME+timedelta(days=1))
    s['plan']['stocks'][0]['rules']['breakout']=20
    with pytest.raises(ValueError,match='有效'):paper.create(s['plan'],900000,POLICY,TIME)
