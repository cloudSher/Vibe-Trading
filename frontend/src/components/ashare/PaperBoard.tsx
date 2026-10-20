import {useEffect,useState} from 'react';
import {ashareRequest,shanghaiTime} from '@/lib/ashare';
import {PaperRotationBoard,type Rotation} from './PaperRotationBoard';

interface Holding {quantity:number;available_quantity:number;entry_price:number;price:number|null;market_value:number|null;unrealized_pnl:number|null;quote_as_of:string|null;sellable_date:string;pending_exit?:{reason:string}}
interface Paper {
  enabled:boolean;initial_cash:number;cash:number;created_at:string;checked_at:string|null;plan_matches:boolean;
  plan_identity:{plan_id:string;version:number;content_hash:string};
  policy:{warning_pct:number;restrict_pct:number;max_drawdown_pct:number};
  risk:{equity:number|null;pnl:number|null;drawdown_pct:number|null;level:string;complete:boolean};
  rows:{symbol:string;name:string;budget:number;rules:{breakout:number;invalid_below:number};position:Holding|null;waiting:string[]}[];
  trades:{side:string;symbol:string;name:string;quantity:number;price:number;fee:number;at:string;reason:string}[];note:string;
  rotation?:Rotation;
}
const number=(v:number|null|undefined)=>v==null?'—':v.toLocaleString('zh-CN',{maximumFractionDigits:2,minimumFractionDigits:2});
const button='rounded-lg border border-border bg-card px-3 py-2 text-sm disabled:opacity-50';
export function PaperBoard(){
  const [paper,setPaper]=useState<Paper|null>(null),[hash,setHash]=useState('');
  const [cash,setCash]=useState('900000'),[error,setError]=useState<string|null>(null),[busy,setBusy]=useState(false);
  useEffect(()=>{
    let active=true;
    async function load(){try{
      const r=await ashareRequest<{portfolio:Paper|null;error:string|null}>('/paper');
      if(!active)return;setPaper(r.portfolio);setError(r.error);
      if(!r.portfolio){const p=await ashareRequest<{focus:{content_hash?:string}}>('/plan');if(active)setHash(p.focus.content_hash||'');}
    }catch(e){if(active)setError(e instanceof Error?e.message:'模拟记录读取失败');}}
    void load();const timer=setInterval(()=>void load(),15000);return()=>{active=false;clearInterval(timer);};
  },[]);
  async function change(path:string,method:string,body:unknown){setBusy(true);try{
    const r=await ashareRequest<{portfolio:Paper;error:string|null}>(path,method,body);setPaper(r.portfolio);setError(r.error);
  }catch(e){setError(e instanceof Error?e.message:'模拟操作失败');}finally{setBusy(false);}}
  return <section aria-label="独立模拟持仓" className="space-y-5 rounded-xl border border-primary/30 bg-card p-5">
    <div className="flex flex-wrap justify-between gap-3"><div><h2 className="font-semibold">模拟持仓 · 不实际下单</h2><p className="mt-2 text-xs leading-6 text-muted-foreground">虚拟资金、模拟成交和盈亏单独保存，真实现金、持仓、历史高点与回撤记录独立保留。</p></div>
      {paper&&<button className={button} disabled={busy||!!error} onClick={()=>void change('/paper/status','PUT',{enabled:!paper.enabled})}>{paper.enabled?'暂停模拟':'继续模拟'}</button>}</div>
    {error&&<p role="alert" className="text-sm text-red-600">{error}；留存数据不能当作当前有效状态。</p>}
    {!paper?<form onSubmit={e=>{e.preventDefault();void change('/paper','POST',{initial_cash:Number(cash),expected_plan_hash:hash});}} className="space-y-3">
      <label className="block text-sm">虚拟初始资金（元）<input aria-label="虚拟初始资金（元）" type="number" min="1000" max="100000000" step="0.01" value={cash} onChange={e=>setCash(e.target.value)} required className="ml-3 rounded border border-border bg-background p-2"/></label>
      <p className="text-xs leading-6 text-muted-foreground">按当前已发布五股计划：每只预算最多初始资金10%（含模拟摩擦成本），至少保留50%现金。仅目标日真实量价、新闻公告和模拟账户风控通过后记录模拟建仓；不按昨日收盘价回填持仓。</p>
      <button className={button} disabled={busy||!hash||!!error} type="submit">启动独立模拟组合</button>
    </form>:<>
      <p className="text-xs leading-6 text-muted-foreground">{paper.plan_identity.plan_id} · v{paper.plan_identity.version} · 校验 {paper.plan_identity.content_hash.slice(0,12)} · {paper.enabled?'模拟运行中':'已暂停'}<br/>创建 {shanghaiTime(paper.created_at)} · 最近计算 {shanghaiTime(paper.checked_at)} · {paper.policy.warning_pct}%预警 / {paper.policy.restrict_pct}%暂停新增 / {paper.policy.max_drawdown_pct}%最大阈值</p>
      {!paper.plan_matches&&<p role="alert">当前观察计划与模拟版本不一致，暂停新增模拟仓位；原模拟持仓继续估值及处理退出条件。</p>}
      <div className="grid gap-3 sm:grid-cols-4">{[['虚拟现金（元）',paper.cash],['模拟净值（元）',paper.risk.equity],['模拟盈亏（元）',paper.risk.pnl],['模拟阶段回撤（%）',paper.risk.drawdown_pct]].map(([label,value])=><div key={String(label)} className="rounded-lg border border-border p-3"><div className="text-xs text-muted-foreground">{label}</div><strong className="mt-2 block font-mono">{number(value as number|null)}</strong></div>)}</div>
      <PaperRotationBoard rotation={paper.rotation} hash={paper.plan_identity.content_hash} busy={busy||!!error} onAction={change}/>
      {!paper.risk.complete&&<p role="status">持仓估值数据缺失或过期：净值与盈亏保持未知，暂停新增模拟仓位。</p>}
      <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr>{['股票 / 预算','模拟持仓 / 可卖','成本 / 最新价','状态与阻碍'].map(x=><th key={x} className="p-3">{x}</th>)}</tr></thead><tbody>{paper.rows.map(row=><tr key={row.symbol} className="border-t border-border"><td className="p-3">{row.name}<br/>{row.symbol}<br/>最多 {number(row.budget)} 元<br/>突破 {number(row.rules.breakout)} / 失效参考 {number(row.rules.invalid_below)}</td><td className="p-3">{row.position?.quantity||0} 股 / {row.position?.available_quantity||0} 股</td><td className="p-3">{number(row.position?.entry_price)} / {number(row.position?.price)}<br/>{row.position&&`报价源时间 ${shanghaiTime(row.position.quote_as_of)}`}</td><td className="max-w-md p-3 leading-6">{row.position?<><p>{row.position.pending_exit?`待模拟退出：${row.position.pending_exit.reason}`:'模拟持有'}</p><p>可卖日期 {row.position.sellable_date} · 浮动盈亏 {number(row.position.unrealized_pnl)} 元</p></>:<><p>尚未模拟买入</p><details><summary>查看等待原因（{row.waiting.length}项）</summary><ul>{row.waiting.map((reason,i)=><li key={i}>{reason}</li>)}</ul></details></>}</td></tr>)}</tbody></table></div>
      <h3 className="text-sm font-semibold">模拟成交流水 · {paper.trades.length} 笔</h3>
      {!paper.trades.length?<p className="text-xs text-muted-foreground">尚无模拟成交。五股是待触发名单，当前模拟为空仓；不补造历史持仓或收益。</p>:<div className="space-y-2 text-xs">{paper.trades.slice(-30).reverse().map((t,i)=><div key={i} className="rounded border border-border p-3">{shanghaiTime(t.at)} · 模拟{t.side==='buy'?'买入':'卖出'} {t.name} {t.quantity}股 × {number(t.price)}元 · 模拟摩擦成本 {number(t.fee)}元<br/>{t.reason}</div>)}</div>}
      <p className="text-xs leading-6 text-muted-foreground">{paper.note} 费用是统一实验假设，不代表券商实收税费。当天买入不能当天卖出；失效时保留待退出记录，下一交易日仍须有效报价及可成交条件。原计划到期停止新建仓，已有模拟持仓继续跟踪退出。模拟只在本地后台运行时推进，停机期间不回填成交，不保证损失不超过20%。</p>
    </>}
  </section>;
}
