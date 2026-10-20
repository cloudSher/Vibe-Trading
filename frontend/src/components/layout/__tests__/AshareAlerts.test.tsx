import { act, cleanup, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AshareAlerts } from "../AshareAlerts";
import { ashareRequest } from "@/lib/ashare";
import { toast } from "sonner";

vi.mock("@/lib/ashare", () => ({ ashareRequest: vi.fn() }));
vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }));
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); vi.clearAllMocks(); });
it("prioritizes critical alerts and keeps undisplayed alerts pending", async () => {
  localStorage.clear();
  vi.stubGlobal("Notification", class { static permission = "denied"; });
  let poll: (() => void) | undefined;
  const original = window.setInterval.bind(window);
  vi.spyOn(window, "setInterval").mockImplementation((callback, delay, ...args) => {
    if (delay === 15000) { poll = callback as () => void; return -1; }
    return original(callback, delay, ...args);
  });
  const alerts = Array.from({ length: 5 }, (_, i) => ({ id: String(i), key: "focus_" + i,
    severity: i === 4 ? "critical" : "warning", message: "提醒" + i, acknowledged_at: null }));
  vi.mocked(ashareRequest).mockResolvedValue({ enabled: true, alerts });
  render(<AshareAlerts />);
  await act(async () => {});
  expect(toast.error).toHaveBeenCalledWith("A 股风控：需要处理", expect.objectContaining({ description: "提醒4" }));
  expect(JSON.parse(localStorage.getItem("ashare-alerts-seen") || "[]")).toHaveLength(3);
  await act(async () => { poll?.(); });
  expect(JSON.parse(localStorage.getItem("ashare-alerts-seen") || "[]")).toHaveLength(5);
});
it("only forwards active alerts matching the published plan identity", async () => {
  const identity = {plan_id: "mainboard-2026-10-20", version: 1, content_hash: "checksum"};
  const base = {key: "focus_ready_000933.SZ", severity: "warning", message: "本计划观察", acknowledged_at: null, plan_identity: identity};
  vi.mocked(ashareRequest).mockResolvedValue({enabled: true, plan_identity: identity, alerts: [
    {...base, id: "old", plan_identity: {...identity, version: 0}}, {...base, id: "inactive", active: false},
    {...base, id: "current", active: true}]});
  render(<AshareAlerts />); await act(async () => {});
  expect(toast).toHaveBeenCalledTimes(1);
  expect(JSON.parse(localStorage.getItem("ashare-alerts-seen") || "[]")).toEqual(["current"]);
});
it('labels current simulated events clearly and drops expired or wrong-plan paper events',async()=>{
  const identity={plan_id:'mainboard-2026-10-21',version:1,content_hash:'same'};
  const base={key:'paper_switch_600100.SH_test',severity:'info',message:'模拟观察位切换，不是券商成交',acknowledged_at:null,plan_identity:identity};
  vi.mocked(ashareRequest).mockResolvedValue({enabled:true,plan_identity:identity,alerts:[
    {...base,id:'wrong',plan_identity:{...identity,version:2}}, {...base,id:'expired',active:false}, {...base,id:'current-paper',active:true}]});
  render(<AshareAlerts/>);await act(async()=>{});
  expect(toast).toHaveBeenCalledTimes(1);
  expect(toast).toHaveBeenCalledWith('模拟组合提醒（非实际成交）',expect.objectContaining({description:base.message}));
});
