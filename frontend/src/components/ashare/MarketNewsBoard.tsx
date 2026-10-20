import { Radio, ShieldAlert } from "lucide-react";
import { qualityLabel, shanghaiTime, type MarketNewsView } from "@/lib/ashare";
import { StockNews } from "./NewsPanel";

export function MarketNewsBoard({ radar, disconnected }: {radar?: MarketNewsView; disconnected: boolean}) {
  return <section className="space-y-4" aria-label="主板利好雷达">
    <div className="rounded-xl border border-primary/20 bg-primary/5 p-5">
      <h2 className="flex items-center gap-2 text-base font-semibold"><Radio className="h-4 w-4 text-primary" />主板利好雷达</h2>
      <p className="mt-2 text-sm leading-6">消息发现面向沪深主板，不限原五股。按公司直接关联整理潜在利好，结合量价、公告和账户风控评估；所有下单由你手动操作。</p>
      <div className="mt-3 flex flex-wrap gap-x-5 gap-y-2 text-xs text-muted-foreground">
        <span>{radar?.enabled ? radar.active ? "交易日09:00—21:00采集" : "当前在采集窗口外或已暂停" : "尚未启用"}</span>
        <span>{radar?.directory_valid && !disconnected ? `${radar.universe_total} 只主板名录有效` : "主板名录待核验"}</span>
        <span>名录 {shanghaiTime(radar?.directory_as_of)}</span>
        <span>快讯 {shanghaiTime(radar?.checked_at)} · {radar?.source_fresh && !disconnected ? "检查有效" : "待检查/过期"}</span>
      </div>
      <p className="mt-3 text-xs leading-6 text-muted-foreground">财联社约30秒检查；发现公司后逐批补查东方财富个股新闻、公告索引与报价。模型最多每5分钟一批，共用原资讯分析额度；盘后只做消息评估和次日观察准备。</p>
      <p className="mt-2 text-xs text-muted-foreground">模型整理 {radar?.analysis_running ? "进行中" : shanghaiTime(radar?.analysis_at)} · {radar?.pending_count ?? 0} 只存在待整理消息，排队期间不作利好确认</p>
      <p className="mt-2 text-xs leading-6 text-muted-foreground">{radar?.note}</p>
      {(radar?.directory_error || radar?.analysis_error || disconnected) && <p role="alert" className="mt-3 text-xs text-amber-700">{disconnected ? "页面连接中断，留存结果仅供参考" : [radar?.directory_error, radar?.analysis_error].filter(Boolean).join("；")}</p>}
    </div>
    {!radar?.rows.length && <p className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground">{radar?.directory_valid ? "当前未发现符合范围的直接利好或待整理催化。未发现不等于没有机会或风险。" : "等待同日主板名录核验和快讯匹配，原重点股监控继续独立运行。"}</p>}
    <div className="grid items-start gap-4 xl:grid-cols-2">{radar?.rows.map(row => <article key={row.symbol} className="rounded-xl border border-border bg-card p-5">
      <div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="font-semibold">{row.name} <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">{row.symbol}</span></h3><p className="mt-2 text-xs text-amber-700">{disconnected ? "连接中断，暂停判断" : row.status}</p></div><div className="text-right"><p className="font-mono text-lg">{row.quote?.price.toFixed(2) ?? "—"}</p><p className="mt-1 text-[11px] text-muted-foreground">{qualityLabel[row.quality] || row.quality}</p></div></div>
      <p className="mt-3 text-[11px] text-muted-foreground">报价 {shanghaiTime(row.quote?.as_of)} · 公告索引 {shanghaiTime(row.review.checked_at)}</p>
      <p className="mt-3 flex gap-2 text-xs leading-6 text-amber-700"><ShieldAlert className="mt-1 h-3.5 w-3.5 shrink-0" />公告原文仍待人工核验；当前结果均不作为买入确认。</p>
      {!!row.reasons.length && <p className="mt-2 text-xs leading-6 text-muted-foreground">当前限制：{row.reasons.join("；")}</p>}
      {row.review.error && <p className="mt-2 text-xs text-amber-700">{row.review.error}</p>}
      <details className="mt-3 text-xs leading-6"><summary className="cursor-pointer font-medium">观察条件与失效条件</summary><p className="mt-2 text-muted-foreground">观察：{row.observation_conditions.join("；")}</p><p className="mt-2 text-muted-foreground">失效：{row.invalidation_conditions.join("；")}</p></details>
      <details className="mt-3 text-xs leading-6"><summary className="cursor-pointer font-medium">近30天公告索引 · {row.announcements.length} 条</summary><p className="mt-2 text-muted-foreground">仅标题与链接检查，未发现风险词不能证明公告无风险。</p>{row.announcements.slice(0, 8).map(a => <p key={a.id} className="mt-2">{a.date} · {/^https:\/\/data\.eastmoney\.com\/notices\//.test(a.source_url) ? <a href={a.source_url} target="_blank" rel="noopener noreferrer" className="text-primary underline">{a.title}</a> : a.title}</p>)}</details>
      <StockNews news={{events: row.news.events, healthy: !!radar.source_fresh, blocked: [], has_fresh_catalyst: row.news.events.some(e => e.fresh_catalyst), note: "公司直接关联的新闻整理；模型方向不等于事实核验或上涨概率。"}} disconnected={disconnected} />
    </article>)}</div>
  </section>;
}
