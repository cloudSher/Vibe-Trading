import { qualityLabel, shanghaiTime, type FocusView, type NewsView } from "@/lib/ashare";
import { NewsStatus, StockNews } from "./NewsPanel";

const reviewLabels: Record<string, string> = { reviewed: "已核验，仍有市场风险", caution: "已核验，有风险提示", blocked: "公告风险：暂停买入观察", pending: "公告待核验" };
const number = (value: number | null | undefined) => value == null ? "—" : value.toFixed(2);

export function FocusBoard({ focus, news, disconnected }: { focus?: FocusView; news?: NewsView; disconnected: boolean }) {
  if (!focus?.configured) return <p className="rounded-xl border border-border bg-card p-6 text-sm text-muted-foreground">尚未配置有日期的重点监控计划。</p>;
  return <div className="space-y-4">
    <section className="rounded-xl border border-primary/25 bg-primary/5 p-4">
      <h2 className="font-semibold">{focus.session_date} 重点监控池 · {focus.rows.length} 只</h2>
      {focus.plan_id && <div className="mt-3 space-y-1 text-xs leading-6" data-testid="published-plan">
        <p className="font-medium">统一观察计划 · {focus.plan_id} · v{focus.version} {focus.lifecycle === "expired" ? "（已到期，不顺延）" : "（已发布）"}</p>
        <p className="text-muted-foreground">研究截止 {shanghaiTime(focus.research_cutoff)} · 发布 {shanghaiTime(focus.published_at)} · 校验 {focus.content_hash?.slice(0, 12)}</p>
        <p>页面、Codex 提醒和会话使用此计划。新增主板资讯候选单独展示，不自动替换这五只。</p>
        {focus.risk_policy && <p className="text-amber-700 dark:text-amber-400">账户风控：{focus.risk_policy.selection_allowed && !disconnected ? "规则通过" : "新增观察确认受限"} · {focus.risk_policy.warning_pct}% 预警 / {focus.risk_policy.restrict_pct}% 暂停新增 / {focus.risk_policy.max_drawdown_pct}% 最大阈值 · {focus.risk_policy.drawdown_basis === "monitoring_period" ? "新阶段基准" : "原历史高点基准"}。历史亏损记录保留。</p>}
      </div>}
      <p className="mt-2 text-xs leading-6 text-muted-foreground">{focus.rule_note} 上日均价参考来自 {focus.reference_date}，不会冒充当日均价。高开达到 {focus.max_open_gap_pct}% 时采用保守规则，停止自动确认。</p>
      <p className="mt-2 text-xs text-muted-foreground">公告核验 {shanghaiTime(focus.reviewed_at)} · 公告检查 {shanghaiTime(focus.disclosures_checked_at)} · {focus.disclosures_fresh && !disconnected ? "检查有效" : "等待有效公告检查，暂停买入观察提醒"}</p>
      {focus.disclosure_error && <p role="alert" className="mt-2 text-xs text-amber-700">{focus.disclosure_error}</p>}
    </section>
    <NewsStatus news={news} disconnected={disconnected} />
    {focus.rows.map((row, index) => <section key={row.symbol} className={`rounded-xl border bg-card p-4 ${row.entry_ready && !disconnected ? "border-primary" : "border-border"}`}>
      <div className="flex flex-wrap items-center justify-between gap-2"><div className="flex items-center gap-2"><span className="font-mono text-xs text-muted-foreground">0{index + 1}</span><h3 className="font-semibold">{row.name}</h3><span className="font-mono text-xs text-muted-foreground">{row.symbol}</span></div><span className={`rounded px-2 py-1 text-xs ${row.entry_ready && !disconnected ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>{disconnected ? "连接中断" : row.status}</span></div>
      {row.reason && <p className="mt-3 text-sm leading-6">{row.reason}</p>}
      {row.research && <p className="mt-2 text-xs text-primary">冻结候选排名 {row.research.rank} · 研究分 {row.research.score}（非胜率） · 全部排名与落选原因见“候选排名与复盘”</p>}
      {row.catalyst_published_at && <p className="mt-1 text-xs text-muted-foreground">催化公开时间 {shanghaiTime(row.catalyst_published_at)} · 计划背景，不能当作当前新消息</p>}
      {!!row.risk_points?.length && <p className="mt-2 text-xs leading-6 text-amber-700 dark:text-amber-400">风险：{row.risk_points.join("；")}</p>}
      <div className="mt-4 grid grid-cols-3 gap-3 text-xs"><div className="text-muted-foreground">最新价<div className="mt-1 font-mono text-base text-foreground">{number(row.quote?.price)}</div></div><div className="text-muted-foreground">上日参考均价<div className="mt-1 font-mono text-base text-foreground">{number(row.reference_vwap)}</div></div><div className="text-muted-foreground">当日估算均价<div className="mt-1 font-mono text-base text-foreground">{row.quality === "fresh" && !disconnected ? number(row.quote?.vwap) : "—"}</div></div></div>
      <p className="mt-3 text-[11px] text-muted-foreground">{qualityLabel[row.quality]} · 报价源时间 {shanghaiTime(row.quote?.as_of)} · {focus.plan_id ? `完整5分钟K线 ${shanghaiTime(row.price_evidence?.bar_as_of)}` : `连续确认 ${row.confirmations}/3`}</p>
      {row.price_evidence?.retest_at && <p className="mt-1 text-xs text-muted-foreground">放量突破 {shanghaiTime(row.price_evidence.breakout_at)} · 量比 {row.price_evidence.volume_ratio} · 回踩确认 {shanghaiTime(row.price_evidence.retest_at)}</p>}
      {row.price_evidence?.source_warning && <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">{row.price_evidence.source_warning}</p>}
      {row.risk_status && <p className="mt-2 text-xs font-medium">风险状态：{disconnected ? "连接中断，不能确认" : row.risk_status}</p>}
      <p className="mt-2 text-xs leading-6 text-muted-foreground">{row.reasons.join("；") || (row.entry_ready ? "只提示观察条件满足，仍需本人核对并手动操作。" : "行情条件满足，等待账户风控通过。")}</p>
      {row.observation_conditions && <details className="mt-3 text-xs" open={!!focus.plan_id || (row.entry_ready && !disconnected)}><summary className="cursor-pointer font-medium">买入观察条件 / 失效条件</summary><div className="mt-3 grid gap-4 md:grid-cols-2"><div><h4 className="font-medium text-primary">满足后仅提示观察</h4><ul className="mt-2 list-inside list-disc space-y-2 leading-6 text-muted-foreground">{row.observation_conditions.map(text => <li key={text}>{text}</li>)}</ul></div><div><h4 className="font-medium text-amber-700 dark:text-amber-400">任一失效都需重新核对</h4><ul className="mt-2 list-inside list-disc space-y-2 leading-6 text-muted-foreground">{row.invalidation_conditions?.map(text => <li key={text}>{text}</li>)}</ul></div></div></details>}
      <StockNews news={row.news} disconnected={disconnected} />
      <details className="mt-3 border-t border-border pt-3 text-xs" open={row.disclosure_status === "blocked" || row.new_disclosure_ids.length > 0}>
        <summary className="cursor-pointer font-medium">{reviewLabels[row.disclosure_status]}{row.new_disclosure_ids.length ? ` · 新增 ${row.new_disclosure_ids.length} 份待核验` : ""}</summary>
        <p className="mt-2 whitespace-pre-wrap leading-6 text-muted-foreground">{row.disclosure_summary}</p>
        <div className="mt-2 flex flex-wrap gap-3">{row.source_urls.filter(url => url.startsWith("https://")).map((url, i) => <a key={url} href={url} target="_blank" rel="noopener noreferrer" className="text-primary underline">核验来源 {i + 1}</a>)}</div>
        <ul className="mt-2 space-y-1 text-muted-foreground">{row.announcements.map(item => <li key={item.id}><a href={item.source_url} target="_blank" rel="noopener noreferrer">{item.date} · {item.title}{row.new_disclosure_ids.includes(item.id) ? "（新增待核验）" : ""}</a></li>)}</ul>
      </details>
    </section>)}
    <p className="text-xs leading-6 text-muted-foreground">公告检索仅覆盖所列公开来源与时间范围。新公告、采集失败、过期报价或账户限制都会阻止买入观察确认；新买入 A 股受 T+1 约束，失效提醒不等于可当日卖出。</p>
  </div>;
}
