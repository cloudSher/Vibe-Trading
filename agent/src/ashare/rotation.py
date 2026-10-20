"""Auditable intraday proposals for the LOCAL virtual portfolio only."""
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json

from .calendar import market_clock
from .mainboard import exclusions
from .plan import price_evidence


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def configuration():
    return {'mode': 'notify', 'margin_points': 10, 'cooldown_minutes': 30, 'max_daily_switches': 2,
        'proposals': {}, 'approvals': {}, 'confirmations': {}, 'history': [], 'notice_keys': []}


def stocks(state):
    return state.get('rotation', {}).get('slots') or state['plan']['stocks']


def source_fingerprint(row):
    return key({'events': sorted([{k: e.get(k) for k in ('id','version','url','published_at','snippet','analysis','risk_flag')}
                          for e in row.get('news',{}).get('events',[])],key=lambda e:(e['id'],e.get('version',''))),
                'announcements':sorted(row.get('announcements',[]),key=lambda a:a['id']),
                'title_flags':sorted(row.get('review',{}).get('title_flags') or [])})


def live_score(row):
    q = row.get('quote') or {}
    events = (row.get('news') or {}).get('events',[])
    fresh = any(e.get('fresh_catalyst') for e in events)
    confirmed = bool((row.get('price_evidence') or {}).get('ready'))
    above = bool(q.get('vwap') and q.get('price',0) >= q['vwap'])
    amount = q.get('amount_yuan') or 0
    score = (25 if fresh else 0) + (25 if confirmed else 0) + (10 if above else 0)
    score += 15 if amount >= 500_000_000 else 10 if amount >= 100_000_000 else 0
    score += 10 if row.get('review',{}).get('checked_at') else 0
    if row.get('risk_blockers'):score -= 20
    return max(0,score)


def freeze_stock(row, bars, now):
    checked=bars.get('checked_at')
    if bars.get('error') or not checked or not 0<=(now-datetime.fromisoformat(checked)).total_seconds()<=150:return None
    complete = [b for b in bars.get('items',[]) if b.get('as_of','')[:10] == now.date().isoformat()
                and datetime.fromisoformat(b['as_of'])+timedelta(seconds=5) <= now]
    if len(complete) < 3:return None
    if not 0<=(now-datetime.fromisoformat(complete[-1]['as_of'])).total_seconds()<=390:return None
    q = row['quote']; recent = complete[-3:]
    breakout = round(max(b['high'] for b in recent),2)
    invalid = round(min(q['vwap'],breakout),2)
    low = round(min(b['low'] for b in recent),2)
    return {'symbol':row['symbol'],'name':row['name'],'reference_close':q['previous_close'],
        'reference_vwap':q['vwap'], 'disclosure_status':'pending','disclosure_summary':'盘中模拟候补，原文尚未核验',
        'reviewed_ids':[], 'source_urls':list(dict.fromkeys(e['url'] for e in row['news']['events']))[:30],
        'reason':'盘中直接资讯候选；不属于原五股预测', 'risk_points':['新增候选，不自动解除当前或归档公告限制'],
        'rules':{'breakout':breakout,'hold':breakout,'invalid_below':invalid,
                 'strong_invalid_below':min(low,invalid),'max_vwap_deviation_pct':2,
                 'breakout_volume_ratio':1.5,'retest_tolerance_pct':.2}}


def evaluate(state, radar, focus_rows, bars, now, *, archived_blocks=None, allow_entries=True):
    """No mutation of published plans; all proposals/decisions remain in virtual state."""
    s=deepcopy(state);r=s.setdefault('rotation',configuration());r.setdefault('slots',deepcopy(s['plan']['stocks']))
    clock=market_clock(now);current=now.date().isoformat()==s['plan']['session_date']
    active=bool(radar.get('active') and radar.get('directory_valid') and current and s['enabled'])
    rowmap={x['symbol']:x for x in [*focus_rows,*radar.get('rows',[])]}
    slot_symbols={x['symbol'] for x in r['slots']}
    base_symbols={x['symbol'] for x in s['plan']['stocks']}
    slotmap={x['symbol']:x for x in r['slots']}
    public_rows=[]
    for row in radar.get('rows',[]):
        symbol=row['symbol']
        if symbol in base_symbols and symbol in slot_symbols:continue
        active_slot=symbol in slot_symbols
        fingerprint=source_fingerprint(row)
        previous=r['proposals'].get(symbol,{})
        proposal=deepcopy(previous) if previous.get('source_hash')==fingerprint else {
            'symbol':symbol,'name':row['name'],'created_at':now.isoformat(),'source_hash':fingerprint,
            'stock':None,'evidence':{},'first_ready_at':None,'ready_count':0}
        if active_slot and proposal['stock'] is None:proposal['stock']=deepcopy(slotmap[symbol])
        q=row.get('quote');reasons=[]
        if not allow_entries:reasons.append('模拟账户回撤或估值完整性限制新增')
        if not active or not clock['continuous'] or (now.hour,now.minute)<(9,45):reasons.append('等待有效交易日9:45后连续竞价')
        if not radar.get('source_fresh'):reasons.append('公开快讯来源未通过时效检查')
        if not q:reasons.append('缺少匹配报价')
        else:
            reasons.extend(exclusions(q,now))
            if (not q.get('limit_up') or not q.get('limit_down') or
                any(abs(q['price']-q[k])<=max(.03,q[k]*.003) for k in ('limit_up','limit_down') if q.get(k))):
                reasons.append('涨跌停边界缺失或过近，不能确认模拟可成交')
        events=row.get('news',{}).get('events',[])
        if not any(e.get('fresh_catalyst') and e.get('relation',{}).get('scope')=='direct' for e in events):
            reasons.append('缺少近30分钟、当前模型版本的直接正向资讯')
        if any(e.get('risk_flag') or not e.get('analysis') or e['analysis'].get('impact') in {'negative','uncertain'} for e in events):
            reasons.append('直接资讯存在负面、待整理或不确定性')
        review=row.get('review',{});checked=review.get('checked_at')
        if not checked or not 0 <= (now-datetime.fromisoformat(checked)).total_seconds()<=600 or review.get('error'):
            reasons.append('个股新闻/公告索引未通过时效检查')
        if review.get('title_flags'):reasons.append('公告存在风险词，不能自动覆盖')
        if (archived_blocks or {}).get(symbol):reasons.append('当前或归档公告限制仍有效：'+archived_blocks[symbol])
        if proposal['stock'] is None and q and q.get('vwap') and not exclusions(q,now):
            proposal['stock']=freeze_stock(row,bars.get(symbol,{}),now)
        evidence={}
        if proposal['stock'] and q and clock['continuous']:
            evidence=price_evidence(proposal['stock'],bars.get(symbol,{}),q,now)
            if not q.get('previous_close') or abs(q['previous_close']-proposal['stock']['reference_close'])>.011:
                reasons.append('昨收与冻结参考口径变化，需重新核验')
            if evidence.get('bar_as_of') and datetime.fromisoformat(q['as_of'])<datetime.fromisoformat(evidence['bar_as_of']):
                reasons.append('报价早于完整5分钟K线，等待同步')
        if not evidence.get('ready'):reasons.extend(evidence.get('reasons') or ['等待完整5分钟放量突破与下一根回踩'])
        elif datetime.fromisoformat(evidence['retest_at'])<datetime.fromisoformat(proposal['created_at']):
            reasons.append('突破回踩早于候选冻结时间，不能回填')
        if not proposal.get('stock'):reasons.append('缺少至少3根真实当日5分钟K线，不能生成参考位')
        approval=r['approvals'].get(symbol,{})
        reviewed=approval.get('source_hash')==fingerprint
        if not reviewed:reasons.append('资讯及公告原文尚未人工核对，暂不允许模拟替换')
        raw={**row,'price_evidence':evidence,'risk_blockers':[] if reviewed else ['原文待核验']}
        score=live_score(raw)+(10 if reviewed else 0)
        ready=not reasons
        if ready:
            proposal['first_ready_at']=proposal.get('first_ready_at') or now.isoformat()
            if proposal.get('last_ready_quote')!=(q or {}).get('as_of'):
                proposal['ready_count']+=1;proposal['last_ready_quote']=(q or {}).get('as_of')
        else:proposal.update(first_ready_at=None,ready_count=0)
        stable=bool(ready and proposal['ready_count']>=2 and (now-datetime.fromisoformat(proposal['first_ready_at'])).total_seconds()>=30)
        if ready and not stable:reasons.append('等待至少两次独立报价、持续30秒确认，避免追逐瞬时信号')
        eligible=stable
        proposal.update(score=score,evidence=evidence,eligible=eligible,active_slot=active_slot,blockers=list(dict.fromkeys(reasons)),
            events=deepcopy(events[:4]),announcements=deepcopy(row.get('announcements',[])),quote=deepcopy(q),
            reviewed=reviewed,checked_at=now.isoformat(),reference_kind='盘中候选冻结时的均价/前三根高低点，非昨日均价')
        r['proposals'][symbol]=proposal;public_rows.append(proposal)
    # Disappeared/reversed/expired candidates remain auditable but never keep eligibility.
    seen={p['symbol'] for p in public_rows}
    for symbol,p in r['proposals'].items():
        if symbol not in seen:p.update(eligible=False,blockers=['候选已过期、方向变化或不在当前有效雷达范围'])
    public_rows.sort(key=lambda p:(-p['score'],p['symbol']))
    for rank,p in enumerate(public_rows,1):p['rank']=rank
    r['ranking']=[p['symbol'] for p in public_rows]
    r['checked_at']=now.isoformat()
    # Only propose changing a weaker slot; all scores use this same live rubric.
    slots=sorted(r['slots'],key=lambda x:(bool(s['positions'].get(x['symbol'])),live_score(rowmap.get(x['symbol'],{})),x['symbol']))
    for p in public_rows:
        if p.get('active_slot'):continue
        if not slots:break
        old=slots[0];oldscore=live_score(rowmap.get(old['symbol'],{}))
        p.update(replace_symbol=old['symbol'],replace_name=old['name'],replace_score=oldscore,
            advantage=p['score']-oldscore,reason=f"同口径盘中研究分 {p['score']} 对 {oldscore}，分差至少需{r['margin_points']}；不是胜率")
        history=r['history'];today=[h for h in history if h['at'][:10]==now.date().isoformat()]
        cooling=bool(history and (now-datetime.fromisoformat(history[-1]['at'])).total_seconds()<r['cooldown_minutes']*60)
        if not p['eligible'] or p['advantage']<r['margin_points'] or cooling or len(today)>=r['max_daily_switches']:continue
        approved_mode=r['mode']=='auto' or r['mode']=='confirm' and r['confirmations'].get(p['symbol'])==p['source_hash']
        if not approved_mode:continue
        new=deepcopy(p['stock']);new.update(disclosure_status='reviewed',reviewed_ids=[a['id'] for a in p['announcements']],
            disclosure_summary='仅模拟候补原文经本地人工核对，风险和新公告仍独立检查')
        r['slots']=[new if x['symbol']==old['symbol'] else x for x in r['slots']]
        if old['symbol'] in s['positions']:
            s['positions'][old['symbol']].setdefault('pending_exit',{'at':now.isoformat(),'reason':'模拟轮换至 '+new['name']+'，仍受T+1与有效报价约束'})
        event={'at':now.isoformat(),'old_symbol':old['symbol'],'old_name':old['name'],'new_symbol':new['symbol'],'new_name':new['name'],
               'source_hash':p['source_hash'],'score':p['score'],'old_score':oldscore,'reason':p['reason'],'simulated':True,
               'events':deepcopy(p['events']),'announcements':deepcopy(p['announcements']),
               'evidence':deepcopy(p['evidence']),'stock':deepcopy(new),'source_review':deepcopy(r['approvals'][new['symbol']])}
        history.append(event)
        r['active_sources']=r.get('active_sources',{})|{new['symbol']:p['source_hash']}
        break
    return s


def dynamic_rows(state, radar, bars, now, risk_level):
    """Validate activated simulation-only slots; never turn radar into real confirmation."""
    r=state.get('rotation',{});rows=[];radar_rows={x['symbol']:x for x in radar.get('rows',[])}
    base={x['symbol'] for x in state['plan']['stocks']}
    for stock in stocks(state):
        symbol=stock['symbol']
        if symbol in base:continue
        raw=radar_rows.get(symbol,{});q=raw.get('quote');proposal=r.get('proposals',{}).get(symbol,{})
        blockers=[];reasons=[]
        if not raw or not r.get('approvals',{}).get(symbol,{}).get('source_hash')==source_fingerprint(raw):blockers.append('新资讯/公告改变，原核对已失效')
        if raw.get('review',{}).get('title_flags'):blockers.append('公告风险待核验')
        events=raw.get('news',{}).get('events',[])
        if any(e.get('risk_flag') or not e.get('analysis') or (e.get('analysis') or {}).get('impact') in {'negative','uncertain','pending'} for e in events):blockers.append('直接新闻风险或方向待核验')
        if not any(e.get('fresh_catalyst') and e.get('relation',{}).get('scope')=='direct' for e in events):reasons.append('缺少近30分钟直接正向资讯，不追旧闻')
        if not radar.get('source_fresh') or raw.get('review',{}).get('error'):blockers.append('新闻公告采集失败或过期')
        checked=raw.get('review',{}).get('checked_at')
        if not checked or not 0<=(now-datetime.fromisoformat(checked)).total_seconds()<=600:blockers.append('个股复核已过期')
        if q:
            reasons.extend(exclusions(q,now))
            if not q.get('previous_close') or abs(q['previous_close']-stock['reference_close'])>.011:reasons.append('昨收口径变化，重新核验')
        else:reasons.append('缺少有效报价')
        if risk_level not in {'normal','warning'}:blockers.append('模拟账户风控限制新增')
        if not proposal.get('eligible'):reasons.append('候补量价及风险条件尚未持续30秒通过')
        evidence=price_evidence(stock,bars.get(symbol,{}),q,now) if q and market_clock(now)['continuous'] else {}
        if q and evidence.get('bar_as_of') and datetime.fromisoformat(q['as_of'])<datetime.fromisoformat(evidence['bar_as_of']):reasons.append('报价早于完整5分钟K线')
        if not evidence.get('ready'):reasons.extend(evidence.get('reasons') or ['等待完整5分钟突破回踩'])
        elif datetime.fromisoformat(evidence['retest_at'])<datetime.fromisoformat(proposal.get('created_at',now.isoformat())):reasons.append('回踩早于候选冻结时间')
        if not radar.get('active'):reasons.append('当前不在有效资讯监控窗口')
        rows.append({**stock,'quote':q,'price_evidence':evidence,'risk_blockers':blockers,
            'reasons':list(dict.fromkeys(reasons+blockers)), 'entry_ready':bool(evidence.get('ready') and not reasons and not blockers)})
    return rows


def notifications(state, now):
    r=state.get('rotation',{});result=[];clock=market_clock(now)
    if not state.get('enabled') or not clock['continuous'] or state['plan']['session_date']!=now.date().isoformat():return []
    base={'simulation':True,'plan_identity':state['plan_identity']}
    for symbol in [x for x in r.get('ranking',[]) if not r['proposals'][x].get('active_slot')][:3]:
        p=r['proposals'][symbol]
        if p['score']<40 or not p.get('replace_symbol'):continue
        result.append(base|{'key':proposal_notice_key(p),'severity':'info',
            'message':f"模拟替换候选 {p['name']} {symbol}，拟比较 {p['replace_name']} {p['replace_symbol']}。{p['reason']}。"+
                ('全部门槛持续满足，等待轮换模式/确认' if p['eligible'] else '仍有阻碍：'+'；'.join(p['blockers'][:3]))+'；不是实际持仓或真实成交',
            'rotation_proposal':{k:deepcopy(p[k]) for k in ('symbol','name','source_hash','created_at','score','blockers','evidence','events','replace_symbol','replace_name','reason')}})
    for h in r.get('history',[]):
        if h['at'][:10]!=now.date().isoformat():continue
        result.append(base|{'key':'paper_switch_'+h['new_symbol']+'_'+key(h),'severity':'info','rotation_event':deepcopy(h),
            'message':f"模拟观察轮换：{h['old_name']} → {h['new_name']}。旧模拟仓位仍按T+1与有效报价排队处理，新股另等成交门槛；未改实际账户或原五股计划"})
    for t in state.get('trades',[]):
        if t['at'][:10]!=now.date().isoformat():continue
        result.append(base|{'key':'paper_fill_'+t['symbol']+'_'+key([t['at'],t['side'],t['symbol']]),'severity':'info',
            'message':f"纯模拟{'买入' if t['side']=='buy' else '卖出'}记录：{t['name']} {t['symbol']}。公开报价与成本假设生成，不是券商成交；明细请看本地模拟页"})
    return result


def proposal_notice_key(p):
    status=key([p.get('eligible'),p.get('blockers'),p.get('replace_symbol'),p.get('score')])
    return f"paper_candidate_{p['symbol']}_{p['source_hash']}_{status[:16]}"


def public_summary(state, error=None):
    if not state:return None
    r=state.get('rotation',{})
    return {'simulation_only':True,'mode':r.get('mode','notify'),'checked_at':r.get('checked_at'),
        'plan_identity':state['plan_identity'],'error':error,
        'active_symbols':[x['symbol'] for x in stocks(state)],'switch_history':deepcopy(r.get('history',[])[-10:]),
        'note':'仅模拟观察对象与履历，不含真实或模拟资金、股数；原发布计划与原排名保留'}
