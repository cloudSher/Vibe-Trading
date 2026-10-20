import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { Activity, Bell, Check, ChevronRight, CirclePause, Play, Plus, RefreshCw, ShieldCheck, Sparkles, Trash2 } from "lucide-react";
import { ashareRequest, localDateInput, qualityLabel, shanghaiTime,
  type Account, type AshareSnapshot, type Observation, type WatchSettings } from "@/lib/ashare";
import { FocusBoard } from "@/components/ashare/FocusBoard";
import { MainboardBoard } from "@/components/ashare/MainboardBoard";
import { MarketNewsBoard } from "@/components/ashare/MarketNewsBoard";
import { ResearchBoard } from "@/components/ashare/ResearchBoard";
import { PaperBoard } from "@/components/ashare/PaperBoard";

const button = "inline-flex items-center justify-center gap-2 rounded-lg border border-border bg-card px-3 py-2 text-sm font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50";
const input = "w-full rounded-lg border border-border bg-background px-3 py-2 text-sm outline-none focus:border-primary focus:ring-2 focus:ring-primary/15";
const fmt = (value: number | null | undefined, digits = 2) => value == null ? "—" : value.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
const money = (value: number | null | undefined) => value == null ? "—" : Math.abs(value) >= 1e8 ? `${fmt(value / 1e8)} 亿` : Math.abs(value) >= 1e4 ? `${fmt(value / 1e4)} 万` : fmt(value);
const tone = (value: number | null | undefined) => value == null || value === 0 ? "text-muted-foreground" : value > 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400";
const levelName: Record<string, string> = { incomplete: "资料待补全", normal: "未触发阈值", warning: "回撤预警", restrict: "限制观察", critical: "风险需要处理" };

function Panel({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-xl border border-border/70 bg-card ${className}`}>{children}</section>;
}
function Metric({ label, value, detail }: { label: string; value: ReactNode; detail: ReactNode }) {
  return <Panel className="px-5 py-4"><div className="text-xs text-muted-foreground">{label}</div><div className="mt-2 text-xl font-semibold tracking-tight tabular-nums">{value}</div><div className="mt-2 text-xs text-muted-foreground">{detail}</div></Panel>;
}

export function Ashare() {
  const navigate = useNavigate();
  const [data, setData] = useState<AshareSnapshot | null>(null);
  const [tab, setTab] = useState("focus");
  const [selected, setSelected] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const seq = useRef(0);
  const mounted = useRef(false);
  const controller = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (busyRef.current) return;
    const id = ++seq.current;
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    try {
      const result = await ashareRequest<AshareSnapshot>("", "GET", undefined, current.signal);
      if (!mounted.current || id !== seq.current) return;
      setData(result); setConnectionError(null);
    } catch (err) {
      if (!mounted.current || current.signal.aborted || id !== seq.current) return;
      setConnectionError(err instanceof Error ? err.message : "无法连接本地服务");
    }
  }, []);
  useEffect(() => {
    mounted.current = true;
    void load();
    const interval = window.setInterval(() => void load(), 15000);
    return () => { mounted.current = false; ++seq.current; controller.current?.abort(); window.clearInterval(interval); };
  }, [load]);

  async function perform(action: () => Promise<void>) {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(null); ++seq.current; controller.current?.abort();
    try { await action(); }
    catch (err) { if (mounted.current) setError(err instanceof Error ? err.message : "操作失败"); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  }
  const refresh = () => perform(async () => {
    setData(await ashareRequest<AshareSnapshot>("/refresh?refresh_pool=true", "POST"));
    setConnectionError(null);
  });
  const analyze = () => perform(async () => {
    const result = await ashareRequest<{ session_id: string }>(tab === "news" ? "/analysis?scope=news" : tab === "mainboard" ? "/analysis?scope=mainboard" : "/analysis", "POST");
    navigate(`/agent?session=${encodeURIComponent(result.session_id)}`);
  });

  if (!data) return <div className="p-8"><h1 className="text-2xl font-semibold">A 股盯盘</h1><p className="mt-4 text-muted-foreground">{connectionError || "正在连接本地行情监控…"}</p><button className={`${button} mt-4`} onClick={() => void load()}>重新连接</button></div>;
  const { risk, market, billboard } = data;
  const warningPct = risk.warning_pct ?? 3;
  const restrictPct = risk.restrict_pct ?? 4;
  const maximumPct = risk.max_drawdown_pct;
  const riskLabel = risk.level === "warning" ? `${warningPct}% 回撤预警` : risk.level === "restrict" ? `${restrictPct}% 限制观察` : levelName[risk.level];
  const rows = data.rows.filter(row => `${row.symbol} ${row.name}`.toLowerCase().includes(search.toLowerCase()));
  const detail = data.rows.find(row => row.symbol === selected);
  const entryBlocked = !!connectionError || !risk.selection_allowed;
  const candidates = entryBlocked ? 0 : data.rows.filter(row => row.candidate).length;
  const indices = data.quotes.filter(q => q.kind === "index");
  const extraQuotes = data.quotes.filter(q => q.kind === "stock" && !data.rows.some(row => row.symbol === q.symbol));
  const notify = async () => {
    if (!("Notification" in window)) { setError("当前浏览器不支持桌面通知；站内提醒仍可使用"); return; }
    const result = await Notification.requestPermission();
    setError(result === "granted" ? "桌面提醒已开启。页面打开时可弹出新提醒；页面关闭后请回到提醒记录查看。" : "未获得桌面通知权限；请使用站内提醒记录。");
  };

  return <div className="min-h-full bg-background p-4 lg:p-7"><div className="mx-auto max-w-[1500px] space-y-5">
    <header className="flex flex-wrap items-start justify-between gap-4 border-b border-border/60 pb-5">
      <div><div className="mb-2 flex items-center gap-2 text-xs font-medium text-primary"><Activity className="h-4 w-4" />A 股观察台<span className="text-muted-foreground">/</span><span className="text-muted-foreground">手动交易</span></div>
        <h1 className="text-2xl font-semibold tracking-tight">主板发现，盘中验证</h1>
        <p className="mt-2 text-sm text-muted-foreground">沪深主板扩展扫描与重点股持续跟踪，用量价筛选观察对象，以账户回撤约束风险。</p></div>
      <div className="flex flex-wrap gap-2"><button className={button} disabled={busy} onClick={() => void refresh()}><RefreshCw className={`h-4 w-4 ${busy ? "animate-spin" : ""}`} />刷新数据</button>
        <button className={`${button} border-primary/30 bg-primary/10 text-primary`} disabled={busy || !!connectionError || !(tab === "news" ? data.market_news?.rows.length : tab === "mainboard" ? data.mainboard?.rows.length : data.rows.length)} onClick={() => void analyze()}><Sparkles className="h-4 w-4" />AI 解读公开行情</button></div>
    </header>
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs text-muted-foreground">
      <span className="flex items-center gap-1.5"><span className={`h-2 w-2 rounded-full ${connectionError || !data.settings.enabled ? "bg-amber-500" : data.monitor_running ? "bg-emerald-500" : "bg-red-500"}`} />{connectionError ? "连接中断" : !data.settings.enabled ? "监控暂停" : data.monitor_running ? "后台监控运行中" : "后台监控未运行"}</span>
      <span>北京时间 · {market.label}</span><span>盘中 {data.settings.poll_seconds} 秒轮询 · 页面 15 秒同步</span><span>最近抓取 {shanghaiTime(data.refreshed_at)}</span>
      <button className="inline-flex items-center gap-1 underline underline-offset-4" disabled={busy} onClick={() => void perform(async () => setData(await ashareRequest<AshareSnapshot>("/settings", "PUT", { ...data.settings, enabled: !data.settings.enabled })))}>{data.settings.enabled ? <CirclePause className="h-3 w-3" /> : <Play className="h-3 w-3" />}{data.settings.enabled ? "暂停监控" : "开启监控"}</button>
    </div>
    {connectionError && <div role="alert" className="rounded-lg border border-red-300 bg-red-50 p-4 text-sm text-red-800 dark:bg-red-950/30">连接中断：{connectionError}。当前页面是留存快照，暂停依赖此页面判断新增仓位。</div>}
    {error && <div role="alert" className="flex justify-between gap-3 rounded-lg border border-amber-300/60 bg-amber-500/10 p-3 text-sm"><span>{error}</span><button onClick={() => setError(null)}>关闭</button></div>}
    {Object.values(data.errors).map(message => <div role="alert" key={message} className="rounded-lg border border-amber-300/60 bg-amber-500/10 p-3 text-sm">{message}</div>)}
    {!market.live_window && <div className="rounded-lg border border-border bg-muted/35 px-4 py-3 text-sm">{market.label}期间展示最近报价，仅供复盘。数据源未承诺交易所级实时延迟；盘中报价超过 90 秒或缺少时间戳时，暂停候选判断。</div>}
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <Metric label="最新已披露龙虎榜" value={billboard.date || "等待榜单"} detail={`${billboard.selected} 只观察 / ${billboard.total_symbols ?? "—"} 只上榜 · ${billboard.pool_valid ? "日期核验通过" : "等待最新榜单"}`} />
      <Metric label="量价条件观察" value={<>{candidates}<span className="ml-2 text-sm font-normal text-muted-foreground">只</span></>} detail={market.continuous ? "仅为观察条件，不代表买入建议或胜率" : "非连续竞价阶段，暂停产生盘中候选"} />
      <Metric label={risk.monitoring_period ? "本阶段回撤 / 阈值" : "账户当前回撤 / 阈值"} value={connectionError ? "连接中断" : <>{fmt(risk.drawdown_pct)}<span className="ml-1 text-sm text-muted-foreground">% / {maximumPct}%</span></>} detail={risk.complete ? `${risk.monitoring_period ? '本阶段高点；历史回撤另存' : '相对份额化历史高点'} · ${riskLabel}` : "尚未得到完整的账户及有效报价"} />
      <Metric label="账户净值（人民币）" value={connectionError ? "—" : money(risk.equity)} detail={risk.basis === "live_quote" ? "公开盘中报价估值 · 不含未录入费用" : "最近报价参考估值 · 不代表盘中成交价格"} />
    </div>
    <Panel className={`flex flex-wrap items-center justify-between gap-3 px-5 py-4 ${risk.latched ? "border-red-400/70 bg-red-500/5" : ""}`}>
      <div className="flex min-w-0 items-start gap-3"><ShieldCheck className={`mt-0.5 h-5 w-5 shrink-0 ${risk.latched ? "text-red-600" : "text-primary"}`} /><div><div className="text-sm font-semibold">{connectionError ? "无法确认当前账户风险" : riskLabel}</div><p className="mt-1 text-sm text-muted-foreground">{risk.guidance}</p></div></div>
      <button className={button} onClick={() => setTab("account")}>{risk.configured ? "更新账户资料" : "录入账户，启用风险计算"}<ChevronRight className="h-4 w-4" /></button>
    </Panel>
    <div className="flex flex-wrap gap-4 border-b border-border text-sm">
      {[['focus', '重点监控池'], ['research', '候选排名与复盘'], ['paper', '模拟持仓'], ['news', '主板利好雷达'], ['mainboard', '沪深主板扫描'], ['watch', '龙虎榜观察池'], ['account', '账户风控'], ['alerts', `提醒记录${data.unread_alerts ? ` · ${data.unread_alerts}` : ''}`], ['settings', '监控设置']].map(([key, label]) => <button key={key} onClick={() => setTab(key)} className={`border-b-2 px-1 pb-3 ${tab === key ? "border-primary font-semibold text-primary" : "border-transparent text-muted-foreground"}`}>{label}</button>)}
    </div>
    {tab === "focus" && <FocusBoard focus={data.focus} news={data.news} disconnected={!!connectionError} />}
    {tab === "research" && <ResearchBoard />}
    {tab === "paper" && <PaperBoard />}
    {tab === "news" && <MarketNewsBoard radar={data.market_news} disconnected={!!connectionError} />}
    {tab === "mainboard" && <MainboardBoard board={data.mainboard} disconnected={!!connectionError} busy={busy} onScan={() => void perform(async () => setData(await ashareRequest<AshareSnapshot>("/mainboard/scan", "POST")))} />}
    {tab === "watch" && <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-3">{indices.map(q => <Panel key={q.symbol} className="flex items-center justify-between p-3"><div><div className="text-xs text-muted-foreground">{q.name}</div><div className="mt-1 font-mono text-sm font-semibold">{fmt(q.price)}</div></div><div className="text-right"><div className={`font-mono text-sm ${tone(q.change_pct)}`}>{q.change_pct > 0 ? "+" : ""}{fmt(q.change_pct)}%</div><div className="mt-1 text-[10px] text-muted-foreground">{qualityLabel[q.quality || "missing"]}</div></div></Panel>)}</div>
      <Panel><div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-4"><div><h2 className="text-sm font-semibold">热门股票池</h2><p className="mt-1 text-xs text-muted-foreground">按龙虎榜成交额形成股票池；同一股票去重。当前观察排序包含资金方向、成交额、开盘价和均价位置。</p></div><input className={`${input} max-w-56`} aria-label="搜索股票" placeholder="搜索股票代码或名称" value={search} onChange={e => setSearch(e.target.value)} /></div>
        <div className="overflow-x-auto"><table className="w-full min-w-[1000px] text-left text-xs"><thead className="bg-muted/35 text-muted-foreground"><tr>{["股票 / 数据时间", "最新价 / 涨跌", "成交额", "均价 / 换手", "5分钟变化", "龙虎榜净买额", "观察分", "状态"].map(h => <th key={h} className="px-4 py-3 font-medium">{h}</th>)}</tr></thead><tbody>{rows.map(row => <ObservationRow key={row.symbol} row={row} disconnected={!!connectionError} riskBlocked={!risk.selection_allowed} onSelect={() => setSelected(selected === row.symbol ? null : row.symbol)} />)}</tbody></table></div>
        {!rows.length && <p className="p-8 text-center text-sm text-muted-foreground">{data.rows.length ? "没有匹配的股票" : "等待龙虎榜数据；可以点击刷新数据重试"}</p>}
      </Panel>
      {detail && <Panel className="p-5"><div className="flex justify-between"><h3 className="font-semibold">{detail.name} <span className="ml-2 font-mono text-xs text-muted-foreground">{detail.symbol}</span></h3><button className="text-xs text-primary" onClick={() => setSelected(null)}>收起</button></div><div className="mt-3 grid gap-3 sm:grid-cols-3"><div className="text-sm">行情时间<p className="mt-1 text-xs text-muted-foreground">{shanghaiTime(detail.quote?.as_of)}</p></div><div className="text-sm">报价来源<p className="mt-1 text-xs text-muted-foreground">腾讯公开报价 · 人民币 · 不复权</p></div><div className="text-sm">抓取时间<p className="mt-1 text-xs text-muted-foreground">{shanghaiTime(detail.quote?.received_at)}</p></div></div><p className="mt-4 text-xs leading-6 text-muted-foreground">上榜原因：{detail.reasons.join("；")}</p><p className="mt-2 text-xs leading-6 text-muted-foreground">{detail.flags.join("；") || "尚无过滤项触发。观察对象仍需盘中时效、流动性、价格与均价位置同时满足。"}</p><p className="mt-2 text-xs text-muted-foreground">规则分 = 龙虎榜净买为正 20 + 成交额 25 + 高于开盘价 20 + 不低于成交均价 20 + 榜单成交额排序 15。不是收益预测。</p></Panel>}
      {!!extraQuotes.length && <Panel className="p-4"><h3 className="text-sm font-semibold">补充自选 / 持仓报价</h3><div className="mt-3 flex flex-wrap gap-6">{extraQuotes.map(q => <div key={q.symbol} className="text-xs"><div>{q.name} <span className="text-muted-foreground">{q.symbol}</span></div><div className="mt-1 font-mono">{fmt(q.price)} <span className={tone(q.change_pct)}>{fmt(q.change_pct)}%</span></div><div className="mt-1 text-muted-foreground">{qualityLabel[q.quality || "missing"]}</div></div>)}</div></Panel>}
      <p className="text-xs leading-6 text-muted-foreground">5 分钟变化需要累计至少 5 分钟有效报价；没有数据时显示“—”。龙虎榜是盘后披露数据，不能代表当前主力资金方向。<a className="ml-1 text-primary underline" href="https://data.eastmoney.com/stock/tradedetail.html" target="_blank" rel="noreferrer">查看榜单来源</a></p>
    </div>}
    {tab === "account" && <div className="grid items-start gap-5 xl:grid-cols-[1.1fr_1fr]">
      <AccountForm key={data.account?.submission_id || "new"} account={data.account} initialized={risk.initialized} busy={busy} onSave={body => perform(async () => setData(await ashareRequest<AshareSnapshot>("/account", "PUT", body)))} />
      <div className="space-y-4"><RiskPeriodPanel data={data} busy={busy || !!connectionError} onStart={body => perform(async () => setData(await ashareRequest<AshareSnapshot>("/risk-period", "POST", body)))} />
        <Panel className="p-5"><h2 className="text-sm font-semibold">{maximumPct}% 回撤规则</h2><div className="mt-5 h-2 overflow-hidden rounded bg-muted"><div className={`h-full ${risk.latched ? "bg-red-600" : "bg-primary"}`} style={{ width: `${Math.min(100, (risk.drawdown_pct || 0) / maximumPct * 100)}%` }} /></div><div className="mt-2 flex justify-between text-xs text-muted-foreground"><span>0%</span><span>{warningPct}% 预警</span><span>{restrictPct}% 暂停新增</span><span>{maximumPct}% 阈值</span></div><dl className="mt-5 space-y-3 text-sm"><div className="flex justify-between"><dt className="text-muted-foreground">{risk.monitoring_period ? "本阶段高点的当前等值" : "份额化高点的当前等值"}</dt><dd>{money(risk.peak_equity_equivalent)}</dd></div><div className="flex justify-between"><dt className="text-muted-foreground">距离 {maximumPct}% 阈值的估算余量</dt><dd>{money(risk.budget_to_limit ?? (maximumPct === 5 ? risk.budget_to_5pct : null))}</dd></div></dl><p className="mt-4 text-xs leading-6 text-muted-foreground">阈值判断使用完整、时效有效的报价。触发后保留当前口径的风险状态，回撤低于 {warningPct}%、账户更新且盘中报价有效时，可由你确认恢复观察。提高阈值不会清除历史高点或已触发状态；建立新阶段仅切换后续提醒口径，历史亏损继续记录。手动下单不受软件强制限制，实际损失仍可能超过阈值。</p><button className={`${button} mt-3`} onClick={() => setTab("settings")}>调整回撤阈值</button>{risk.latched && <button className={`${button} mt-3`} disabled={busy || !risk.complete || !market.live_window || (risk.drawdown_pct ?? 100) >= warningPct} onClick={() => void perform(async () => setData(await ashareRequest<AshareSnapshot>("/resume", "POST")))}>确认风险缓解并恢复观察</button>}</Panel>
        <Panel className="p-5"><h2 className="text-sm font-semibold">持仓风险</h2>{!risk.holdings.length && <p className="mt-3 text-xs text-muted-foreground">当前已保存快照持仓 0 只，请确认实际空仓或补齐遗漏；重点监控池不代表实际持仓。</p>}{risk.holdings.map(p => <div key={p.symbol} className="mt-4 border-t border-border pt-3 text-xs"><div className="flex justify-between"><strong>{p.name} · {p.symbol}</strong><span>占比 {fmt(p.weight_pct)}%</span></div><div className="mt-2 flex flex-wrap gap-4 text-muted-foreground"><span>持有 {p.quantity} 股</span><span>可卖 {p.available_quantity} 股</span><span>不可卖 {p.locked_quantity} 股</span><span>成本盈亏 {fmt(p.cost_pnl_pct)}%</span><span>{qualityLabel[p.quality]}</span></div></div>)}</Panel>
      </div>
    </div>}
    {tab === "alerts" && <Panel className="p-5"><div className="flex flex-wrap justify-between gap-3"><div><h2 className="font-semibold">站内提醒记录</h2><p className="mt-1 text-xs text-muted-foreground">同一风险持续存在时不反复弹出；恢复或升级后重新提醒。已读不等于风险已解除。</p></div><div className="flex gap-2"><button className={button} onClick={() => void notify()}><Bell className="h-4 w-4" />开启桌面提醒</button><button className={button} disabled={busy || !data.unread_alerts} onClick={() => void perform(async () => { await ashareRequest("/alerts/ack", "POST", { ids: data.alerts.map(a => a.id) }); setData(await ashareRequest<AshareSnapshot>()); })}><Check className="h-4 w-4" />标记已读</button></div></div><div className="mt-5 divide-y divide-border">{data.alerts.map(alert => <div key={alert.id} className="flex gap-3 py-4"><span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${alert.severity === "critical" ? "bg-red-600" : alert.severity === "warning" ? "bg-amber-500" : "bg-slate-400"}`} /><div className="flex-1"><p className="text-sm">{alert.active === false && <span className="mr-2 text-muted-foreground">历史事件 · 当前条件已失效</span>}{alert.message}</p><p className="mt-1 text-xs text-muted-foreground">{shanghaiTime(alert.created_at)} · {alert.acknowledged_at ? "已读" : "未读"}</p></div></div>)}{!data.alerts.length && <p className="py-10 text-center text-sm text-muted-foreground">暂无已记录的提醒。请先录入账户，启用个人风险计算。</p>}</div><p className="mt-4 text-xs text-muted-foreground">服务运行时保留站内记录；桌面提醒需要浏览器授权且页面保持打开。没有配置外部消息渠道。</p></Panel>}
    {tab === "settings" && <SettingsForm key={JSON.stringify(data.settings)} value={data.settings} audit={data.risk_policy_audit} busy={busy} onSave={body => perform(async () => setData(await ashareRequest<AshareSnapshot>("/settings", "PUT", body)))} />}
    <footer className="pb-6 text-[11px] leading-6 text-muted-foreground">{data.notice} 个人账户资料只参与本地确定性风控，AI 解读按钮仅发送公开榜单与行情。{!market.calendar_verified && "当前年度日历未核验，盘中候选暂停。"}</footer>
  </div></div>;
}

function ObservationRow({ row, onSelect, disconnected, riskBlocked }: { row: Observation; onSelect: () => void; disconnected: boolean; riskBlocked: boolean }) {
  const q = row.quote;
  return <tr className="border-t border-border/60 hover:bg-muted/25"><td className="px-4 py-3"><button className="text-left" onClick={onSelect}><div className="font-medium">{row.name}<span className="ml-2 font-mono text-muted-foreground">{row.symbol}</span></div><div className="mt-1 text-[10px] text-muted-foreground">{q ? shanghaiTime(q.as_of) : "等待报价"}</div></button></td><td className="px-4 py-3 font-mono"><div>{fmt(q?.price)}</div><div className={`mt-1 ${tone(q?.change_pct)}`}>{fmt(q?.change_pct)}%</div></td><td className="px-4 py-3 font-mono">{money(q?.amount_yuan)}</td><td className="px-4 py-3 font-mono"><div>{fmt(q?.vwap)}</div><div className="mt-1 text-muted-foreground">{fmt(q?.turnover_pct)}%</div></td><td className={`px-4 py-3 font-mono ${tone(row.five_minute_change_pct)}`}>{fmt(row.five_minute_change_pct)}{row.five_minute_change_pct == null ? "" : "%"}</td><td className={`px-4 py-3 font-mono ${tone(row.net_buy_amount)}`}>{money(row.net_buy_amount)}</td><td className="px-4 py-3 font-mono">{fmt(row.score, 0)}<span className="text-muted-foreground"> /100</span></td><td className="max-w-48 px-4 py-3"><span className={`rounded px-2 py-1 text-[10px] ${row.candidate && !disconnected && !riskBlocked ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>{disconnected ? "连接中断" : row.candidate && riskBlocked ? "仅行情观察" : row.label}</span><div className="mt-2 text-[10px] leading-4 text-muted-foreground">{qualityLabel[row.quality]}{row.flags[0] ? ` · ${row.flags[0]}` : ""}</div></td></tr>;
}

type PositionDraft = { symbol: string; quantity: string; available_quantity: string; cost_price: string };
function RiskPeriodPanel({ data, busy, onStart }: { data: AshareSnapshot; busy: boolean; onStart: (body: unknown) => Promise<void> }) {
  const { risk, account } = data;
  const [confirmedAccount, setConfirmedAccount] = useState<string | null>(null);
  const submission = useRef({ account: "", id: "" });
  const period = risk.monitoring_period;
  const todayConfirmed = account?.confirmed_at.slice(0, 10) === data.market.now.slice(0, 10);
  return <Panel className="p-5"><h2 className="text-sm font-semibold">监测基准与历史记录</h2>
    {period ? <><p className="mt-3 text-sm font-medium text-primary">当前按本监测阶段高点计算回撤</p><dl className="mt-4 space-y-3 text-sm">
      <div className="flex justify-between gap-4"><dt className="text-muted-foreground">阶段起点</dt><dd>{shanghaiTime(period.started_at)}</dd></div>
      <div className="flex justify-between"><dt className="text-muted-foreground">起始账户净值</dt><dd>{money(period.start_equity)} 元</dd></div>
      <div className="flex justify-between"><dt className="text-muted-foreground">本阶段当前回撤</dt><dd>{fmt(risk.drawdown_pct)}%</dd></div>
      <div className="flex justify-between"><dt className="text-muted-foreground">本阶段净值变动（剔除出入金）</dt><dd>{fmt(risk.period_return_pct)}%</dd></div>
    </dl><p className="mt-3 text-xs leading-6 text-muted-foreground">起点取自 {shanghaiTime(period.account_confirmed_at)} 确认的已保存账户，{period.valuation_basis === "live_quote" ? "使用当时有效盘中报价" : "使用当时参考估值"}。后续高点随净值上升更新，保存账户、出入金或重启不会重建起点。</p></> : <>
      <p className="mt-3 text-xs leading-6 text-muted-foreground">当前使用历史高点。可按今天已确认的完整账户估值建立新阶段，后续提醒改用阶段高点；原高点、亏损和旧风险事件保留。请先保存完整现金、全部持仓及可卖数量。</p>
      <label className="mt-3 flex items-start gap-2 text-xs leading-5"><input className="mt-1" type="checkbox" checked={!!account && confirmedAccount === account.submission_id} disabled={busy || !risk.complete || !todayConfirmed} onChange={e => setConfirmedAccount(e.target.checked ? account?.submission_id || null : null)} />我已核对已保存的全部现金和持仓，按当前完整估值建立新阶段</label>
      <button className={`${button} mt-3`} disabled={busy || !risk.complete || !todayConfirmed || !account || confirmedAccount !== account.submission_id} onClick={() => {
        if (!account) return;
        if (submission.current.account !== account.submission_id) submission.current = { account: account.submission_id, id: crypto.randomUUID() };
        void onStart({ submission_id: submission.current.id, expected_account_submission_id: account.submission_id, confirm_account_complete: true });
      }}>按已确认净值建立新阶段</button>
    </>}
    <div className="mt-4 border-t border-border pt-4"><h3 className="text-xs font-medium">历史高点口径（保留）</h3><dl className="mt-3 space-y-3 text-sm">
      <div className="flex justify-between"><dt className="text-muted-foreground">历史高点的当前等值</dt><dd>{money(risk.historical_peak_equity_equivalent ?? risk.peak_equity_equivalent)} 元</dd></div>
      <div className="flex justify-between"><dt className="text-muted-foreground">相对历史高点回撤</dt><dd>{fmt(risk.historical_drawdown_pct ?? risk.drawdown_pct)}%</dd></div>
      <div className="flex justify-between"><dt className="text-muted-foreground">距历史高点净值差额</dt><dd>{money(risk.historical_gap_to_peak)} 元</dd></div>
    </dl><p className="mt-3 text-xs leading-6 text-muted-foreground">历史差额来自已录入高点与当前估值，并非券商已实现盈亏报表。{risk.historical_latched && "历史曾触发回撤限制，记录继续保留。"}建立新阶段不表示历史亏损已收回，也不构成买入确认。</p></div>
  </Panel>;
}

function AccountForm({ account, initialized, onSave, busy }: { account: Account | null; initialized: boolean; busy: boolean; onSave: (value: unknown) => Promise<void> }) {
  const [cash, setCash] = useState(account ? String(account.cash) : "");
  const [peak, setPeak] = useState(account ? String(account.historical_peak_equity) : "");
  const [peakAt, setPeakAt] = useState(localDateInput(account?.peak_as_of));
  const [flow, setFlow] = useState("0");
  const [positions, setPositions] = useState<PositionDraft[]>((account?.positions || []).map(p => ({ symbol: p.symbol, quantity: String(p.quantity), available_quantity: String(p.available_quantity), cost_price: p.cost_price == null ? "" : String(p.cost_price) })));
  const lastSubmission = useRef({ fingerprint: "", id: "" });
  const update = (index: number, key: keyof PositionDraft, value: string) => setPositions(old => old.map((p, i) => i === index ? { ...p, [key]: value } : p));
  return <Panel className="p-5"><h2 className="font-semibold">本地账户快照</h2><p className="mt-2 text-xs leading-6 text-muted-foreground">录入实际现金与持仓，不需要券商账号、密码或密钥。每次手动成交后、每个交易日开盘前请更新并确认；可卖股数以券商为准，不会自动推算为可卖。</p><form className="mt-5 space-y-5" onSubmit={event => { event.preventDefault(); const body = { cash: Number(cash), historical_peak_equity: Number(peak), peak_as_of: peakAt + ":00+08:00", external_flow: Number(flow), positions: positions.map(p => ({ symbol: p.symbol, quantity: Number(p.quantity), available_quantity: Number(p.available_quantity), cost_price: p.cost_price ? Number(p.cost_price) : null })) }; const fingerprint = JSON.stringify(body); if (lastSubmission.current.fingerprint !== fingerprint) lastSubmission.current = { fingerprint, id: crypto.randomUUID() }; void onSave({ ...body, submission_id: lastSubmission.current.id }); }}>
    <div className="grid gap-4 sm:grid-cols-2"><label className="space-y-2 text-xs">实际现金（元）<input aria-label="实际现金（元）" className={`${input} mt-2`} type="number" min="0" step="0.01" required value={cash} placeholder="请输入实有现金" onChange={e => setCash(e.target.value)} /></label><label className="space-y-2 text-xs">初始账户历史高点（元）<input aria-label="初始账户历史高点（元）" className={`${input} mt-2`} type="number" min="0.01" step="0.01" required readOnly={initialized} value={peak} onChange={e => setPeak(e.target.value)} /></label><label className="space-y-2 text-xs">高点时间（北京时间）<input aria-label="高点时间（北京时间）" className={`${input} mt-2`} type="datetime-local" required readOnly={initialized} value={peakAt} onChange={e => setPeakAt(e.target.value)} /></label><label className="space-y-2 text-xs">本次净出入金（元）<input aria-label="本次净出入金（元）" className={`${input} mt-2`} type="number" step="0.01" disabled={!initialized} value={flow} onChange={e => setFlow(e.target.value)} /></label></div>
    <p className="text-xs leading-5 text-muted-foreground">净出入金：入金填正数，出金填负数；买卖成交款、手续费、分红均不填此项。份额化高点保留历史，初始高点建立后不可重设。</p>
    <div className="flex justify-between"><h3 className="text-sm font-medium">持仓（单位：股）</h3><button className="inline-flex items-center gap-1 text-xs text-primary" type="button" onClick={() => setPositions([...positions, { symbol: "", quantity: "", available_quantity: "", cost_price: "" }])}><Plus className="h-3 w-3" />添加持仓</button></div>
    <div className="space-y-3">{positions.map((p, i) => <fieldset key={i} className="rounded-lg border border-border p-3"><div className="mb-3 flex items-center justify-between text-xs"><span>持仓 {i + 1}</span><button type="button" aria-label={`删除持仓 ${i + 1}`} onClick={() => setPositions(positions.filter((_, index) => index !== i))}><Trash2 className="h-3.5 w-3.5 text-muted-foreground" /></button></div><div className="grid gap-3 sm:grid-cols-2">{([['symbol', '股票代码'], ['quantity', '持仓股数'], ['available_quantity', '可卖股数'], ['cost_price', '成本价（可选）']] as [keyof PositionDraft, string][]).map(([key, label]) => <label key={key} className="text-xs text-muted-foreground">{label}<input aria-label={`${label} ${i + 1}`} className={`${input} mt-1`} type={key === "symbol" ? "text" : "number"} min={key === "available_quantity" ? 0 : key === "cost_price" ? 0.01 : 1} step={key === "cost_price" ? "0.001" : 1} required={key !== "cost_price"} value={p[key]} onChange={e => update(i, key, e.target.value)} placeholder={key === "symbol" ? "000001.SZ" : ""} /></label>)}</div></fieldset>)}</div>
    <button className={`${button} w-full border-primary/40 bg-primary/10 text-primary`} disabled={busy} type="submit">保存并确认账户快照</button><p className="text-xs text-muted-foreground">上次确认：{shanghaiTime(account?.confirmed_at)}</p>
  </form></Panel>;
}

function SettingsForm({ value, audit, busy, onSave }: { value: WatchSettings; audit?: AshareSnapshot["risk_policy_audit"]; busy: boolean; onSave: (value: WatchSettings) => Promise<void> }) {
  const [draft, setDraft] = useState({ ...value, warning_drawdown_pct: value.warning_drawdown_pct ?? 3, restrict_drawdown_pct: value.restrict_drawdown_pct ?? 4, max_drawdown_pct: value.max_drawdown_pct ?? 5 });
  const [watchlist, setWatchlist] = useState(value.watchlist.join(", "));
  const thresholdsValid = draft.warning_drawdown_pct > 0 && draft.warning_drawdown_pct < draft.restrict_drawdown_pct && draft.restrict_drawdown_pct < draft.max_drawdown_pct && draft.max_drawdown_pct < 100;
  return <Panel className="max-w-3xl p-5"><h2 className="font-semibold">监控设置</h2><form className="mt-5 space-y-5" onSubmit={e => { e.preventDefault(); if (thresholdsValid) void onSave({ ...draft, watchlist: watchlist.split(/[,，\s]+/).filter(Boolean) }); }}>
    <fieldset className="rounded-lg border border-border p-4"><legend className="px-2 text-sm font-medium">持续资讯与公告评估</legend><label className="flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={!!draft.recurring_news_enabled} onChange={e => setDraft({ ...draft, recurring_news_enabled: e.target.checked })} />每个交易日09:00—21:00持续资讯评估</label><label className="mt-3 flex items-start gap-2 text-sm"><input type="checkbox" className="mt-1" checked={!!draft.market_news_enabled} onChange={e => setDraft({ ...draft, market_news_enabled: e.target.checked })} />主板利好雷达：发现原五股以外的公司直接消息</label><p className="mt-2 text-xs leading-6 text-muted-foreground">北京时间；休市日跳过，直到暂停。盘后资讯用于复盘评估；旧交易预案到期后需重新核验参考位与公告，不能自动生成次日买点。Codex提醒与本地采集分别运行，需保持Mac唤醒及应用可用。</p></fieldset>
    <fieldset className="rounded-lg border border-primary/20 bg-primary/5 p-4"><legend className="px-2 text-sm font-medium">账户回撤阈值（%）</legend><div className="grid gap-4 sm:grid-cols-3">
      {([['warning_drawdown_pct', '预警阈值（%）'], ['restrict_drawdown_pct', '暂停新增阈值（%）'], ['max_drawdown_pct', '最大回撤阈值（%）']] as const).map(([key, label]) => <label key={key} className="text-xs">{label}<input aria-label={label} className={`${input} mt-2`} type="number" min="0.1" max="99.9" step="0.1" required value={draft[key]} onChange={e => setDraft({ ...draft, [key]: Number(e.target.value) })} /></label>)}
    </div><p className="mt-3 text-xs leading-6 text-muted-foreground">三个值须依次增大。更改后保留历史高点、真实回撤和已触发风险状态，并记录修改时间。这里只调整提醒规则；仓位上限单独设置，收益不作保证。</p>{!thresholdsValid && <p role="alert" className="mt-2 text-xs text-red-600">请满足：0 &lt; 预警 &lt; 暂停新增 &lt; 最大回撤阈值 &lt; 100%。</p>}</fieldset>
    {!!audit?.length && <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">最近阈值调整记录</summary>{[...audit].reverse().map(a => <p className="mt-2 leading-6" key={a.changed_at}>{shanghaiTime(a.changed_at)}：预警 {a.before.warning_pct}% → {a.after.warning_pct}%；暂停新增 {a.before.restrict_pct}% → {a.after.restrict_pct}%；最大阈值 {a.before.max_drawdown_pct}% → {a.after.max_drawdown_pct}%</p>)}</details>}
    <div className="grid gap-4 sm:grid-cols-2"><label className="text-xs">盘中轮询间隔（秒）<select className={`${input} mt-2`} value={draft.poll_seconds} onChange={e => setDraft({ ...draft, poll_seconds: Number(e.target.value) })}>{[30, 60, 120, 300].map(v => <option key={v}>{v}</option>)}</select></label><label className="text-xs">龙虎榜池大小<select className={`${input} mt-2`} value={draft.pool_size} onChange={e => setDraft({ ...draft, pool_size: Number(e.target.value) })}>{[5, 10, 20, 30, 50, 60].map(v => <option key={v}>{v}</option>)}</select></label><label className="text-xs">单股仓位提醒上限（%）<input className={`${input} mt-2`} type="number" min="1" max="30" step="1" required value={draft.max_single_position_pct} onChange={e => setDraft({ ...draft, max_single_position_pct: Number(e.target.value) })} /></label></div>
    <label className="block text-xs">补充自选（代码，用逗号分隔）<textarea className={`${input} mt-2`} rows={3} value={watchlist} onChange={e => setWatchlist(e.target.value)} /></label><p className="text-xs leading-6 text-muted-foreground">补充自选和持仓额外取价。龙虎榜约15分钟检查，主板发现池当日盘中约5分钟重新扫描，报价仍按盘中轮询间隔更新。回撤阈值不改变公告、新闻、报价时效和T+1限制。</p>
    <button type="submit" disabled={busy || !thresholdsValid} className={`${button} border-primary/30 bg-primary/10 text-primary`}>保存监控设置</button>
  </form></Panel>;
}
