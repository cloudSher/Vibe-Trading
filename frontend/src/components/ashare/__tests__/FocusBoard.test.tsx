import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { FocusBoard } from "../FocusBoard";
import type { FocusView } from "@/lib/ashare";

afterEach(() => { cleanup(); });
function focus(): FocusView {
  return { configured: true, enabled: true, session_date: "2026-10-19", reference_date: "2026-10-16",
    reviewed_at: "2026-10-18T16:00:00+08:00", disclosures_checked_at: "2026-10-19T10:00:00+08:00",
    disclosures_fresh: true, max_open_gap_pct: 3, rule_note: "报价确认规则", rows: [{ symbol: "001696.SZ", name: "宗申动力",
      reference_close: 15.87, reference_vwap: 15.45, disclosure_status: "blocked", disclosure_summary: "重大资产置换仍存在不确定性",
      source_urls: ["https://data.eastmoney.com/notice", "javascript:alert(1)"], quote: null, quality: "missing",
      market_ready: false, entry_ready: false, status: "待确认", confirmations: 0, new_disclosure_ids: [],
      reasons: ["公告风险暂停买入观察"], announcements: [] }] };
}
it("shows material risk details and filters unsafe source links", () => {
  render(<FocusBoard focus={focus()} disconnected={false} />);
  expect(screen.getByText("重大资产置换仍存在不确定性")).toBeVisible();
  expect(screen.getByText("公告风险暂停买入观察")).toBeInTheDocument();
  expect(screen.getAllByRole("link")).toHaveLength(1);
  expect(screen.getByText("当日估算均价").parentElement).toHaveTextContent("—");
});
it("does not display a retained entry confirmation after losing connection", () => {
  const data = focus(); data.rows[0].entry_ready = true; data.rows[0].status = "买入观察条件满足";
  render(<FocusBoard focus={data} disconnected={true} />);
  expect(screen.getByText("连接中断")).toBeInTheDocument();
  expect(screen.queryByText("买入观察条件满足")).not.toBeInTheDocument();
});
it("shows the published version, actual conditions and risk status together", () => {
  const data = focus();
  Object.assign(data, { plan_id: "mainboard-2026-10-20", version: 1, content_hash: "abcdef1234567890", lifecycle: "current",
    research_cutoff: "2026-10-19T23:09:49+08:00", published_at: "2026-10-20T00:30:00+08:00",
    risk_policy: { level: "critical", selection_allowed: false, drawdown_basis: "historical_high", warning_pct: 12, restrict_pct: 16, max_drawdown_pct: 20 } });
  Object.assign(data.rows[0], { reason: "公告背景", risk_status: "限制中", risk_points: ["公告待原文核验"],
    observation_conditions: ["放量突破 26.12 后回踩"], invalidation_conditions: ["跌破25.65取消观察"] });
  render(<FocusBoard focus={data} disconnected={false} />);
  expect(screen.getByTestId("published-plan")).toHaveTextContent("mainboard-2026-10-20 · v1");
  expect(screen.getByTestId("published-plan")).toHaveTextContent("20% 最大阈值");
  expect(screen.getByText("放量突破 26.12 后回踩")).toBeVisible();
  expect(screen.getByText("跌破25.65取消观察")).toBeVisible();
  expect(screen.getByText("风险状态：限制中")).toBeVisible();
});
