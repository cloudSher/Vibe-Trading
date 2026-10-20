import { useEffect, useState } from "react";
import { Link } from "react-router";
import { ashareRequest, shanghaiTime, type ObservationPlanSnapshot } from "@/lib/ashare";

export function ObservationPlanCard() {
  const [data, setData] = useState<ObservationPlanSnapshot | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    const load = () => ashareRequest<ObservationPlanSnapshot>("/plan", "GET", undefined, controller.signal)
      .then(value => { setData(value); setError(false); }).catch(() => { if (!controller.signal.aborted) setError(true); });
    void load();
    const timer = window.setInterval(load, 15_000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, []);
  const focus = data?.focus;
  const riskLabels: Record<string, string> = {critical: "风险锁定，暂停新增", incomplete: "账户资料待补齐", restrict: "暂停新增", warning: "回撤预警", normal: "未触发回撤预警"};
  return <section className="rounded-xl border border-primary/25 bg-primary/5 p-5" aria-label="A股统一观察计划">
    <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold">A 股观察计划</h2><Link className="text-sm text-primary underline" to="/ashare">查看观察条件、失效条件与实时风险 →</Link></div>
    {error ? <p role="alert" className="mt-3 text-sm text-amber-700">计划读取失败，保留内容不能作为当前确认，请到观察台核对。</p> : null}
    {focus?.plan_id ? <>
      <p className="mt-3 text-sm">{focus.session_date} · {focus.plan_id} · v{focus.version} · {focus.lifecycle === "expired" ? "已到期" : "已发布"}</p>
      <p className="mt-2 text-sm">{focus.rows.map(r => `${r.name} ${r.symbol}`).join(" · ")}</p>
      <p className="mt-2 text-xs text-muted-foreground">研究截止 {shanghaiTime(focus.research_cutoff)} · 校验 {focus.content_hash?.slice(0, 12)} · 最大回撤阈值 {data?.risk_policy.max_drawdown_pct}% · 风险状态 {error ? "待核对" : riskLabels[data?.risk_level || ""] || "待核对"}</p>
      <p className="mt-2 text-xs text-muted-foreground">计划只用于观察与手动决策；行情、公告、新闻及账户状态在观察台持续更新。研究计划不包含回测收益。</p>
      {focus.rows.some(r => r.research) && <p className="mt-2 text-xs text-muted-foreground">已保存全部候选排名、分项与落选原因；到观察台“候选排名与复盘”核对。研究分不是涨停概率。</p>}
    </> : <p className="mt-3 text-sm text-muted-foreground">{error ? "等待恢复" : "等待读取已发布的观察计划"}</p>}
    {!!data?.archives.length && <details className="mt-3 text-xs text-muted-foreground"><summary className="cursor-pointer">历史计划与公告限制（已归档）</summary>{data.archives.map((p, i) => <p key={i} className="mt-2 leading-6">{p.session_date} · {p.stocks.map(s => `${s.name}：${s.disclosure_summary}`).join("；")}</p>)}</details>}
  </section>;
}
