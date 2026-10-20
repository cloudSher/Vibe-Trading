from copy import deepcopy
from datetime import datetime
import json

import pytest

from src.ashare.models import FocusPlan
from src.ashare.plan import digest
from src.ashare.screening import VERSION, build_audit, event_type, evaluate_outcome, validate_audit
from src.ashare.service import AshareService
from tests.test_ashare_plan import TIME, plan
from tests.test_ashare_monitor import Feeds


def inputs():
    candidates = []
    for i in range(8):
        symbol = f'60010{i}.SH'
        text = '公司收到客户项目中标通知，尚待正式签约。'
        candidates.append({'symbol': symbol, 'name': f'测试公司{i}',
            'quote': {'symbol':symbol,'price':10+i*.1,'previous_close':10,'open':10,
                'high':11,'low':9.9,'vwap':10,'amount_yuan':600_000_000,
                'turnover_pct':3,'as_of':'2026-10-19T15:00:00+08:00'},
            'evidence':[{'title':'公司项目中标公告','source_text':text,'url':'https://example.com/'+str(i),
                'relation':'direct','published_at':'2026-10-19T18:00:00+08:00'}],
            'review': {'original_reviewed':True,'evidence_quote':'公司收到客户项目中标通知',
                'novelty':'new','risk_points':['正式合同未签订']}})
    return {'run_id':'research-test','method_version':VERSION,'target_date':'2026-10-20',
        'reference_date':'2026-10-19','cutoff':'2026-10-19T23:00:00+08:00',
        'coverage':{'directory_verified':True,'reference_quote_coverage':.99},'candidates':candidates}


def research_plan():
    audit = build_audit(inputs())
    p = plan().model_dump(mode='json')
    p['research_cutoff'] = audit['cutoff']
    p['reviewed_at'] = '2026-10-19T23:01:00+08:00'
    p['screening'] = audit
    original = p['stocks'][0]
    p['stocks'] = [deepcopy(original) | {'symbol':s,'name':next(c['name'] for c in audit['rows'] if c['symbol']==s)} for s in audit['selected_symbols']]
    return FocusPlan.model_validate(p)


def test_freeze_reproducible_ranking_and_rejection_reasons():
    value = inputs()
    audit = build_audit(value)
    assert validate_audit(audit) == build_audit(deepcopy(value))
    assert len(audit['selected_symbols']) == 5
    assert all(r['decision'] and r['score_parts'] for r in audit['rows'])
    assert all(not r['entry_ready'] for r in audit['rows'])
    assert len(audit['rows']) == 8


@pytest.mark.parametrize('field', ['score','decision','selected','rank'])
def test_changed_frozen_result_is_rejected(field):
    audit=build_audit(inputs());audit['rows'][0][field]='tampered'
    with pytest.raises(ValueError):validate_audit(audit)


def test_future_evidence_and_partial_market_fail_closed():
    value=inputs();value['candidates'][0]['evidence'][0]['published_at']='2026-10-20T09:00:00+08:00'
    with pytest.raises(ValueError,match='截止'):build_audit(value)
    value=inputs();value['coverage']['reference_quote_coverage']=.5
    with pytest.raises(ValueError,match='90%'):build_audit(value)


def test_review_requires_traceable_original_and_cannot_pad_five():
    value=inputs();value['candidates'][0]['review']['evidence_quote']='原文没有的结论'
    with pytest.raises(ValueError,match='逐字'):build_audit(value)
    value=inputs()
    for c in value['candidates'][:4]:c['review']['original_reviewed']=False
    with pytest.raises(ValueError,match='五只'):build_audit(value)


def test_administrative_documents_and_procurement_are_not_sales_orders():
    assert event_type('关于签订募集资金监管协议的公告') == 'none'
    assert event_type('关于召开2026年三季度业绩说明会') == 'none'
    assert event_type('暂缓推进收购股权') == 'none'
    assert event_type('签订8.39亿元GPU采购合同') == 'capacity'
    assert event_type('收到中国邮政项目中标通知书') == 'order'


def test_plan_and_audit_cannot_diverge_and_old_hash_remains_valid():
    p=research_plan().model_dump(mode='json')
    p['stocks'].reverse()
    with pytest.raises(ValueError,match='入选顺序'):FocusPlan.model_validate(p)
    old=plan().model_dump(mode='json');with_none=digest(old)
    old.pop('screening');assert digest(old)==with_none


def test_missing_or_wrong_date_outcomes_are_not_zero_returns():
    audit=build_audit(inputs())
    result=evaluate_outcome(audit,{})
    assert result['selected_mean_day_change_pct'] is None
    assert result['candidate_coverage']==0
    quotes={c['symbol']:c['quote'] for c in audit['rows']}
    assert evaluate_outcome(audit,quotes)['selected_coverage']==0
    quotes={s:q|{'as_of':'2026-10-20T15:00:00+08:00','limit_up':11} for s,q in quotes.items()}
    result=evaluate_outcome(audit,quotes)
    assert result['selected_coverage']==5 and result['candidate_coverage']==8
    assert result['selected_mean_open_to_close_pct'] is not None


def test_staged_publish_protects_account_thresholds_and_keeps_public_journal(tmp_path):
    service=AshareService(tmp_path,clock=lambda:TIME,feeds=Feeds())
    p=research_plan();(tmp_path/'research-draft.json').write_text(p.model_dump_json())
    protected=deepcopy({k:service.state[k] for k in ('account','ledger','settings')})
    with pytest.raises(ValueError,match='改变'):service.publish_research_draft('a'*64)
    expected=digest(p.model_dump(mode='json'))
    service.publish_research_draft(expected)
    assert {k:service.state[k] for k in protected}==protected
    snap=service.research_snapshot()
    assert snap['published']['content_hash']==p.screening['content_hash']
    journal=list((tmp_path/'public-signal-journal').glob('*.jsonl'))
    assert journal and len(journal[0].read_text().splitlines())==5
    service._save();assert len(journal[0].read_text().splitlines())==5
    assert 'cash' not in journal[0].read_text() and 'quantity' not in journal[0].read_text()


def test_local_model_pipeline_is_public_only_and_never_clears_announcement_review():
    from src.ashare.research_workflow import enrich_public_inputs, plan_from_audit
    def analyzer(items):
        assert 'account' not in json.dumps(items) and 'cash' not in json.dumps(items)
        return {i['id']:{'symbol_impacts':{i['id']:'positive'},'summary':'中标事实，合同尚待签订',
            'evidence':'公司收到客户项目中标通知'} for i in items}
    value=enrich_public_inputs(inputs(),analyzer=analyzer)
    p=plan_from_audit(build_audit(value),now=datetime.fromisoformat('2026-10-19T23:01:00+08:00'))
    assert all(s.disclosure_status=='pending' and not s.reviewed_ids for s in p.stocks)


def test_local_model_bad_evidence_stops_generation_instead_of_guessing():
    from src.ashare.research_workflow import enrich_public_inputs
    def analyzer(items):
        return {i['id']:{'symbol_impacts':{i['id']:'positive'},'summary':'invented',
            'evidence':'原文从未出现的承诺'} for i in items}
    with pytest.raises(ValueError,match='摘录'):enrich_public_inputs(inputs(),analyzer=analyzer)
