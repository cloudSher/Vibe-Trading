import { useCallback, useEffect, useState } from "react";
import { ashareRequest, shanghaiTime } from "@/lib/ashare";

type ResearchRow = { symbol: string; name: string; rank: number; score: number; selected: boolean;
  score_parts: Record<string, number>; decision: string; evidence: {title: string; url: string; published_at: string}[];
  review?: {risk_points?: string[]; evidence_quote?: string; summary?: string; novelty?: string} };
type Audit = { run_id: string; content_hash: string; cutoff: string; target_date: string; reference_date: string;
  candidate_count: number; validation_status: string; selected_symbols: string[]; rows: ResearchRow[];
  method: {note: string}; coverage: {universe_count?: number; reference_quote_count?: number; note?: string;
    theme_status?: string; cls_items?: number; primary_documents_fetched?: number; full_pdf_count?: number; substantively_reviewed_candidates?: number} };
type ResearchSnapshot = {published?: Audit | null; draft?: {content_hash: string; plan: {session_date: string; screening: Audit}} | null;
  error?: string | null; job?: {running: boolean; status: string; progress?: string; error?: string}; outcome?: {selected_coverage: number; candidate_coverage: number; selected_limit_up_count: number;
    selected_mean_day_change_pct?: number | null; selected_mean_open_to_close_pct?: number | null;
    candidate_mean_open_to_close_pct?: number | null; note: string} | null };
const parts: Record<string, string> = {catalyst: "催化", source: "原文", momentum: "价格强度", liquidity: "流动性", theme: "板块关联", crowding_penalty: "拥挤扣分", risk_penalty: "风险扣分"};

export function ResearchBoard() {
  const [data, setData] = useState<ResearchSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState("published");
  const [filter, setFilter] = useState("");
  const [selectedOnly, setSelectedOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {setData(await ashareRequest<ResearchSnapshot>("/screening")); setError(null);}
    catch (e) {setError(e instanceof Error ? e.message : "研究排名读取失败");}
  }, []);
  useEffect(() => {void load(); const timer = window.setInterval(() => void load(), 15000); return () => clearInterval(timer);}, [load]);
  const draft = data?.draft;
  const audit = mode === "draft" ? draft?.plan.screening : data?.published;
  const publish = async () => {
    if (!draft) return;
    setBusy(true);
    try {await ashareRequest("/screening/publish", "POST", {expected_hash: draft.content_hash}); setMode("published"); await load();}
    catch (e) {setError(e instanceof Error ? e.message : "发布失败");}
    finally {setBusy(false);}
  };
  const generate = async () => {setBusy(true);try {setData(await ashareRequest<ResearchSnapshot>("/screening/run","POST"));setError(null);}catch(e){setError(e instanceof Error ? e.message : "研究任务未能启动");}finally{setBusy(false);}};
  const rows = audit?.rows.filter(r => (!selectedOnly || r.selected) && `${r.name} ${r.symbol} ${r.decision}`.includes(filter)) || [];
  const exportCsv = () => {
    if (!audit) return;
    const quote = (x: unknown) => `"${String(x ?? "").replace(/"/g, '""')}"`;
    const csv = ["排名,股票,代码,研究分,入选,分项,决定与落选原因", ...audit.rows.map(r =>
      [r.rank,r.name,r.symbol,r.score,r.selected ? "是" : "否",JSON.stringify(r.score_parts),r.decision].map(quote).join(","))].join("\n");
    const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob(["\uFEFF"+csv], {type:"text/csv;charset=utf-8"}));
    link.download = `${audit.run_id}-候选排名.csv`; link.click(); URL.revokeObjectURL(link.href);
  };
  const button = "rounded-lg border border-border px-3 py-2 text-xs disabled:opacity-50";
  return <section className="space-y-4" aria-label="冻结候选排名与复盘">
    <div className="rounded-xl border border-border bg-card p-5">
      <h2 className="font-semibold">候选排名与复盘</h2>
      <p className="mt-2 text-sm text-muted-foreground">研究排名与盘中执行条件分别记录。原名单保留用于复盘，入选不解除公告、新闻或账户限制。</p>
      <div className="mt-4 flex flex-wrap gap-3"><button className={button} onClick={() => setMode("published")}>已发布排名</button><button className={button} disabled={!draft} onClick={() => setMode("draft")}>查看待发布草案</button><button className={button} disabled={busy || data?.job?.running} onClick={() => void generate()}>{data?.job?.running ? "后台研究进行中…" : "收盘后生成下一交易日草案"}</button><button className={button} onClick={() => void load()}>重新读取</button></div>
      <p className="mt-2 text-xs text-muted-foreground">生成由本地后台完成：公开资料采集、原文摘录校验、模型整理与固定规则排名。新草案需核对后发布；任务失败会保留已发布计划。</p>
      {data?.job?.progress && <p className="mt-2 text-xs text-muted-foreground">{data.job.progress}{data.job.error ? ` · ${data.job.error}` : ""}</p>}
      {(error || data?.error) && <p role="alert" className="mt-3 text-sm text-amber-700">{error || data?.error}；保留内容不能作为当前确认。</p>}
      {!audit && <p className="mt-4 text-sm text-muted-foreground">{mode === "published" ? "当前计划没有冻结候选排名，可查看本地研究草案。" : "暂无经过校验的本地研究草案。"}</p>}
      {audit && <>
        <p className="mt-4 font-medium">{audit.target_date} · {mode === "draft" ? "待发布" : "已发布"} · {audit.candidate_count}只候选 / 5只观察</p>
        <p className="mt-2 text-xs leading-6 text-muted-foreground">证据截止 {shanghaiTime(audit.cutoff)} · 参考日 {audit.reference_date} · 名录 {audit.coverage.universe_count}只 / 参考报价 {audit.coverage.reference_quote_count}只</p>
        <p className="mt-1 text-xs leading-6 text-muted-foreground">{audit.coverage.note} {audit.method.note}</p>
        <p className="mt-1 text-xs leading-6 text-muted-foreground">快讯 {audit.coverage.cls_items ?? "—"}条 · 公告原文或片段 {audit.coverage.primary_documents_fetched ?? "—"}份 · 全文PDF {audit.coverage.full_pdf_count ?? "—"}份 · 主要证据已整理候选 {audit.coverage.substantively_reviewed_candidates ?? "—"}只。{audit.coverage.theme_status}</p>
        <p className="mt-1 text-xs text-amber-700">{audit.validation_status}；研究分不是胜率、涨停概率或买入信号。</p>
        <p className="mt-2 font-mono text-[11px] text-muted-foreground">{audit.run_id} · {audit.content_hash.slice(0,16)}</p>
        {mode === "draft" && <button className={`${button} mt-4 border-primary text-primary`} disabled={busy || !!error || !!data?.error} onClick={() => void publish()}>{busy ? "正在发布…" : "发布这份统一观察计划"}</button>}
      </>}
    </div>
    {audit && <>
      <div className="flex flex-wrap items-center gap-3"><input aria-label="搜索候选与落选原因" className="rounded-lg border border-border bg-background px-3 py-2 text-sm" placeholder="股票、代码或落选原因" value={filter} onChange={e=>setFilter(e.target.value)} /><label className="text-xs"><input type="checkbox" checked={selectedOnly} onChange={e=>setSelectedOnly(e.target.checked)} /> 只看入选五股</label><button className={button} onClick={exportCsv}>导出全部排名 CSV</button><span className="text-xs text-muted-foreground">显示 {rows.length} / {audit.rows.length}</span></div>
      <div className="overflow-x-auto rounded-xl border border-border bg-card"><table className="w-full min-w-[700px] text-left text-xs"><thead><tr className="bg-muted/40"><th className="p-3">排名 / 股票</th><th className="p-3">研究分</th><th className="p-3">分项依据</th><th className="p-3">入选 / 落选原因与原文</th></tr></thead><tbody>{rows.map(r=><tr key={r.symbol} className="border-t border-border"><td className="p-3 align-top"><strong>{r.rank} · {r.name}</strong><p className="mt-1 font-mono text-muted-foreground">{r.symbol}</p></td><td className="p-3 align-top font-mono">{r.score}</td><td className="max-w-56 p-3 align-top leading-6">{Object.entries(r.score_parts).map(([k,v])=><span key={k} className="mr-2 inline-block">{parts[k] || k} {v}</span>)}</td><td className="max-w-lg p-3 align-top leading-6"><p className={r.selected ? "font-medium text-primary" : ""}>{r.selected ? "入选 · " : "落选/候补 · "}{r.decision}</p>{r.review?.summary && <p>{r.review.summary}</p>}{!!r.review?.risk_points?.length && <p className="text-amber-700">风险：{r.review.risk_points.join("；")}</p>}<details className="mt-1"><summary className="cursor-pointer text-muted-foreground">原文依据 · {r.evidence.length}条</summary>{r.review?.evidence_quote && <p className="mt-2 border-l-2 border-border pl-3">{r.review.evidence_quote}</p>}{r.evidence.slice(0,8).map(e=><p key={e.url}><a className="text-primary underline" href={e.url} target="_blank" rel="noreferrer">{e.title}</a> · {shanghaiTime(e.published_at)}</p>)}</details></td></tr>)}</tbody></table></div>
    </>}
    {mode === "published" && <div className="rounded-xl border border-border bg-card p-5"><h3 className="text-sm font-semibold">冻结名单的结果评估</h3>{data?.outcome ? <><p className="mt-3 text-sm">五股覆盖 {data.outcome.selected_coverage}/5 · 候选覆盖 {data.outcome.candidate_coverage} · 五股收盘涨停 {data.outcome.selected_limit_up_count}只</p><p className="mt-2 text-sm">五股平均日涨幅 {data.outcome.selected_mean_day_change_pct ?? "—"}% · 开盘至收盘 {data.outcome.selected_mean_open_to_close_pct ?? "—"}% · 全候选开盘至收盘 {data.outcome.candidate_mean_open_to_close_pct ?? "—"}%</p><p className="mt-2 text-xs text-muted-foreground">{data.outcome.note}</p></> : <p className="mt-3 text-xs leading-6 text-muted-foreground">等待目标日收盘后的真实报价。后台会在本地运行时采集冻结候选结果；未取得数据不补零、不计作策略盈利。盘中实际观察状态变化另行留档。</p>}</div>}
  </section>;
}
