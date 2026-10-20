import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Ashare } from "../Ashare";
import { ashareRequest, type AshareSnapshot } from "@/lib/ashare";

vi.mock("react-router", () => ({ useNavigate: () => vi.fn() }));
vi.mock("@/lib/ashare", async (original) => ({ ...await original<typeof import("@/lib/ashare")>(), ashareRequest: vi.fn() }));

function snapshot(): AshareSnapshot {
  return {
    settings: { enabled: true, poll_seconds: 30, pool_size: 30, watchlist: [], max_single_position_pct: 20 },
    market: { now: "2026-10-19T10:00:00+08:00", phase: "continuous", label: "连续竞价", live_window: true,
      continuous: true, calendar_verified: true, latest_completed_session: "2026-10-16" },
    monitor_running: true, refreshed_at: "2026-10-19T10:00:00+08:00", errors: {},
    billboard: { date: "2026-10-16", selected: 0, pool_valid: true }, rows: [], quotes: [], account: null,
    risk: { level: "incomplete", configured: false, initialized: false, complete: false, equity: null,
      drawdown_pct: null, max_drawdown_pct: 5, latched: false, selection_allowed: false,
      guidance: "请先录入现金、持仓及净值高点；账户风险尚未启用", basis: "live_quote", holdings: [] },
    alerts: [], unread_alerts: 0, notice: "公开报价不保证交易所级延迟",
  };
}

beforeEach(() => { vi.mocked(ashareRequest).mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("A-share manual observation workbench", () => {
  it("shows period zero drawdown alongside retained historical loss", async () => {
    const data = snapshot();
    Object.assign(data.risk, { complete: true, drawdown_pct: 0, historical_drawdown_pct: 25,
      historical_peak_equity_equivalent: 1000000, historical_gap_to_peak: 250000, historical_latched: true,
      monitoring_period: { id: "period-001", started_at: data.market.now, account_confirmed_at: data.market.now,
        start_equity: 750000, valuation_basis: "live_quote", history_at_start: { drawdown_pct: 25, peak_equity_equivalent: 1000000, breached: true } } });
    vi.mocked(ashareRequest).mockResolvedValue(data);
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "账户风控" }));
    expect(screen.getByText("本阶段回撤 / 阈值")).toBeInTheDocument();
    expect(screen.getByText("25.00%")).toBeInTheDocument();
    expect(screen.getByText("历史高点口径（保留）")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "按已确认净值建立新阶段" })).not.toBeInTheDocument();
    expect(ashareRequest).not.toHaveBeenCalledWith("/risk-period", "POST", expect.anything());
  });

  it("requires saved complete account confirmation and retries period creation with the same ID", async () => {
    const data = snapshot();
    data.account = { submission_id: "account-001", cash: 750000, positions: [], historical_peak_equity: 1000000,
      peak_as_of: "2026-10-16T15:00:00+08:00", confirmed_at: data.market.now, external_flow: 0 };
    Object.assign(data.risk, { complete: true, configured: true, initialized: true, equity: 750000 });
    const requests: unknown[] = [];
    vi.mocked(ashareRequest).mockImplementation(async (route, _method, body) => {
      if (route === "/risk-period") { requests.push(body); throw new Error("响应丢失"); }
      return data;
    });
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "账户风控" }));
    const start = screen.getByRole("button", { name: "按已确认净值建立新阶段" });
    expect(start).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(start);
    await screen.findByText("响应丢失");
    fireEvent.click(start);
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1]).toEqual(requests[0]);
    expect(requests[0]).toMatchObject({ expected_account_submission_id: "account-001", confirm_account_complete: true });
    expect(requests[0]).not.toHaveProperty("start_equity");
  });

  it("renders configured thresholds instead of a fixed five percent limit", async () => {
    const data = snapshot();
    Object.assign(data.risk, { warning_pct: 12, restrict_pct: 16, max_drawdown_pct: 20 });
    vi.mocked(ashareRequest).mockResolvedValue(data);
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "账户风控" }));
    expect(screen.getByText("20% 回撤规则")).toBeInTheDocument();
    expect(screen.getByText("12% 预警")).toBeInTheDocument();
    expect(screen.getByText("16% 暂停新增")).toBeInTheDocument();
    expect(screen.queryByText("5% 回撤规则")).not.toBeInTheDocument();
  });

  it("keeps defaults until explicit save and rejects inverted risk thresholds", async () => {
    vi.mocked(ashareRequest).mockResolvedValue(snapshot());
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "监控设置" }));
    expect(screen.getByLabelText("最大回撤阈值（%）")).toHaveValue(5);
    fireEvent.change(screen.getByLabelText("预警阈值（%）"), { target: { value: "12" } });
    expect(screen.getByRole("button", { name: "保存监控设置" })).toBeDisabled();
    expect(ashareRequest).not.toHaveBeenCalledWith("/settings", "PUT", expect.anything());
    fireEvent.change(screen.getByLabelText("暂停新增阈值（%）"), { target: { value: "16" } });
    fireEvent.change(screen.getByLabelText("最大回撤阈值（%）"), { target: { value: "20" } });
    fireEvent.click(screen.getByRole("button", { name: "保存监控设置" }));
    await waitFor(() => expect(ashareRequest).toHaveBeenCalledWith("/settings", "PUT", expect.objectContaining({
      warning_drawdown_pct: 12, restrict_drawdown_pct: 16, max_drawdown_pct: 20, max_single_position_pct: 20,
    })));
  });
  it("saves recurring news only through an explicit settings action", async () => {
    vi.mocked(ashareRequest).mockResolvedValue(snapshot());
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "监控设置" }));
    const recurring = screen.getByRole("checkbox", { name: "每个交易日09:00—21:00持续资讯评估" });
    expect(recurring).not.toBeChecked();
    fireEvent.click(recurring);
    expect(ashareRequest).not.toHaveBeenCalledWith("/settings", "PUT", expect.anything());
    fireEvent.click(screen.getByRole("button", { name: "保存监控设置" }));
    await waitFor(() => expect(ashareRequest).toHaveBeenCalledWith("/settings", "PUT", expect.objectContaining({ recurring_news_enabled: true })));
  });
  it("uses the main-board AI scope even when there are no billboard rows", async () => {
    const data = snapshot();
    data.mainboard = { enabled: true, valid: true, note: "", scan: {}, rows: [{
      symbol: "600000.SH", name: "测试主板股", quote: null, scan_quote: {} as import("@/lib/ashare").Quote,
      market_ready: false, entry_ready: false, quality: "missing", status: "待核验", reasons: [],
    }] };
    vi.mocked(ashareRequest).mockImplementation(async route => route === "/analysis?scope=mainboard" ? { session_id: "mainboard-test" } : data);
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "沪深主板扫描" }));
    fireEvent.click(screen.getByRole("button", { name: "AI 解读公开行情" }));
    await waitFor(() => expect(ashareRequest).toHaveBeenCalledWith("/analysis?scope=mainboard", "POST"));
  });
  it("requires actual account input instead of inventing the planned capital", async () => {
    vi.mocked(ashareRequest).mockResolvedValue(snapshot());
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "录入账户，启用风险计算" }));
    expect(screen.getByLabelText("实际现金（元）")).toHaveValue(null);
    expect(screen.getByLabelText("初始账户历史高点（元）")).toHaveValue(null);
    expect(screen.getByLabelText("本次净出入金（元）")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "添加持仓" }));
    expect(screen.getByLabelText("可卖股数 1")).toBeRequired();
    expect(screen.queryByText("未触发阈值")).not.toBeInTheDocument();
  });

  it("saves a complete manual snapshot and explicit available quantity", async () => {
    vi.mocked(ashareRequest).mockResolvedValue(snapshot());
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "账户风控" }));
    fireEvent.change(screen.getByLabelText("实际现金（元）"), { target: { value: "500000" } });
    fireEvent.change(screen.getByLabelText("初始账户历史高点（元）"), { target: { value: "1000000" } });
    fireEvent.change(screen.getByLabelText("高点时间（北京时间）"), { target: { value: "2026-10-16T15:00" } });
    fireEvent.click(screen.getByRole("button", { name: "添加持仓" }));
    fireEvent.change(screen.getByLabelText("股票代码 1"), { target: { value: "600000.SH" } });
    fireEvent.change(screen.getByLabelText("持仓股数 1"), { target: { value: "50000" } });
    fireEvent.change(screen.getByLabelText("可卖股数 1"), { target: { value: "20000" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并确认账户快照" }));
    await waitFor(() => expect(ashareRequest).toHaveBeenCalledWith("/account", "PUT", expect.objectContaining({
      cash: 500000, external_flow: 0, historical_peak_equity: 1000000, peak_as_of: "2026-10-16T15:00:00+08:00",
      positions: [{ symbol: "600000.SH", quantity: 50000, available_quantity: 20000, cost_price: null }],
    })));
  });

  it("masks retained account equity and disables AI when polling loses connection", async () => {
    let poll: (() => void) | undefined;
    const originalSetInterval = window.setInterval.bind(window);
    vi.spyOn(window, "setInterval").mockImplementation((callback, delay, ...args) => {
      if (delay === 15000) {
        poll = callback as () => void;
        return -1;
      }
      return originalSetInterval(callback, delay, ...args);
    });
    const data = snapshot();
    Object.assign(data.risk, { configured: true, initialized: true, complete: true, level: "normal", equity: 1e6, drawdown_pct: 0 });
    let calls = 0;
    vi.mocked(ashareRequest).mockImplementation(async () => {
      if (calls++ === 0) return data;
      throw new Error("服务不可达");
    });
    render(<Ashare />);
    await screen.findByText("未触发阈值");
    await act(async () => { poll?.(); });
    expect(screen.getByRole("alert")).toHaveTextContent("连接中断：服务不可达");
    expect(screen.getByText("无法确认当前账户风险")).toBeInTheDocument();
    expect(screen.queryByText("100.00 万")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "AI 解读公开行情" })).toBeDisabled();
  });

  it("reuses the submission ID after a lost save response", async () => {
    const data = snapshot();
    const submitted: Record<string, unknown>[] = [];
    vi.mocked(ashareRequest).mockImplementation(async (route, _method, body) => {
      if (route === "/account") {
        submitted.push(body as Record<string, unknown>);
        if (submitted.length === 1) throw new Error("保存响应中断，请重试");
      }
      return data;
    });
    render(<Ashare />);
    fireEvent.click(await screen.findByRole("button", { name: "账户风控" }));
    fireEvent.change(screen.getByLabelText("实际现金（元）"), { target: { value: "1000000" } });
    fireEvent.change(screen.getByLabelText("初始账户历史高点（元）"), { target: { value: "1000000" } });
    fireEvent.click(screen.getByRole("button", { name: "保存并确认账户快照" }));
    await screen.findByText("保存响应中断，请重试");
    fireEvent.click(screen.getByRole("button", { name: "保存并确认账户快照" }));
    await waitFor(() => expect(submitted).toHaveLength(2));
    expect(submitted[0].submission_id).toBe(submitted[1].submission_id);
  });
});
