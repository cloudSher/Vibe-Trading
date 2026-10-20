from copy import deepcopy
from datetime import timedelta
import json

import pytest
from src.ashare import rotation,paper
from src.ashare.relay import check_relay
from tests.test_ashare_paper import setup
from tests.test_ashare_plan import TIME,bars
from tests.test_ashare_recurring import write_source,alert
from tests.test_ashare_monitor import NOW
from src.ashare.models import PaperRotationInput,PaperCandidateInput,PaperStartInput
from src.ashare.service import AshareService
from tests.test_ashare_monitor import Feeds
from tests.test_ashare_plan import plan

NEW='600001.SH'

def candidate(now):
    _,_,quotes=setup();q=deepcopy(next(iter(quotes.values())))
    q.update(symbol=NEW,name='新候选公司',as_of=now.isoformat(),price=10.13,open=10,high=10.17,vwap=10.06,
        low=9.99,change_pct=1.4,previous_close=9.99)
    event={'id':'new-item','version':'v1','title':'新候选公司公告新中标','source':'公开测试来源','url':'https://example.com/news',
        'published_at':now.replace(hour=9,minute=46,second=0).isoformat(),'snippet':'公告披露中标事项，履约存在不确定性',
        'fresh_catalyst':True,'risk_flag':False,'analysis':{'impact':'positive','summary':'模型整理，不是事实核验'},'relation':{'scope':'direct'}}
    return {'symbol':NEW,'name':'新候选公司','quote':q,'news':{'events':[event]},'review':{'checked_at':now.isoformat(),'error':None,'title_flags':[]},
        'announcements':[{'id':'ANN','title':'中标公告','date':now.date().isoformat(),'source_url':'https://example.com/notice'}]}

def frame(now):
    return {'active':True,'directory_valid':True,'source_fresh':True,'rows':[candidate(now)]}

def initialized(mode='auto'):
    s,v,q=setup();s['rotation']=rotation.configuration()|{'mode':mode}
    when=TIME.replace(hour=9,minute=46,second=30)
    raw=frame(when);b={NEW:bars(when)}
    s=rotation.evaluate(s,raw,v['rows'],b,when)
    p=s['rotation']['proposals'][NEW]
    assert not p['eligible'] and p['stock']
    s['rotation']['approvals'][NEW]={'source_hash':p['source_hash'],'at':when.isoformat()}
    return s,v,q

def ready(mode='auto'):
    s,v,q=initialized(mode)
    first=TIME.replace(hour=10,minute=0,second=30)
    s=rotation.evaluate(s,frame(first),v['rows'],{NEW:bars(first)},first)
    assert not s['rotation']['history']
    second=first+timedelta(seconds=31)
    return rotation.evaluate(s,frame(second),v['rows'],{NEW:bars(second)},second),v,q,second


def test_proposal_freezes_known_bars_before_signal_and_preserves_original_prediction():
    s,v,q=initialized();original=deepcopy(s['plan'])
    out,_,_,_=ready()
    p=out['rotation']['proposals'][NEW]
    assert p['stock']['rules']['breakout']==10.1
    assert len(out['rotation']['history'])==1 and rotation.stocks(out)[0]['symbol']==NEW
    assert out['plan']==original and not out['trades']
    assert out['rotation']['history'][0]['simulated']


@pytest.mark.parametrize('case',['unreviewed','negative','old','sector','source','directory','quote','index','archive','risk','pause','out_of_window','bars','limit','previous_close','quote_before_bar'])
def test_good_headline_cannot_remove_a_blocker(case):
    s,v,q=initialized();s['rotation']['mode']='auto';now=TIME.replace(second=30)
    raw=frame(now);kwargs={};b={NEW:bars(now)}
    if case=='unreviewed':s['rotation']['approvals']={}
    if case=='negative':raw['rows'][0]['news']['events'][0]['analysis']['impact']='negative'
    if case=='old':raw['rows'][0]['news']['events'][0]['fresh_catalyst']=False
    if case=='sector':raw['rows'][0]['news']['events'][0]['relation']['scope']='sector'
    if case=='source':raw['source_fresh']=False
    if case=='directory':raw['directory_valid']=False
    if case=='quote':raw['rows'][0]['quote']['as_of']=(now-timedelta(seconds=91)).isoformat()
    if case=='index':raw['rows'][0]['review']['error']='索引失败'
    if case=='archive':kwargs['archived_blocks']={NEW:'历史公告未解除'}
    if case=='risk':kwargs['allow_entries']=False
    if case=='pause':s['enabled']=False
    if case=='out_of_window':raw['active']=False
    if case=='bars':b={}
    if case=='limit':raw['rows'][0]['quote']['limit_up']=raw['rows'][0]['quote']['price']
    if case=='previous_close':raw['rows'][0]['quote']['previous_close']=9.5
    if case=='quote_before_bar':raw['rows'][0]['quote']['as_of']=(now-timedelta(seconds=31)).isoformat()
    s=rotation.evaluate(s,raw,v['rows'],b,now,**kwargs)
    later=now+timedelta(seconds=31)
    s=rotation.evaluate(s,raw,v['rows'],b,later,**kwargs)
    assert not s['rotation']['history']
    assert not s['rotation']['proposals'][NEW]['eligible']


def test_notify_and_confirm_modes_and_changed_source_revoke_approval():
    s,v,q,now=ready('notify')
    assert not s['rotation']['history'] and s['rotation']['proposals'][NEW]['eligible']
    s['rotation']['mode']='confirm';p=s['rotation']['proposals'][NEW]
    s['rotation']['confirmations'][NEW]=p['source_hash']
    s=rotation.evaluate(s,frame(now),v['rows'],{NEW:bars(now)},now)
    assert len(s['rotation']['history'])==1
    raw=frame(now);raw['rows'][0]['news']['events'][0]['version']='v2'
    assert rotation.dynamic_rows(s,raw,{NEW:bars(now)},now,'normal')[0]['entry_ready'] is False


def test_rotation_queues_t1_old_holding_and_waits_for_funds():
    s,v,q=initialized();s=paper.advance(s,v,q,TIME)
    old=next(iter(s['positions']));assert s['positions'][old]['quantity']>0
    now=TIME+timedelta(seconds=30)
    s=rotation.evaluate(s,frame(now),v['rows'],{NEW:bars(now)},now)
    now+=timedelta(seconds=31);s=rotation.evaluate(s,frame(now),v['rows'],{NEW:bars(now)},now)
    assert s['positions'][old]['pending_exit']
    # A watch-slot change is not a sale or a new fill.
    assert len(s['trades'])==1 and s['trades'][0]['side']=='buy'
    rows=paper.snapshot(s,q,now)['rows']
    assert {x['symbol'] for x in rows}=={old,NEW}


def test_cooling_and_daily_cap_avoid_repeated_switches():
    s,v,q,now=ready('notify');r=s['rotation'];r['mode']='auto'
    r['history']=[{'at':(now-timedelta(minutes=5)).isoformat()}]
    s=rotation.evaluate(s,frame(now),v['rows'],{NEW:bars(now)},now)
    assert len(s['rotation']['history'])==1
    r=s['rotation'];r['history']=[{'at':now.replace(hour=9,minute=0).isoformat()},{'at':now.replace(hour=9,minute=1).isoformat()}]
    s=rotation.evaluate(s,frame(now),v['rows'],{NEW:bars(now)},now)
    assert len(s['rotation']['history'])==2


def test_public_events_exclude_funds_and_virtual_fill_is_explicit():
    s,v,q,now=ready()
    events=rotation.notifications(s,now)
    event=next(x for x in events if x['key'].startswith('paper_switch_'))
    assert event['simulation'] and '模拟' in event['message'] and '真实成交' not in event['message']
    public=rotation.public_summary(s)
    serialized=json.dumps(public|{'events':events},ensure_ascii=False)
    assert all(k not in serialized for k in ['initial_cash','quantity','cost_total','900000'])


def test_unmarked_paper_alert_is_not_forwarded_and_same_event_deduplicates(tmp_path):
    a=alert(key='paper_fill_600100.SH_fixture',message='纯模拟成交，不是券商成交')
    source=write_source(tmp_path,alerts=[a])
    assert not any(x['key'].startswith('paper_') for x in check_relay(tmp_path,recurring=True,now=NOW)['alerts'])
    a['simulation']=True;source['alerts']=[a];(tmp_path/'relay.json').write_text(json.dumps(source))
    first=check_relay(tmp_path,recurring=True,now=NOW)
    assert any(x['key'].startswith('paper_') for x in first['alerts'])
    assert not check_relay(tmp_path,recurring=True,now=NOW)['notify']


def test_rotation_configuration_and_stale_review_preserve_real_fields(tmp_path):
    service=AshareService(tmp_path,feeds=Feeds(),clock=lambda:TIME.replace(hour=8))
    service.publish_focus(plan());h=service.state['focus']['content_hash']
    service.start_paper(PaperStartInput(expected_plan_hash=h))
    before=deepcopy(service.state)
    service.set_paper_rotation(PaperRotationInput(expected_plan_hash=h,mode='auto'))
    assert service._paper['rotation']['mode']=='auto' and service.state==before
    assert json.loads((tmp_path/'relay.json').read_text())['paper_rotation']['mode']=='auto'
    with pytest.raises(ValueError,match='身份'):
        service.set_paper_rotation(PaperRotationInput(expected_plan_hash='0'*64,mode='auto'))
    with pytest.raises(ValueError,match='来源已改变'):
        service.paper_candidate(PaperCandidateInput(symbol=NEW,source_hash='0'*64,action='review'))
    assert service.state==before
    restored=AshareService(tmp_path,feeds=Feeds(),clock=lambda:TIME.replace(hour=8))
    assert restored._paper['rotation']['mode']=='auto'


def test_source_order_changes_do_not_revoke_an_identical_review():
    row=candidate(TIME)
    row['announcements'].append({'id':'ANN2','title':'公司公告','date':TIME.date().isoformat(),'source_url':'https://example.com/2'})
    same=deepcopy(row);same['announcements'].reverse()
    assert rotation.source_fingerprint(row)==rotation.source_fingerprint(same)


def test_candidate_status_change_gets_new_notice_without_repeating_unchanged_state():
    s,v,q,now=ready('notify')
    first=rotation.notifications(s,now)[0]
    assert first['key']==rotation.notifications(s,now)[0]['key']
    p=s['rotation']['proposals'][NEW];p['eligible']=False;p['blockers']=['新报价失效']
    second=rotation.notifications(s,now)[0]
    assert first['key']!=second['key'] and '新报价失效' in second['message']


def test_activated_candidate_fills_only_in_separate_virtual_reducer():
    s,v,q,now=ready()
    raw=frame(now);q[NEW]=raw['rows'][0]['quote']
    rows=rotation.dynamic_rows(s,raw,{NEW:bars(now)},now,'normal')
    assert rows[0]['entry_ready']
    filled=paper.advance(s,{'rows':rows},q,now)
    assert len(filled['trades'])==1 and filled['trades'][0]['symbol']==NEW
    assert filled['trades'][0]['simulation'] and filled['cash']>=filled['initial_cash']*.5
    assert filled['positions'][NEW]['sellable_date']>now.date().isoformat()
    assert s['trades']==[] and s['positions']=={}


def test_active_virtual_slot_can_review_changed_sources_without_replacing_itself():
    s,v,q,now=ready()
    raw=frame(now);raw['rows'][0]['news']['events'][0]['version']='v2'
    changed=rotation.evaluate(s,raw,v['rows'],{NEW:bars(now)},now)
    p=changed['rotation']['proposals'][NEW]
    assert p['active_slot'] and not p['reviewed'] and NEW in changed['rotation']['ranking']
    assert len(changed['rotation']['history'])==1
    assert not rotation.dynamic_rows(changed,raw,{NEW:bars(now)},now,'normal')[0]['entry_ready']


def test_paper_alerts_with_old_source_or_wrong_plan_become_inactive(tmp_path):
    s,v,q,now=ready('notify')
    service=AshareService(tmp_path,feeds=Feeds(),clock=lambda:now)
    service.publish_focus(plan());service._paper=s
    p=s['rotation']['proposals'][NEW]
    event=rotation.notifications(s,now)[0]
    service.state['alerts']=[alert(now,key=event['key'],simulation=True,plan_identity=s['plan_identity'],rotation_proposal=event['rotation_proposal'])]
    assert service.snapshot()['alerts'][0]['active']
    p['source_hash']='changed'
    assert not service.snapshot()['alerts'][0]['active']
    service.state['alerts'][0]['key']='paper_fill_test'
    service.state['alerts'][0]['plan_identity']={**s['plan_identity'],'version':999}
    assert not service.snapshot()['alerts'][0]['active']
