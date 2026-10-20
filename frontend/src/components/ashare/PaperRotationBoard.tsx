import {useState} from 'react';
import {shanghaiTime} from '@/lib/ashare';

interface Proposal {
  symbol:string;name:string;source_hash:string;rank:number;score:number;eligible:boolean;reviewed:boolean;active_slot?:boolean;
  replace_name?:string;replace_symbol?:string;advantage?:number;reason?:string;blockers:string[];
  created_at:string;evidence:{bar_as_of?:string;volume_ratio?:number};
  quote?:{price:number;as_of:string};
  events:{id:string;title:string;url:string;source:string;published_at:string;analysis?:{summary?:string}}[];
  announcements:{id:string;date:string;title:string;source_url:string}[];
}
export interface Rotation {mode:'notify'|'confirm'|'auto';margin_points:number;cooldown_minutes:number;max_daily_switches:number;
  ranking?:string[];proposals:Record<string,Proposal>;checked_at?:string;
  history:{at:string;old_name:string;new_name:string;reason:string}[]}

export function PaperRotationBoard({rotation,hash,busy,onAction}:{rotation?:Rotation;hash:string;busy:boolean;
  onAction:(path:string,method:string,body:unknown)=>Promise<void>}){
  const [checked,setChecked]=useState<Record<string,boolean>>({});
  const rows=(rotation?.ranking||[]).map(s=>rotation!.proposals[s]);
  const button='rounded border border-border px-3 py-2 text-xs disabled:opacity-40';
  return <section aria-label="盘中发现与模拟轮换" className="space-y-4 rounded-lg border border-primary/25 bg-primary/5 p-4">
    <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="font-semibold">盘中发现与模拟轮换</h3><p className="mt-2 text-xs leading-6 text-muted-foreground">沪深主板直接资讯候选另列，原五股预测、原排名和实际账户独立保留。盘中比较分是研究规则，不是胜率。</p></div>
      <label className="text-xs">切换方式 <select aria-label="模拟轮换方式" value={rotation?.mode||'notify'} disabled={busy} className="ml-2 rounded border border-border bg-background p-2" onChange={e=>void onAction('/paper/rotation','PUT',{mode:e.target.value,expected_plan_hash:hash})}>
        <option value="notify">只提醒候选</option><option value="confirm">确认后轮换模拟</option><option value="auto">满足条件自动轮换模拟</option>
      </select></label></div>
    <p className="text-xs leading-6 text-muted-foreground">分差至少{rotation?.margin_points??10}分；条件需两次独立报价持续30秒；冷却{rotation?.cooldown_minutes??30}分钟，每天最多{rotation?.max_daily_switches??2}次。新增股须先核对资讯及公告原文，来源或公告变化后核对失效。自动模式也不能解除公告限制、绕过完整5分钟突破回踩或20%回撤规则。</p>
    <p className="text-xs text-muted-foreground">最近比较 {shanghaiTime(rotation?.checked_at)} · 行情约30秒轮询，模型最多每5分钟一批；公开来源不保证交易所级实时性或全市场无遗漏。</p>
    {!rows.length&&<p role="status" className="text-xs leading-6">当前没有有效盘中替换候选。等待交易时段的直接消息、报价与完整5分钟K线；不会为切换补凑股票。</p>}
    {rows.slice(0,8).map(p=><article key={p.symbol} className="space-y-2 rounded-lg border border-border bg-card p-4 text-xs leading-6">
      <div className="flex flex-wrap justify-between"><strong>{p.rank} · {p.name} {p.symbol} · 盘中分{p.score}</strong><span>{p.eligible?'门槛持续通过':'等待条件/核验'}</span></div>
      <p>{p.active_slot?'已在模拟观察位，后续消息变化仍需重新核对原文':<>拟比较：{p.replace_name||'待比较'} {p.replace_symbol} · {p.reason}</>}</p>
      <p className="text-muted-foreground">冻结 {shanghaiTime(p.created_at)} · 报价 {shanghaiTime(p.quote?.as_of)} · 完整5分钟K线 {shanghaiTime(p.evidence.bar_as_of)} · 突破量比 {p.evidence.volume_ratio??'—'}</p>
      <details><summary>资讯、公告原文与当前阻碍</summary>{p.events.map(e=><div key={e.id} className="mt-2"><p>{e.source} · {shanghaiTime(e.published_at)} · {e.title}</p>{e.analysis?.summary&&<p>模型整理：{e.analysis.summary}</p>}{e.url.startsWith('https://')&&<a href={e.url} target="_blank" rel="noopener noreferrer" className="text-primary underline">查看原文</a>}</div>)}{p.announcements.map(a=><p key={a.id}>{a.date} · {a.source_url.startsWith('https://')?<a href={a.source_url} target="_blank" rel="noopener noreferrer" className="text-primary underline">{a.title}</a>:a.title}</p>)}<ul className="mt-2">{p.blockers.map((x,i)=><li key={i}>{x}</li>)}</ul></details>
      {!p.reviewed&&<div className="flex flex-wrap items-center gap-3"><label><input type="checkbox" checked={!!checked[p.source_hash]} onChange={e=>setChecked({...checked,[p.source_hash]:e.target.checked})}/> 我已阅读上述资讯及公告原文，确认仅用于模拟候补</label><button className={button} disabled={busy||!checked[p.source_hash]} onClick={()=>void onAction('/paper/candidate','POST',{symbol:p.symbol,source_hash:p.source_hash,action:'review'})}>保存本次原文核对</button></div>}
      {rotation?.mode==='confirm'&&!p.active_slot&&<button className={button} disabled={busy||!p.eligible} onClick={()=>void onAction('/paper/candidate','POST',{symbol:p.symbol,source_hash:p.source_hash,action:'confirm'})}>确认本次模拟替换</button>}
    </article>)}
    {!!rotation?.history.length&&<div><h4 className="font-semibold text-sm">模拟轮换履历（与原五股复盘分开）</h4>{rotation.history.map((h,i)=><p key={i} className="mt-2 text-xs leading-6">{shanghaiTime(h.at)} · {h.old_name} → {h.new_name} · {h.reason}。这是模拟观察位切换；旧仓仍须可卖、新仓仍须满足模拟成交条件。</p>)}</div>}
  </section>;
}
