// Trading view (#74; UI-02). Account health and the Account DD sit apart from the bot's
// Cycle Net, which is always labelled an estimate. Every panel says how fresh it is, and
// a failed read says so instead of looking like "no data". Controls are enabled only for
// a known bot state that allows them; each needs confirmation, and Emergency Stop lives
// in its own zone and needs STOP typed. One command runs at a time.
import { useEffect, useRef, useState } from "react";
import { useApp, useNow } from "./app";
import { type CommandState, runCommand } from "./commands";
import { formatMoney, formatPercent, signed } from "./format";
import { freshness } from "./freshness";
import { useSnapshot } from "./snapshot";
import type { ControlAction, ControlState, Cycle, Status } from "./types";

/** Keep equal to the backend's ACCOUNT_STALE_AFTER (backend/app/api/app.py). */
const ACCOUNT_MAX_AGE_MS = 60_000;
/** M06 reports the Account DD on change; an older reading is shown with its age. */
const RISK_MAX_AGE_MS = 5 * 60_000;
const CYCLE_MAX_AGE_MS = 60_000;

function Fresh({
  asOf,
  maxAgeMs,
  stale,
}: {
  asOf: string | null;
  maxAgeMs: number;
  stale?: boolean;
}) {
  const now = useNow();
  const old = stale === true || freshness(asOf, now, maxAgeMs) === "STALE";
  return (
    <p className="muted">
      {old ? <span className="pill warn">Stale</span> : <span className="pill ok">Fresh</span>}{" "}
      {asOf ? `observed ${new Date(asOf).toLocaleTimeString()}` : "never observed"}
    </p>
  );
}

function ReadError() {
  return <p role="alert">Could not read this from the bot. Retrying…</p>;
}

function Money({ value }: { value: string | null }) {
  const s = value === null ? null : signed(value);
  if (s === null) return <span>—</span>;
  return (
    <span className={`money ${s.word}`}>
      <span>{s.text}</span> <span className="muted">{s.word}</span>
    </span>
  );
}

function AccountCard({ status }: { status: Status | null }) {
  const account = useSnapshot((api) => api.account());
  const snap = account.data?.snapshot ?? null;
  const risk = status?.risk ?? null;
  return (
    <section className="card" aria-labelledby="acct">
      <h3 id="acct">Account</h3>
      {account.loading && <p className="muted">Loading…</p>}
      {account.error !== null && <ReadError />}
      {!account.loading && account.error === null && !snap && (
        <p className="muted">No account reading yet.</p>
      )}
      {snap && (
        <>
          <dl>
            <dt>Balance</dt>
            <dd>{formatMoney(snap.balance)}</dd>
            <dt>Equity</dt>
            <dd>{formatMoney(snap.equity)}</dd>
            <dt>Margin</dt>
            <dd>{formatMoney(snap.margin)}</dd>
            <dt>Free margin</dt>
            <dd>{formatMoney(snap.free_margin)}</dd>
            <dt>Broker</dt>
            <dd>{snap.connected ? "Connected" : "Disconnected"}</dd>
          </dl>
          <Fresh
            asOf={snap.observed_at}
            maxAgeMs={ACCOUNT_MAX_AGE_MS}
            stale={account.data?.stale ?? true}
          />
        </>
      )}
      <dl>
        <dt>Account DD</dt>
        <dd>
          {risk?.epoch_dd_percent == null
            ? "Unknown"
            : `${formatPercent(risk.epoch_dd_percent)}% of ${formatPercent(risk.epoch_ceiling_percent)}% limit`}
        </dd>
      </dl>
      {risk && <Fresh asOf={risk.observed_at} maxAgeMs={RISK_MAX_AGE_MS} />}
    </section>
  );
}

function CycleCard() {
  const cycles = useSnapshot((api) => api.cycles(1));
  const cycle: Cycle | undefined = cycles.data?.items[0];
  return (
    <section className="card" aria-labelledby="cycle">
      <h3 id="cycle">Current cycle</h3>
      {cycles.loading && <p className="muted">Loading…</p>}
      {cycles.error !== null && <ReadError />}
      {!cycles.loading && cycles.error === null && !cycle && <p className="muted">No cycle yet.</p>}
      {cycle && (
        <>
          <dl>
            <dt>State</dt>
            <dd>{cycle.state}</dd>
            <dt>Cycle Net (estimate)</dt>
            <dd>
              <Money value={cycle.cycle_net_estimated_usd} />
            </dd>
            <dt>Profit Target</dt>
            <dd>{formatMoney(cycle.target_usd)}</dd>
            <dt>Trailing</dt>
            <dd>
              {cycle.trailing_active
                ? `active, floor ${formatMoney(cycle.trailing_floor_usd)}`
                : "not active"}
            </dd>
            {cycle.unresolved.length > 0 && (
              <>
                <dt>Unresolved</dt>
                <dd>{cycle.unresolved.join(", ")}</dd>
              </>
            )}
          </dl>
          <Fresh asOf={cycles.data?.as_of ?? null} maxAgeMs={CYCLE_MAX_AGE_MS} />
        </>
      )}
    </section>
  );
}

function PositionsCard() {
  // `/api/positions` reports `available: false` until M06's position read model is
  // exposed: nothing to fetch yet, so nothing is fetched.
  return (
    <section className="card" aria-labelledby="pos">
      <h3 id="pos">Bot positions</h3>
      <p className="muted">Position list is not available yet.</p>
    </section>
  );
}

const PHASE_TEXT: Record<CommandState["phase"], string> = {
  SENDING: "Sending…",
  PENDING: "Pending: waiting for the bot to confirm",
  COMPLETED: "Completed",
  REJECTED: "Rejected",
  UNRESOLVED: "Unresolved: not everything could be closed",
  STILL_PENDING: "Still pending",
  REFUSED: "Not sent",
};

function CommandOutcome({ state }: { state: CommandState | null }) {
  if (!state) return null;
  return (
    <p role="status">
      {PHASE_TEXT[state.phase]}
      {state.result?.unresolved.length ? `: ${state.result.unresolved.join(", ")}` : ""}
      {state.result?.reason ? ` (${state.result.reason})` : ""}
      {state.message ? ` — ${state.message}` : ""}
    </p>
  );
}

/** One command at a time; polling stops when the view goes away. */
function useCommand() {
  const { api, refresh } = useApp();
  const [state, setState] = useState<CommandState | null>(null);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => () => abort.current?.abort(), []);
  const busy = state?.phase === "SENDING" || state?.phase === "PENDING";
  const send = (action: ControlAction) => {
    abort.current = new AbortController();
    void runCommand(api, action, { onUpdate: setState, signal: abort.current.signal }).finally(
      refresh,
    );
  };
  return { state, busy, send };
}

function ConfirmButton({
  label,
  disabled,
  onConfirm,
  confirmWord,
  danger = false,
}: {
  label: string;
  disabled: boolean;
  onConfirm: () => void;
  /** When set, the word must be typed before Confirm is enabled. */
  confirmWord?: string;
  danger?: boolean;
}) {
  const [asking, setAsking] = useState(false);
  const [typed, setTyped] = useState("");
  const close = () => {
    setAsking(false);
    setTyped("");
  };
  const cls = danger ? "danger" : undefined;
  if (!asking) {
    return (
      <button type="button" className={cls} disabled={disabled} onClick={() => setAsking(true)}>
        {label}
      </button>
    );
  }
  return (
    <span className="confirm">
      {confirmWord && (
        <label>
          Type {confirmWord} to confirm
          <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
        </label>
      )}
      <button
        type="button"
        className={cls}
        disabled={disabled || (confirmWord !== undefined && typed !== confirmWord)}
        onClick={() => {
          close();
          onConfirm();
        }}
      >
        Confirm {label}
      </button>
      <button type="button" className="quiet" onClick={close}>
        Cancel
      </button>
    </span>
  );
}

const CAN_START: ControlState[] = ["STOPPED", "PAUSED"];
const CAN_FINISH: ControlState[] = ["RUNNING", "PAUSED"];

function Controls({
  status,
  busy,
  send,
}: { status: Status | null } & ReturnType<typeof useCommand>) {
  const missing = status?.missing_for_start ?? [];
  const state = status?.control_state;
  const known = state !== undefined;
  return (
    <div className="row">
      <ConfirmButton
        label="Start"
        disabled={busy || !known || !CAN_START.includes(state) || missing.length > 0}
        onConfirm={() => send("start")}
      />
      <ConfirmButton
        label="Pause"
        disabled={busy || state !== "RUNNING"}
        onConfirm={() => send("pause")}
      />
      <ConfirmButton
        label="Finish Cycle"
        disabled={busy || !known || !CAN_FINISH.includes(state)}
        onConfirm={() => send("finish-cycle")}
      />
      {status?.risk_locked && (
        <ConfirmButton
          label="Approve Recovery"
          disabled={busy}
          onConfirm={() => send("approve-recovery")}
        />
      )}
    </div>
  );
}

export function TradingView() {
  const { status } = useApp();
  const command = useCommand();
  const missing = status?.missing_for_start ?? [];
  return (
    <>
      <h2>Trading</h2>
      <div className="grid">
        <AccountCard status={status} />
        <CycleCard />
        <PositionsCard />
        <section className="card" aria-labelledby="ctl">
          <h3 id="ctl">Controls</h3>
          {status === null && <p className="muted">Bot state unknown: controls are off.</p>}
          <Controls status={status} {...command} />
          {missing.length > 0 && <p className="muted">Start needs: {missing.join(", ")}</p>}
          <CommandOutcome state={command.state} />
        </section>
      </div>
      <section className="card emergency" aria-labelledby="em">
        <h3 id="em">Emergency</h3>
        <p className="muted">
          Closes the bot's positions now. It is done only when the bot confirms every close.
        </p>
        <ConfirmButton
          label="Emergency Stop"
          danger
          confirmWord="STOP"
          disabled={command.busy}
          onConfirm={() => command.send("emergency-stop")}
        />
      </section>
    </>
  );
}
