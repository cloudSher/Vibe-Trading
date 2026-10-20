import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, expect, it, vi } from "vitest";
import { ObservationPlanCard } from "../ObservationPlanCard";
const request = vi.hoisted(() => vi.fn());
vi.mock("@/lib/ashare", async original => ({ ...await original<typeof import("@/lib/ashare")>(), ashareRequest: request }));
afterEach(() => { cleanup(); request.mockReset(); });
it("reads the same canonical plan and links reports to live conditions", async () => {
  request.mockResolvedValue({ focus: {plan_id: "mainboard-2026-10-20", version: 1, session_date: "2026-10-20",
    content_hash: "abcdef1234567890", rows: ["神火股份", "璞泰来", "金发科技", "鹏鼎控股", "骏亚科技"].map((name, i) => ({name, symbol: String(i)}))},
    risk_policy: {max_drawdown_pct: 20}, risk_level: "critical", archives: [] });
  render(<MemoryRouter><ObservationPlanCard /></MemoryRouter>);
  expect(await screen.findByText(/神火股份 0/)).toHaveTextContent("骏亚科技 4");
  expect(request.mock.calls[0][0]).toBe("/plan");
  expect(screen.getByRole("link")).toHaveAttribute("href", "/ashare");
  expect(screen.getByText(/最大回撤阈值/)).toHaveTextContent("20%");
});
it("shows read failures rather than presenting a current confirmation", async () => {
  request.mockRejectedValue(new Error("offline"));
  render(<MemoryRouter><ObservationPlanCard /></MemoryRouter>);
  expect(await screen.findByRole("alert")).toHaveTextContent("计划读取失败");
});
