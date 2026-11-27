// Response shapes of the M08 API (backend/app/api/app.py) and the contracts it serves
// (backend/app/core/contracts.py). Money and scores are decimal strings: never parsed to
// float for display or arithmetic. Times are UTC ISO strings.

export type ControlState =
  | "STOPPED"
  | "STARTING"
  | "RECONCILING"
  | "RUNNING"
  | "PAUSED"
  | "FINISHING"
  | "CLOSING"
  | "RISK_LOCKED"
  | "SAFE_MODE";

export type CommandStatus = "ACCEPTED" | "REJECTED" | "COMPLETED" | "UNRESOLVED";
export type ControlAction =
  | "start"
  | "pause"
  | "finish-cycle"
  | "emergency-stop"
  | "approve-recovery";
export type HealthStatus = "OK" | "DEGRADED" | "DOWN";

export interface HealthBeat {
  component: string;
  status: HealthStatus;
  last_heartbeat: string;
  detail: string | null;
}

export interface Status {
  as_of: string;
  control_state: ControlState;
  control_state_seq: number | null;
  risk_locked: boolean;
  risk_lock_seq: number | null;
  requires_reconcile: boolean;
  config_version: number;
  account_mode: "SHADOW" | "DEMO";
  missing_for_start: string[];
  live_allowed: boolean;
  health: Record<string, HealthBeat>;
  latest_seq: number;
  /** M06's latest Account DD reading; null until one exists (unknown, never 0). */
  risk: Risk | null;
}

export interface Risk {
  epoch_dd_percent: string | null;
  lifetime_dd_percent: string | null;
  epoch_ceiling_percent: string;
  lifetime_ceiling_percent: string;
  reason_codes: string[];
  policy_version: string;
  observed_at: string;
}

export interface AccountSnapshot {
  currency: string;
  balance: string | null;
  equity: string | null;
  margin: string | null;
  free_margin: string | null;
  connected: boolean;
  quote_status: "OK" | "STALE" | "DEGRADED" | "MISSING";
  observed_at: string;
}

export interface Account {
  as_of: string;
  snapshot: AccountSnapshot | null;
  stale: boolean;
}

export interface BotPosition {
  position_id: string;
  symbol: string;
  side: "BUY" | "SELL";
  volume: string;
  open_price: string;
  profit: string;
  commission: string;
  swap: string;
  cycle_id: string | null;
}

/** The bot's own positions from M06's latest snapshot (never Manual Positions). */
export interface Positions {
  as_of: string;
  observed_at: string | null;
  stale: boolean;
  items: BotPosition[];
}

export interface Cycle {
  cycle_id: string | null;
  seq: number;
  state: "IDLE" | "ACTIVE" | "CLOSING" | "CLOSED";
  target_usd: string;
  cycle_net_estimated_usd: string | null;
  peak_net_usd: string | null;
  trailing_floor_usd: string | null;
  trailing_active: boolean;
  final_net_usd: string | null;
  close_reason: string | null;
  target_triggered: boolean;
  target_achieved: boolean | null;
  unresolved: string[];
}

export interface Confidence {
  value: string;
  semantics: string;
  calibrated: boolean;
}

export interface Prediction {
  seq: number;
  prediction_id: string;
  model_id: string;
  model_version: string;
  lineage: string;
  horizon: string;
  horizon_bars: number;
  direction: "UP" | "DOWN" | "NEUTRAL";
  probability: Confidence;
  data_quality: string;
  generated_at: string;
  valid_until: string;
}

export interface Decision {
  seq: number;
  decision_id: string;
  action: "ENTER" | "ADD" | "EXIT" | "WAIT";
  strategy_id: string | null;
  side: "BUY" | "SELL" | null;
  expected_value_usd: string | null;
  confidence: Confidence | null;
  reason_codes: string[];
  evidence_refs: string[];
  policy_version: string;
}

export interface Items<T> {
  as_of: string;
  items: T[];
}

export interface CommandResult {
  command_id: string;
  status: CommandStatus;
  resulting_state?: ControlState;
  risk_locked?: boolean;
  unresolved: string[];
  reason?: string | null;
}

export interface TradingConfig {
  symbol: string;
  initial_lot: string | null;
  profit_target_usd: string | null;
  max_drawdown_percent: string | null;
  order_scope: "BOT_ONLY";
  drawdown_scope: "ACCOUNT_EQUITY";
  account_mode: "SHADOW" | "DEMO";
  risk_profile: "LOW" | "NORMAL" | "HIGH";
  strategies_enabled: string[];
}

export interface LlmConfig {
  provider: string | null;
  model: string | null;
  api_key_ref: string | null;
  news_handling: "adaptive" | "off";
}

export interface UserConfig {
  trading: TradingConfig;
  llm: LlmConfig;
  system: {
    deployment: "local" | "vps" | "hybrid";
    storage: "local" | "cloud" | "hybrid";
    bind_host: string;
    dashboard_origin: string;
  };
}

export interface Settings {
  version: number;
  config: UserConfig;
  protected: { policy_version: string; live_allowed: boolean };
  missing_for_start: string[];
  /** Strategy ids the backend knows; enabling any other is refused (422). */
  available_strategies: string[];
}

/** One `/ws/live` message: the journal seq and the event envelope. */
export interface LiveMessage {
  seq: number;
  event: { event_type?: string; payload?: unknown } & Record<string, unknown>;
}

export const MASK = "***";
