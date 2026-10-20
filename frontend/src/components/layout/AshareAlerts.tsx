import { useEffect } from "react";
import { toast } from "sonner";
import { ashareRequest, type WatchAlert } from "@/lib/ashare";
import { safeGet, safeSet } from "@/lib/storage";

export function AshareAlerts() {
  useEffect(() => {
    let disposed = false;
    let pending = false;
    const controller = new AbortController();
    let seen: string[] = [];
    try {
      const saved = JSON.parse(safeGet("ashare-alerts-seen") || "[]");
      seen = Array.isArray(saved) ? saved.filter(item => typeof item === "string") : [];
    } catch { /* Ignore invalid local preference. */ }
    const poll = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await ashareRequest<{ alerts: WatchAlert[]; enabled: boolean; plan_identity?: WatchAlert["plan_identity"] }>("/alerts", "GET", undefined, controller.signal);
        if (disposed || !result.enabled) return;
        const fresh = result.alerts.filter(a => !a.acknowledged_at && !seen.includes(a.id) && (
          !result.plan_identity || !(a.key.startsWith("focus_") || a.key.startsWith('paper_')) || (a.active !== false &&
            a.plan_identity?.plan_id === result.plan_identity.plan_id && a.plan_identity?.version === result.plan_identity.version &&
            a.plan_identity?.content_hash === result.plan_identity.content_hash)))
          .sort((a, b) => Number(b.severity === "critical") - Number(a.severity === "critical"));
        const delivered = fresh.slice(0, 3);
        delivered.forEach(alert => {
          const expired = alert.key.startsWith("focus_ready_") && alert.active === false;
          const title = alert.key.startsWith('paper_') ? '模拟组合提醒（非实际成交）' : expired ? "观察条件已失效" : alert.severity === "critical" ? "A 股风控：需要处理" : "A 股观察提醒";
          const message = expired ? "历史事件，当前不构成买入观察确认。" + alert.message : alert.message;
          if (alert.severity === "critical") toast.error(title, { description: message, duration: 12000 });
          else toast(title, { description: message });
          if ("Notification" in window && Notification.permission === "granted") {
            new Notification(title, { body: message, tag: alert.key });
          }
        });
        seen = [...seen, ...delivered.map(a => a.id)].slice(-600);
        safeSet("ashare-alerts-seen", JSON.stringify(seen));
      } catch { /* The workbench shows connection errors; never claim healthy. */ }
      finally { pending = false; }
    };
    void poll();
    const interval = window.setInterval(() => void poll(), 15000);
    return () => { disposed = true; controller.abort(); window.clearInterval(interval); };
  }, []);
  return null;
}
