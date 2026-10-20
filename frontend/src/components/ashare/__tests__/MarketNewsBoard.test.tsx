import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { MarketNewsBoard } from "../MarketNewsBoard";
import type { MarketNewsView } from "@/lib/ashare";

afterEach(cleanup);
const radar: MarketNewsView = {enabled: true, active: true, scope: "sh_sz_mainboard", directory_valid: true,
  universe_total: 3488, directory_as_of: "2026-10-19T10:00:00+08:00", source_fresh: true,
  candidate_limit: 40, note: "公开快讯不保证全量覆盖", rows: [{symbol: "600000.SH", name: "测试股票", quality: "missing", quote: null,
    market_ready: false, entry_ready: false, status: "利好背景观察；条件未通过", reasons: ["公告原文待核验"],
    news: {events: [{id: "one", title: "测试股票新订单", source: "财联社", url: "https://www.cls.cn/detail/1",
      published_at: "2026-10-19T15:45:00+08:00", first_seen_at: "2026-10-19T15:45:30+08:00", relation: {scope: "direct", matched: ["测试股票"]},
      snippet: "测试股票签署新订单", risk_flag: false, reference_only: false, fresh_catalyst: true, market_report: false,
      analysis: {impact: "positive", summary: "仅按报道判断正向，待公告核验", evidence: "签署新订单"}}]},
    observation_conditions: ["仅连续竞价有效报价"], invalidation_conditions: ["更正或否认即失效"],
    review: {}, announcements: [{id: "unsafe", date: "2026-10-19", title: "外部索引", source_url: "javascript:alert(1)"}]}]};

it("shows broad discovery, source evidence and unconfirmed manual observation", () => {
  render(<MarketNewsBoard radar={radar} disconnected={false} />);
  expect(screen.getByText("3488 只主板名录有效")).toBeVisible();
  expect(screen.getByText("测试股票")).toBeVisible();
  expect(screen.getByText(/当前结果均不作为买入确认/)).toBeVisible();
  expect(screen.getByText("模型正向判断：仅按报道判断正向，待公告核验")).toBeInTheDocument();
  expect(screen.getByRole("link", {name: "查看新闻原文", hidden: true})).toHaveAttribute("href", "https://www.cls.cn/detail/1");
  expect(screen.queryByRole("link", {name: "外部索引", hidden: true})).not.toBeInTheDocument();
});
it("never renders retained health as current after disconnection", () => {
  render(<MarketNewsBoard radar={radar} disconnected />);
  expect(screen.queryByText("3488 只主板名录有效")).not.toBeInTheDocument();
  expect(screen.getByText("连接中断，暂停判断")).toBeVisible();
});
