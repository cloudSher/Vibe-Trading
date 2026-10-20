import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { MainboardBoard } from "../MainboardBoard";
import type { MainboardView } from "@/lib/ashare";

afterEach(cleanup);

it("shows complete coverage separately from eligible quotes and pending original review", () => {
  const board: MainboardView = { enabled: true, valid: true, note: "", scan: {
    coverage_complete: true, received_total: 3488, universe_total: 3488, fresh_count: 3100, qualifying_total: 100,
  }, rows: [] };
  render(<MainboardBoard board={board} disconnected={false} busy={false} onScan={() => {}} />);
  expect(screen.getByText("3488 / 3488")).toBeInTheDocument();
  expect(screen.getByText("3100 只")).toBeInTheDocument();
  expect(screen.getByText(/新候选尚未完成公告原文和新闻核验/)).toBeInTheDocument();
});

it("prevents scans while disconnected and identifies retained snapshots", () => {
  render(<MainboardBoard disconnected busy={false} onScan={() => {}} />);
  expect(screen.getByRole("button", { name: "扫描沪深主板" })).toBeDisabled();
  expect(screen.getByRole("alert")).toHaveTextContent("连接中断，仅显示留存记录");
});
