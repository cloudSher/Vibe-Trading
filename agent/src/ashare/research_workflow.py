"""Local public-research CLI: capture -> prepare -> verify originals -> rank -> stage.

Publishing is an authenticated UI action. No account or brokerage file is read.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from .calendar import SHANGHAI, is_session, prior_session
from .feeds import PublicFeeds, _get
from .market_news import CompanyResolver
from .models import FocusPlan
from .news_feeds import article
from .plan import digest
from .screening import VERSION, build_audit, evaluate_outcome, event_type, score_candidate
import re


def write_public(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))
    os.replace(temporary,path)


def capture_public(target, root, output):
    now=datetime.now(SHANGHAI)
    target_day=datetime.fromisoformat(target).date()
    if is_session(target_day) is not True or now.date() >= target_day:
        raise ValueError('只在已核验目标交易日前冻结研究；不能回填预测')
    reference=str(prior_session(target_day))
    directory=json.loads((root/'mainboard-universe.json').read_text())
    if (directory['as_of'][:10]!=reference or len(directory['stocks'])!=directory['total']
        or len({s['symbol'] for s in directory['stocks']})!=directory['total']):
        raise ValueError('需要上一交易日完整、无重复的主板名录；请先运行主板扫描')
    resolver=CompanyResolver(directory['stocks'])
    start=datetime.fromisoformat(reference+'T15:00:00+08:00')
    result={'target_date':target,'reference_date':reference,'cutoff':now.isoformat(),'directory':directory,
        'news':[],'announcements':[],'quotes':{},'errors':{},'coverage':{}}
    cursor=int(now.timestamp());covered=False
    for page in range(30):
        params={'app':'CailianpressWeb','category':'','last_time':cursor,'os':'web','refresh_type':'1','rn':'20','sv':'8.4.6'}
        params['sign']=hashlib.md5(hashlib.sha1(urlencode(params).encode()).hexdigest().encode()).hexdigest()
        raw=_get('https://www.cls.cn/v1/roll/get_roll_list',params=params,
            headers={'Referer':'https://www.cls.cn/telegraph','User-Agent':'Mozilla/5.0'}).json()
        rows=(raw.get('data') or {}).get('roll_data')
        if not isinstance(rows,list):raise ValueError('快讯来源结构无效')
        if not rows:covered=True;break
        for r in rows:
            stamp=datetime.fromtimestamp(int(r['ctime']),SHANGHAI)
            if start<=stamp<=now:result['news'].append(resolver.attach(article('财联社',r.get('title'),r.get('content'),
                stamp.isoformat(),'https://www.cls.cn/detail/'+str(r['id']),now,[])))
        oldest=min(int(r['ctime']) for r in rows)
        if oldest<=start.timestamp():covered=True;break
        if oldest>=cursor:raise ValueError('快讯分页游标不前进')
        cursor=oldest-1
    result['coverage'].update(cls_pages=page+1,cls_interval_complete=covered,cls_items=len(result['news']),cls_from=start.isoformat())
    for page in range(1,31):
        raw=_get('https://np-anotice-stock.eastmoney.com/api/security/ann',params={'sr':-1,'page_size':100,
            'page_index':page,'ann_type':'A','begin_time':reference,'end_time':target}).json()
        data=raw.get('data') or {}
        if raw.get('success')!=1 or not isinstance(data.get('list'),list):raise ValueError('公告分页无效')
        total=int(data['total_hits'])
        for a in data['list']:
            try:stamp=datetime.fromisoformat(str(a.get('display_time') or a.get('eiTime'))[:19]).replace(tzinfo=SHANGHAI)
            except ValueError:continue
            if not start<=stamp<=now:continue
            symbols=[c['stock_code']+('.SH' if str(c.get('market_code'))=='1' else '.SZ') for c in a.get('codes',[])]
            symbols=[s for s in symbols if s in resolver.stocks]
            if symbols:result['announcements'].append({'id':a['art_code'],'title':a['title'],'symbols':symbols,
                'published_at':stamp.isoformat(),'document_date':str(a.get('notice_date',''))[:10],
                'url':f"https://data.eastmoney.com/notices/detail/{symbols[0][:6]}/{a['art_code']}.html"})
        if page*100>=total:break
    result['coverage'].update(announcement_pages=page,announcement_index_total=total,announcement_pages_complete=page*100>=total,
        mainboard_evening_announcements=len(result['announcements']))
    feeds=PublicFeeds();symbols=[s['symbol'] for s in directory['stocks']]
    for i in range(0,len(symbols),60):
        try:result['quotes'].update(feeds.quotes(symbols[i:i+60],datetime.now(SHANGHAI)))
        except Exception as exc:result['errors'][f'quote_batch_{i//60}']=type(exc).__name__
        if i%600==0:print(f'报价覆盖 {i}/{len(symbols)}',flush=True)
    result['coverage'].update(requested_quotes=len(symbols),received_quotes=len(result['quotes']),
        reference_date_quotes=sum(q['as_of'][:10]==reference for q in result['quotes'].values()))
    result['completed_at']=datetime.now(SHANGHAI).isoformat()
    write_public(output,result)
    return result


def prepare_inputs(capture):
    names={s['symbol']:s['name'] for s in capture['directory']['stocks']};pool={}
    def add(symbol,evidence,origin):
        if symbol not in names:return
        c=pool.setdefault(symbol,{'symbol':symbol,'name':names[symbol],'quote':capture['quotes'].get(symbol),
            'evidence':[],'origins':[],'review':{}})
        if evidence and not any(e['url']==evidence['url'] for e in c['evidence']):c['evidence'].append(evidence)
        if origin not in c['origins']:c['origins'].append(origin)
    # Reverse/uncertain news also stays in the pool and cannot be hidden by a positive headline.
    for n in capture['news']:
        for r in n['relations']:
            if r['scope']=='direct':add(r['symbol'],{'title':n['title'],'source':n['source'],'url':n['url'],
                'published_at':n['published_at'],'relation':'direct','source_text':n['content']},'news')
    for a in capture['announcements']:
        if event_type(a['title'])=='none':continue
        for s in a['symbols']:add(s,{'title':a['title'],'source':'公司公告索引','url':a['url'],
            'published_at':a['published_at'],'relation':'direct','source_text':'','announcement_id':a['id']},'announcement')
    for q in sorted(capture['quotes'].values(),key=lambda q:q['change_pct'],reverse=True)[:60]:add(q['symbol'],None,'price_leader')
    coverage=capture['coverage']
    count=coverage['reference_date_quotes']
    return {'run_id':'research-'+capture['target_date']+'-v1','method_version':VERSION,
        'target_date':capture['target_date'],'reference_date':capture['reference_date'],'cutoff':capture['cutoff'],
        'coverage':{'directory_verified':True,'universe_count':capture['directory']['total'],'reference_quote_count':count,
            'reference_quote_coverage':count/capture['directory']['total'],**coverage,
            'note':'完整名录和报价不表示全部新闻无遗漏；快讯、公告及原文核验范围分别披露。'},
        'candidates':list(pool.values())}


def enrich_public_inputs(inputs, *, analyzer=None, limit=40, progress=None):
    """Local backend model reads public evidence; it never clears risk gates."""
    from .news_analysis import analyze_news
    from copy import deepcopy
    value=deepcopy(inputs);analyzer=analyzer or analyze_news
    ranked=sorted(value['candidates'],key=lambda c:-score_candidate(c,value['reference_date'],
        datetime.fromisoformat(value['cutoff']))['score'])
    queue=[c for c in ranked if c['evidence']][:limit]
    documents=0
    for index,c in enumerate(queue):
        for e in [e for e in c['evidence'] if e.get('announcement_id')][:3]:
            r=_get('https://np-cnotice-stock.eastmoney.com/api/content/ann',params={
                'art_code':e['announcement_id'],'client_source':'web','page_index':1}).json()
            if r.get('success')!=1:raise ValueError('公司公告原文获取失败，保留旧计划')
            text=r['data'].get('notice_content','')
            if not text:raise ValueError('公司公告正文为空')
            e['source_text']=text;e['pdf_url']=r['data'].get('attach_url');documents+=1
        if progress:progress(f'公开原文 {index+1}/{len(queue)}')
    for offset in range(0,len(queue),8):
        batch=queue[offset:offset+8];items=[]
        for c in batch:
            source='\n\n'.join(e.get('source_text','') for e in c['evidence'])[:12000]
            items.append({'id':c['symbol'],'title':c['name']+'公开催化及反向证据','content':source,
                'source':'已抓取的公开公告与快讯','published_at':value['cutoff'],
                'relations':[{'symbol':c['symbol'],'name':c['name'],'scope':'direct'}]})
        analyses=analyzer(items)
        for c in batch:
            a=analyses[c['symbol']];text=a['evidence']
            if not any(text in e.get('source_text','') for e in c['evidence']):
                raise ValueError('模型摘录不在对应原文中，停止生成草案')
            titles=' '.join(e['title'] for e in c['evidence'])
            from .screening import EVENT_POINTS
            kind=max((event_type(e['title']) for e in c['evidence']),key=lambda k:EVENT_POINTS[k],default='none')
            if re.search('采购.*GPU|GPU.*采购|采购合同|项目投资协议',titles):kind='capacity'
            impact=a['symbol_impacts'][c['symbol']]
            c['review']={'original_reviewed':True,'evidence_quote':text,'summary':a['summary'],
                'event_type':kind,'novelty':'known' if re.search('进展|累计|前三季度新接订单',titles) else 'unknown',
                'risk_penalty':25 if impact=='negative' else 15 if impact=='uncertain' else 5,
                'risk_points':[a['summary'],'模型整理不是事实核验；公告与账户限制仍独立有效'],
                'annotation_source':'local_model_public_excerpt',
                'review_scope':'已取得公开原文并逐字校验摘录；模型方向不代表事实核验，发布后仍暂停公告确认'}
            if impact in {'negative','neutral'}:c['review']['exclude_reason']='模型未确认直接正向催化，保留原文待核验：'+a['summary']
        if progress:progress(f'模型整理 {min(offset+8,len(queue))}/{len(queue)}')
    value['coverage'].update(primary_documents_fetched=documents,substantively_reviewed_candidates=len(queue),
        theme_status='未取得全候选同口径行业映射，板块分统一为0，不让模型推断公司订单')
    return value


def plan_from_audit(audit, now=None):
    now=now or datetime.now(SHANGHAI);stocks=[]
    for symbol in audit['selected_symbols']:
        c=next(r for r in audit['rows'] if r['symbol']==symbol);q=c['quote'];review=c['review']
        if not q.get('vwap'):raise ValueError('入选股参考VWAP缺失，不能生成价位')
        risks=list(review.get('risk_points',[]))
        if (q.get('amount_yuan') or 0)<100_000_000:risks.append('参考日成交额不足1亿元，目标日须重新放量达到门槛')
        if q['price']<q['vwap']:risks.append('参考日收盘未站稳VWAP，目标日需重新收复')
        if abs(q['price']-(q.get('limit_up') or 0))<.005:risks.append('参考日已涨停，次日一字板或高开达到3%不追')
        sources=[e for e in c['evidence'] if e.get('announcement_id') and e.get('source_text')]
        stocks.append({'symbol':symbol,'name':c['name'],'reference_close':q['price'],'reference_vwap':q['vwap'],
            'disclosure_status':'pending','disclosure_summary':'主要催化原文及摘录已保存；近30日其他公告、反向消息和账户风险仍须核验，模型整理不解除限制。',
            'reviewed_ids':review.get('reviewed_announcement_ids',[]),
            'source_urls':list(dict.fromkeys(e['url'] for e in [*sources,*c['evidence']]))[:30],
            'reason':review.get('summary') or c['decision'],'risk_points':risks,
            'catalyst_published_at':max((e['published_at'] for e in sources or c['evidence'])),
            'rules':{'breakout':round(q['high'],2),'hold':round(q['high'],2),
                'invalid_below':min(round(q['vwap'],2),round(q['high'],2)),
                'strong_invalid_below':min(round(q['low'],2),round(q['vwap'],2)),
                'max_vwap_deviation_pct':2,'breakout_volume_ratio':1.5,'retest_tolerance_pct':.2}})
    return FocusPlan.model_validate({'plan_id':'mainboard-'+audit['target_date'],'version':1,
        'enabled':True,'news_enabled':True,'session_date':audit['target_date'],'reference_date':audit['reference_date'],
        'review_since':str(datetime.fromisoformat(audit['reference_date']).date()-timedelta(days=30)),
        'reviewed_at':now.isoformat(),'research_cutoff':audit['cutoff'],'rules_version':'breakout-retest-m5-v1',
        'max_open_gap_pct':3,'stocks':stocks,'screening':audit})


def generate_local_draft(root, target, output, progress=None):
    if progress:progress('回补快讯、公告与主板参考报价')
    capture=capture_public(target,root,output/'public-capture.json')
    inputs=prepare_inputs(capture)
    version=1
    relay_path=root/'relay.json'
    if relay_path.exists():
        focus=json.loads(relay_path.read_text()).get('focus') or {}
        if focus.get('plan_id')=='mainboard-'+target:version=int(focus['version'])+1
    inputs['run_id']='research-'+target+'-v'+str(version)
    write_public(output/'candidate-inputs.json',inputs)
    enriched=enrich_public_inputs(inputs,progress=progress);write_public(output/'reviewed-candidate-inputs.json',enriched)
    audit=build_audit(enriched);write_public(output/'ranking.json',audit)
    plan=FocusPlan.model_validate(plan_from_audit(audit).model_dump(mode='json')|{'version':version})
    write_public(output/'plan.json',plan.model_dump(mode='json'))
    write_public(root/'research-draft.json',plan.model_dump(mode='json'))
    return {'target_date':target,'candidate_count':len(audit['rows']),'selected_symbols':audit['selected_symbols'],
            'content_hash':digest(plan.model_dump(mode='json')),'published':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['capture','prepare','enrich','rank','stage','review','generate'])
    parser.add_argument('--root',type=Path,default=Path('.runtime/ashare'))
    parser.add_argument('--target');parser.add_argument('--input',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.action=='generate':
        if not args.target or not args.output:parser.error('generate需要--target与--output目录')
        print(json.dumps(generate_local_draft(args.root,args.target,args.output,progress=lambda x:print(x,flush=True)),ensure_ascii=False));return
    if args.action=='capture':
        if not args.target or not args.output:parser.error('capture需要--target与--output')
        result=capture_public(args.target,args.root,args.output)
        print(json.dumps(result['coverage'],ensure_ascii=False));return
    if not args.input:parser.error('需要--input')
    value=json.loads(args.input.read_text())
    if args.action=='prepare':result=prepare_inputs(value)
    elif args.action=='enrich':result=enrich_public_inputs(value,progress=lambda x:print(x,flush=True))
    elif args.action=='rank':result=build_audit(value)
    elif args.action=='stage':
        plan=FocusPlan.model_validate(value)
        if plan.screening is None:raise ValueError('发布草案必须附冻结研究')
        write_public(args.root/'research-draft.json',plan.model_dump(mode='json'))
        print(json.dumps({'staged':True,'plan_id':plan.plan_id,'version':plan.version,
            'content_hash':digest(plan.model_dump(mode='json')),'requires_authenticated_publish':True},ensure_ascii=False));return
    else:
        plan=FocusPlan.model_validate(value)
        now=datetime.now(SHANGHAI)
        if now.date()!=plan.session_date or now.hour<15:raise ValueError('仅在目标日收盘后读取同日结果，禁止回填或未来数据')
        quotes=PublicFeeds().quotes([r['symbol'] for r in plan.screening['rows']],now)
        result=evaluate_outcome(plan.screening,{s:q for s,q in quotes.items() if q['as_of'][11:16]>='15:00'})
    if not args.output:parser.error('需要--output')
    write_public(args.output,result)
    print(json.dumps({'output':str(args.output),'candidate_count':len(result.get('candidates',result.get('rows',[])))},ensure_ascii=False))


if __name__=='__main__':main()
