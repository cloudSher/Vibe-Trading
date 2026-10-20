import { RefreshCw } from "lucide-react";
import { qualityLabel, shanghaiTime, type MainboardView } from "@/lib/ashare";

const fmt = (v: number | null | undefined) => v == null ? "—" : v.toFixed(2);

export function MainboardBoard({ board, disconnected, busy, onScan }: {
  board?: MainboardView; disconnected: boolean; busy: boolean; onScan: () => void;
}) {
  const scan = board?.scan;
  const scanning = !!(board?.running || board?.requested);
  return <div className="space-y-4">
    <section className="rounded-xl border border-primary/25 bg-primary/5 p-5">
      <div className="flex flex-wrap items-start justify-between gap-4"><div>
        <h2 className="font-semibold">沪深主板 · 扩展发现池</h2>
        <p className="mt-2 text-xs leading-6 text-muted-foreground">排除创业板、科创板、北交所及 ST / 退市 / 上市初期标记。按代码完整分页，量价过滤后按成交额排序，最多跟踪 20 只。</p>
      </div><button className="inline-flex items-center gap-2 rounded-lg border border-primary/30 bg-card px-3 py-2 text-sm text-primary disabled:opacity-50" disabled={busy || scanning || disconnected} onClick={onScan}><RefreshCw className={`h-4 w-4 ${scanning ? "animate-spin" : ""}`} />{scanning ? "正在扫描主板" : "扫描沪深主板"}</button></div>
      <div className="mt-5 grid grid-cols-2 gap-4 border-t border-primary/15 pt-4 md:grid-cols-4">
        {[["主板名录覆盖", scan?.coverage_complete ? `${scan.received_total} / ${scan.universe_total}` : "尚未完成"], ["扫描时报价时效通过", `${scan?.fresh_count ?? "—"} 只`], ["量价初筛通过", `${scan?.qualifying_total ?? "—"} 只`], ["当前保留", `${board?.rows.length ?? 0} 只`]].map(([label, value]) => <div key={label}><p className="text-xs text-muted-foreground">{label}</p><p className="mt-2 font-mono text-lg font-semibold">{value}</p></div>)}
      </div>
      <p className="mt-4 text-[11px] leading-5 text-muted-foreground">完成时间 {shanghaiTime(scan?.completed_at)} · 当日连续竞价约 5 分钟扫描；入池股票按原行情轮询间隔取价，超过 90 秒失效。原五只重点池继续独立监控。</p>
      {board?.source_warning && <p className="mt-3 text-xs leading-6 text-amber-700">{board.source_warning} 名录核对：{shanghaiTime(scan?.universe_as_of)}；返回有效报价 {scan?.quote_received_total} / {scan?.universe_total}，未返回 {scan?.missing_quote_total} 只。</p>}
      {scanning && <p role="status" className="mt-3 text-xs text-primary">分页进度 {board?.progress?.received ?? 0} / {board?.progress?.total ?? "待返回"}，完整扫描通常需要约一分钟。</p>}
    </section>
    {(disconnected || board?.error || (scan?.completed_at && !board?.valid)) && <p role="alert" className="rounded-lg border border-amber-300/60 bg-amber-500/10 p-4 text-sm">{disconnected ? "连接中断，仅显示留存记录" : board?.error || "主板扫描已过期、非当日或监控暂停；保留结果仅供参考"}</p>}
    <p className="text-xs leading-6 text-muted-foreground">筛选条件：涨幅 0%–7%、成交额至少 1 亿元、换手不高于 20%，站上开盘价与当日均价、偏离均价不超过 2%；高开达 3% 或日内高点回落达 3% 排除。排序代表成交活跃程度，不代表收益或胜率。新候选尚未完成公告原文和新闻核验，不能作为买入确认。</p>
    <div className="overflow-x-auto rounded-xl border border-border bg-card"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="bg-muted/40 text-muted-foreground"><tr>{["候选 / 报价时间", "价格 / 涨幅", "日内均价", "成交额 / 换手", "观察状态"].map(h => <th key={h} className="px-4 py-3 font-medium">{h}</th>)}</tr></thead><tbody>
      {board?.rows.map((row, index) => { const q = row.quote; return <tr key={row.symbol} className="border-t border-border/60"><td className="px-4 py-4"><div><span className="mr-3 font-mono text-muted-foreground">{String(index + 1).padStart(2, "0")}</span><strong>{row.name}</strong><span className="ml-2 font-mono text-muted-foreground">{row.symbol}</span></div><p className="mt-2 text-[10px] text-muted-foreground">{shanghaiTime(q?.as_of)} · {qualityLabel[row.quality]}</p>{row.review && <details className="mt-2 max-w-md whitespace-normal"><summary className="cursor-pointer text-primary">公告 / 新闻索引 · 待原文核验</summary><p className="my-2 text-[10px] text-muted-foreground">检索 {shanghaiTime(row.review.checked_at)} · {row.review.error || "标题风险词只能提示线索，未命中不代表无风险"}</p>{row.review.title_flags.map(flag => <p key={flag} className="my-1 text-amber-700">{flag}</p>)}{row.review.announcements.map((a, i) => <a key={i} className="my-2 block leading-5 text-muted-foreground underline" href={a.source_url} target="_blank" rel="noopener noreferrer">公告 {a.date} · {a.title}</a>)}{row.review.news.map((n, i) => <a key={i} className="my-2 block leading-5 text-muted-foreground underline" href={n.url} target="_blank" rel="noopener noreferrer">{n.source} · {shanghaiTime(n.published_at)} · {n.title}</a>)}</details>}</td><td className="px-4 py-4 font-mono"><p>{fmt(q?.price)}</p><p className={`mt-1 ${(q?.change_pct ?? 0) > 0 ? "text-red-600" : "text-emerald-600"}`}>{fmt(q?.change_pct)}%</p></td><td className="px-4 py-4 font-mono">{disconnected || row.quality !== "fresh" ? "—" : fmt(q?.vwap)}</td><td className="px-4 py-4 font-mono"><p>{q?.amount_yuan == null ? "—" : `${fmt(q.amount_yuan / 1e8)} 亿`}</p><p className="mt-1 text-muted-foreground">{fmt(q?.turnover_pct)}%</p></td><td className="max-w-xs whitespace-normal px-4 py-4"><span className={`rounded px-2 py-1 text-[10px] ${!disconnected && row.market_ready && board.valid ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>{disconnected ? "连接中断" : row.status}</span><p className="mt-2 text-[10px] leading-5 text-muted-foreground">{row.reasons.join("；") || "仍需公告、新闻、回踩确认及账户风控共同核验"}</p></td></tr>; })}
      {!board?.rows.length && <tr><td colSpan={5} className="p-10 text-center text-muted-foreground">{scanning ? "等待完整分页与独立报价" : "点击扫描，建立当日主板候选池；未通过筛选时如实留空。"}</td></tr>}
    </tbody></table></div>
    <details className="text-xs text-muted-foreground"><summary className="cursor-pointer">查看本轮排除原因（同一股票可命中多项）</summary><div className="mt-3 flex flex-wrap gap-x-6 gap-y-3">{Object.entries(scan?.excluded_counts || {}).map(([reason, count]) => <span key={reason}>{reason}：{count}</span>)}</div></details>
  </div>;
}
