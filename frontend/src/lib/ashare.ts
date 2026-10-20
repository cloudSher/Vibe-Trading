import { authHeaders } from "@/lib/apiAuth";

export interface Quote {
  symbol: string; name: string; kind: string; price: number; previous_close: number;
  open: number | null; high: number | null; low: number | null; change_pct: number;
  amount_yuan: number | null; turnover_pct: number | null; vwap: number | null;
  as_of: string; received_at: string; source: string; quality?: string;
}
export interface Observation {
  symbol: string; name: string; billboard_date: string; billboard_amount: number;
  net_buy_amount: number | null; reasons: string[]; quote: Quote | null;
  quality: string; flags: string[]; score: number | null; candidate: boolean;
  five_minute_change_pct: number | null; label: string;
}
export interface Holding {
  symbol: string; quantity: number; available_quantity: number; cost_price: number | null;
}
export interface Account {
  submission_id: string; cash: number; positions: Holding[]; historical_peak_equity: number;
  peak_as_of: string; confirmed_at: string; external_flow: number;
}
export interface WatchSettings {
  enabled: boolean; poll_seconds: number; pool_size: number; watchlist: string[];
  recurring_news_enabled?: boolean;
  market_news_enabled?: boolean;
  max_single_position_pct: number;
  warning_drawdown_pct?: number; restrict_drawdown_pct?: number; max_drawdown_pct?: number;
}
export interface WatchAlert {
  plan_identity?: {plan_id: string; version: number; content_hash: string};
  id: string; severity: string; key: string; message: string; created_at: string;
  acknowledged_at: string | null;
  active?: boolean;
}
export interface RiskPeriod {
  id: string; started_at: string; account_confirmed_at: string; start_equity: number;
  valuation_basis: string;
  history_at_start: { drawdown_pct: number; peak_equity_equivalent: number; breached: boolean };
}
export interface AshareSnapshot {
  market_news?: MarketNewsView;
  mainboard?: MainboardView;
  risk_policy_audit?: { changed_at: string; before: {warning_pct: number; restrict_pct: number; max_drawdown_pct: number}; after: {warning_pct: number; restrict_pct: number; max_drawdown_pct: number} }[];
  focus?: FocusView;
  news?: NewsView;
  settings: WatchSettings;
  market: { now: string; phase: string; label: string; live_window: boolean; continuous: boolean;
    calendar_verified: boolean; latest_completed_session: string | null };
  monitor_running: boolean; refreshed_at: string | null; errors: Record<string, string>;
  billboard: { date?: string; fetched_at?: string; selected: number; total_symbols?: number; pool_valid: boolean; source_url?: string };
  rows: Observation[]; quotes: Quote[]; account: Account | null;
  risk: { level: string; configured: boolean; initialized: boolean; complete: boolean; equity: number | null; cash?: number;
    drawdown_pct: number | null; max_drawdown_pct: number; latched: boolean;
    warning_pct?: number; restrict_pct?: number;
    drawdown_basis?: "historical_high" | "monitoring_period"; monitoring_period?: RiskPeriod | null;
    historical_drawdown_pct?: number | null; historical_peak_equity_equivalent?: number;
    historical_gap_to_peak?: number; historical_latched?: boolean; period_return_pct?: number | null;
    peak_equity_equivalent?: number; budget_to_5pct?: number; budget_to_limit?: number; selection_allowed: boolean;
    guidance: string; basis: string; holdings: (Holding & { name: string; market_value: number | null;
      locked_quantity: number; weight_pct?: number; cost_pnl_pct: number | null; quality: string })[] };
  alerts: WatchAlert[]; unread_alerts: number; notice: string;
}

export interface MarketNewsView {
  enabled: boolean; active: boolean; scope: string; directory_valid: boolean; directory_as_of?: string;
  universe_total: number; directory_error?: string | null; source_fresh: boolean; checked_at?: string;
  analysis_at?: string; analysis_error?: string | null; candidate_limit: number; note: string;
  analysis_running?: boolean; pending_count?: number;
  rows: { symbol: string; name: string; quality: string; quote: Quote | null; market_ready: boolean;
    entry_ready: false; status: string; reasons: string[]; news: {events: NewsEvent[]};
    observation_conditions: string[]; invalidation_conditions: string[];
    review: { checked_at?: string; announcement_since?: string; error?: string | null; title_flags?: string[] };
    announcements: {id: string; date: string; title: string; source_url: string}[] }[];
}

export interface MainboardView {
  enabled: boolean; session_date?: string; running?: boolean; requested?: boolean;
  attempted_at?: string; error?: string | null; valid: boolean; note: string;
  source_warning?: string | null;
  progress?: { received?: number; total?: number; pages?: number };
  scan: { completed_at?: string; started_at?: string; coverage_complete?: boolean;
    universe_total?: number; received_total?: number; pages?: number; fresh_count?: number;
    qualifying_total?: number; excluded_counts?: Record<string, number>; source_url?: string; method?: string;
    universe_as_of?: string; quote_received_total?: number; missing_quote_total?: number };
  rows: { symbol: string; name: string; quote: Quote | null; scan_quote: Quote;
    market_ready: boolean; entry_ready: false; quality: string; status: string; reasons: string[];
    review?: { checked_at: string; announcements: { date: string; title: string; source_url: string }[];
      news: { title: string; published_at: string; url: string; source: string }[];
      title_flags: string[]; error?: string | null } }[];
}

export interface FocusView {
  plan_id?: string; version?: number; content_hash?: string; published_at?: string; research_cutoff?: string;
  lifecycle?: string;
  risk_policy?: { level: string; selection_allowed: boolean; drawdown_basis: string; warning_pct: number; restrict_pct: number; max_drawdown_pct: number };
  configured: boolean; enabled: boolean; session_date: string; reference_date: string;
  reviewed_at: string; disclosures_checked_at: string | null; disclosures_fresh: boolean;
  disclosure_error?: string | null; max_open_gap_pct: number; rule_note: string;
  rows: { symbol: string; name: string; reference_close: number; reference_vwap: number;
    reason?: string; risk_points?: string[]; catalyst_published_at?: string;
    research?: {rank: number; score: number; score_parts: Record<string,number>; decision: string};
    risk_status?: string; risk_blockers?: string[];
    price_evidence?: {stage: string; bar_as_of?: string; breakout_at?: string; retest_at?: string; volume_ratio?: number; source_warning?: string; bar_source?: string};
    disclosure_status: string; disclosure_summary: string; source_urls: string[];
    quote: Quote | null; quality: string; market_ready: boolean; entry_ready: boolean;
    status: string; confirmations: number; new_disclosure_ids: string[]; reasons: string[];
    news?: StockNewsView | null; observation_conditions?: string[]; invalidation_conditions?: string[];
    announcements: { id: string; date: string; title: string; source_url: string }[] }[];
}

export interface ObservationPlanSnapshot {
  focus: FocusView;
  risk_policy: { warning_pct: number; restrict_pct: number; max_drawdown_pct: number; drawdown_basis: string };
  risk_level: string;
  archives: { session_date: string; archived_at: string; stocks: {symbol: string; name: string; disclosure_status: string; disclosure_summary: string}[] }[];
}

export interface NewsEvent {
  id: string; title: string; source: string; url: string; published_at: string; first_seen_at: string;
  relation: { scope: string; matched: string[] }; snippet: string; risk_flag: boolean;
  fresh_catalyst: boolean; reference_only: boolean; market_report: boolean;
  analysis: { impact: string; summary: string; evidence: string } | null;
}
export interface StockNewsView {
  healthy: boolean; blocked: string[]; events: NewsEvent[]; has_fresh_catalyst: boolean; note: string;
}
export interface NewsView {
  monitoring?: { recurring: boolean; enabled: boolean; active: boolean; session_date: string;
    trading_plan_current: boolean; trading_plan_date: string | null; calendar_verified: boolean; start: string; end: string };
  enabled: boolean; sources: Record<string, { fresh: boolean; checked_at?: string; latest_published_at?: string;
    count?: number; error?: string | null }>; analysis_at?: string | null; analysis_error?: string | null;
  analysis_running?: boolean; pending_count?: number; event_count?: number; note?: string;
}

export async function ashareRequest<T>(route = "", method = "GET", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch("/api/ashare" + route, {
    method, signal, headers: { ...authHeaders(), ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map((item: { msg?: string }) => item.msg || "字段无效").join("；") : `请求失败 (${response.status})`);
  }
  return response.json();
}

export const qualityLabel: Record<string, string> = {
  fresh: "时效通过", reference: "非盘中参考", stale: "报价过期", missing: "缺少报价",
  future_timestamp: "时间戳异常", calendar_unknown: "日历待核验", invalid_price: "价格无效",
  timestamp_missing: "时间缺失",
};

export function shanghaiTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "—";
}

export function localDateInput(value?: string): string {
  return new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .format(value ? new Date(value) : new Date()).replace(" ", "T");
}
