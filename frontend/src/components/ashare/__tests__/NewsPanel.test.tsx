import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { NewsStatus, StockNews } from "../NewsPanel";
import type { StockNewsView } from "@/lib/ashare";

afterEach(cleanup);
const news: StockNewsView = { healthy: true, blocked: [], has_fresh_catalyst: false, note: "仅为行业背景",
  events: [{ id: "one", title: "行业发布新产品", source: "财联社", url: "https://www.cls.cn/detail/1",
    published_at: "2026-10-19T09:50:00+08:00", first_seen_at: "2026-10-19T09:50:30+08:00",
    relation: { scope: "sector", matched: ["汽车电子"] }, snippet: "某公司发布新产品，尚无订单信息",
    risk_flag: true, fresh_catalyst: false, reference_only: false, market_report: false,
    analysis: { impact: "uncertain", summary: "不能推断重点公司获得订单", evidence: "尚无订单信息" } }] };

it("labels sector association and displays original evidence without treating it as a company catalyst", () => {
  render(<StockNews news={news} disconnected={false} />);
  expect(screen.getByText("仅行业关联")).toBeVisible();
  expect(screen.getByText("模型方向待核验：不能推断重点公司获得订单")).toBeVisible();
  expect(screen.getByText("原文依据：尚无订单信息")).toBeVisible();
  expect(screen.getByRole("link", { name: "查看新闻原文" })).toHaveAttribute("href", "https://www.cls.cn/detail/1");
});
it("does not show source health as valid after losing connection", () => {
  render(<NewsStatus disconnected news={{ enabled: true, sources: { cls: { fresh: true, count: 20 } } }} />);
  expect(screen.getByText("0/1 来源时效通过")).toBeVisible();
  expect(screen.queryByText("检查有效")).not.toBeInTheDocument();
});
it("blocks unsafe links and states retained news is reference only when disconnected", () => {
  const copied = structuredClone(news); copied.events[0].url = "https://attacker.test/source";
  render(<StockNews news={copied} disconnected />);
  expect(screen.getByText("连接中断，保留新闻仅供参考")).toBeVisible();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});
it("shows the recurring window while keeping an expired trading plan blocked", () => {
  render(<NewsStatus disconnected={false} news={{ enabled: true, sources: {}, monitoring: {
    recurring: true, enabled: true, active: true, session_date: "2026-10-20", trading_plan_current: false,
    trading_plan_date: "2026-10-19", calendar_verified: true, start: "09:00", end: "21:00",
  } }} />);
  expect(screen.getByText(/每个交易日北京时间09:00—21:00持续评估/)).toBeVisible();
  expect(screen.getByText(/今日交易预案待核验/)).toBeVisible();
  expect(screen.getByText(/资讯日期 2026-10-20/)).toBeVisible();
});
