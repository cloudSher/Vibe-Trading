import { Newspaper, Radio, ShieldAlert } from "lucide-react";
import { shanghaiTime, type NewsView, type StockNewsView } from "@/lib/ashare";

const impact: Record<string, string> = { positive: "正向判断", negative: "负向判断", neutral: "中性整理", uncertain: "方向待核验" };
const safeLink = (url: string) => /^https:\/\/(?:[\w-]+\.)*(?:eastmoney\.com|cls\.cn)\//.test(url);

export function NewsStatus({ news, disconnected }: { news?: NewsView; disconnected: boolean }) {
  if (!news?.enabled) return null;
  const sources = Object.entries(news.sources);
  const healthy = sources.filter(([, source]) => source.fresh && !disconnected).length;
  return <section className="overflow-hidden rounded-xl border border-border bg-card">
    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/30 px-4 py-3">
      <h2 className="flex items-center gap-2 text-sm font-semibold"><Radio className="h-4 w-4 text-primary" />快讯与个股新闻</h2>
      <span className="font-mono text-xs text-muted-foreground">{healthy}/{sources.length} 来源时效通过</span>
    </div>
    <div className="space-y-3 p-4">
      <p className="text-xs leading-6 text-muted-foreground">{news.monitoring?.recurring ? "每个交易日北京时间09:00—21:00持续评估，直到暂停；休市日跳过，盘后只做资讯与公告评估。" : "目标日盘中采集。"}财联社约30秒检查，个股新闻约5分钟检查；仅有新关联事件时调用模型，最多每5分钟一批。</p>
      {news.monitoring?.recurring && <p className="text-xs leading-6 text-muted-foreground">{news.monitoring.active ? "当前在资讯采集窗口内" : "当前不在采集窗口内或监控已暂停"} · 资讯日期 {news.monitoring.session_date} · 交易预案日期 {news.monitoring.trading_plan_date || "未配置"}</p>}
      {news.monitoring?.recurring && !news.monitoring.trading_plan_current && <p className="text-xs leading-6 text-amber-700">今日交易预案待核验，旧参考位与买入观察条件已到期；资讯继续评估，不能沿用旧日期买点。</p>}
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">{sources.map(([key, source]) => <div key={key} className="border-l-2 border-border pl-3 text-[11px] leading-5">
        <div className="flex items-center justify-between gap-2"><span className="font-medium">{key === "cls" ? "财联社电报" : "东方财富 " + key.replace("eastmoney_", "")}</span><span className={source.fresh && !disconnected ? "text-emerald-700 dark:text-emerald-400" : "text-amber-700 dark:text-amber-400"}>{source.fresh && !disconnected ? "检查有效" : "待检查/过期"}</span></div>
        <div className="text-muted-foreground">抓取 {shanghaiTime(source.checked_at)} · 本轮 {source.count ?? "—"} 条</div>
        <div className="text-muted-foreground">最新源时间 {shanghaiTime(source.latest_published_at)}</div>
        {source.error && <div className="text-amber-700">{source.error}</div>}
      </div>)}</div>
      <p className="text-xs text-muted-foreground">模型整理 {news.analysis_running ? "进行中" : shanghaiTime(news.analysis_at)} · 待整理 {news.pending_count ?? 0} 条 · 保留 {news.event_count ?? 0} 条来源记录</p>
      {news.analysis_error && <p role="alert" className="text-xs text-amber-700">{news.analysis_error}，待核验事项继续暂停确认。</p>}
      <p className="text-[11px] leading-5 text-muted-foreground">{news.note}</p>
    </div>
  </section>;
}

export function StockNews({ news, disconnected }: { news?: StockNewsView | null; disconnected: boolean }) {
  if (!news) return null;
  return <div className="mt-4 border-t border-border pt-4">
    <div className="flex items-center gap-2 text-xs font-medium"><Newspaper className="h-3.5 w-3.5 text-primary" />关联新闻 <span className="font-normal text-muted-foreground">{news.events.length} 条近72小时记录</span></div>
    <p className="mt-2 text-xs leading-6 text-muted-foreground">{disconnected ? "连接中断，保留新闻仅供参考" : news.note}</p>
    {!!news.blocked.length && <p className="mt-2 flex items-start gap-1.5 text-xs leading-6 text-amber-700 dark:text-amber-400"><ShieldAlert className="mt-1 h-3.5 w-3.5 shrink-0" />{news.blocked.join("；")}</p>}
    {!news.events.length && <p className="mt-2 text-xs text-muted-foreground">本轮未匹配到近72小时相关新闻，不能据此认定没有风险。</p>}
    <div className="mt-3 space-y-3">{news.events.map(item => <details key={item.id} className="rounded-lg border border-border/70 px-3 py-2" open={item.risk_flag}>
      <summary className="cursor-pointer text-xs leading-6"><span className="mr-2 rounded bg-muted px-1.5 py-0.5 text-[10px]">{item.relation.scope === "direct" ? "直接相关" : "仅行业关联"}</span>{item.title}</summary>
      <div className="mt-2 space-y-2 border-t border-border/60 pt-2 text-[11px] leading-6 text-muted-foreground">
        <p>{item.source} · 发布 {shanghaiTime(item.published_at)} · 首次抓取 {shanghaiTime(item.first_seen_at)}</p>
        <p>{item.market_report ? "行情统计参考" : item.reference_only ? "旧闻/盘前背景，不作为新增盘中催化" : "近30分钟内发布，仍需核验"} · 匹配 {item.relation.matched.join("、")}</p>
        <p>{item.snippet}</p>
        {item.market_report ? <p>量价统计仅供复盘，不作为独立新闻催化。</p> : item.analysis ? <><p className="text-foreground">模型{impact[item.analysis.impact]}：{item.analysis.summary}</p><blockquote className="border-l-2 border-primary/30 pl-3">原文依据：{item.analysis.evidence}</blockquote></> : <p>尚未完成模型整理；直接相关新闻待核验时暂停观察确认。</p>}
        {safeLink(item.url) && <a href={item.url} target="_blank" rel="noopener noreferrer" className="text-primary underline underline-offset-2">查看新闻原文</a>}
      </div>
    </details>)}</div>
  </div>;
}
