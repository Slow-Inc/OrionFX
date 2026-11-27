// Trading view (#74; UI-02). Account health and the Account DD sit apart from the bot's
// Cycle Net, which is always labelled an estimate. Every panel says how fresh it is.
// Controls need confirmation; Emergency Stop lives in its own zone and needs STOP typed.
import { useState } from "react";
import { useApp, useNow } from "./app";
import { type CommandState, runCommand } from "./commands";
import { formatMoney, signed } from "./format";
import { freshness } from "./freshness";
import { useSnapshot } from "./snapshot";
import type { ControlAction, Cycle, Status } from "./types";

const ACCOUNT_MAX_AGE_MS = 60_000;

function Fresh({ asOf, stale }: { asOf: string | null; stale?: boolean }) {
  const now = useNow();
  const old = stale === true || freshness(asOf, now, ACCOUNT_MAX_AGE_MS) === "STALE";
  return (
    <p className="muted">
      {old ? <span className="pill warn">Stale</span> : <span className="pill ok">Fresh</span>}{" "}
      {asOf ? `observed ${new Date(asOf).toLocaleTimeString()}` : "never observed"}
    </p>
  );
}

function Money({ value }: { value: string | null }) {
  if (value === null) return <span>—</span>;
  const s = signed(value);
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
      {!account.loading && !snap && <p className="muted">No account reading yet.</p>}
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
          <Fresh asOf={snap.observed_at} stale={account.data?.stale ?? true} />
        </>
      )}
      <dl>
        <dt>Account DD</dt>
        <dd>
          {risk?.epoch_dd_percent == null
            ? "Unknown"
            : `${risk.epoch_dd_percent}% of ${risk.epoch_ceiling_percent}% limit`}
        </dd>
      </dl>
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
      {!cycles.loading && !cycle && <p className="muted">No cycle yet.</p>}
      {cycle && (
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
      )}
    </section>
  );
}

function PositionsCard() {
  const positions = useSnapshot((api) => api.positions());
  return (
    <section className="card" aria-labelledby="pos">
      <h3 id="pos">Bot positions</h3>
      {positions.data && !positions.data.available && (
        <p className="muted">Position list is not available yet.</p>
      )}
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

function useCommand() {
  const { api, refresh } = useApp();
  const [state, setState] = useState<CommandState | null>(null);
  const busy = state?.phase === "SENDING" || state?.phase === "PENDING";
  const send = (action: ControlAction) =>
    runCommand(api, action, { onUpdate: setState }).finally(refresh);
  return { state, busy, send };
}

function ConfirmButton({
  label,
  action,
  disabled,
  busy,
  onSend,
}: {
  label: string;
  action: ControlAction;
  disabled: boolean;
  busy: boolean;
  onSend: (a: ControlAction) => void;
}) {
  const [asking, setAsking] = useState(false);
  if (!asking) {
    return (
      <button type="button" disabled={disabled || busy} onClick={() => setAsking(true)}>
        {label}
      </button>
    );
  }
  return (
    <span className="confirm">
      <button
        type="button"
        onClick={() => {
          setAsking(false);
          onSend(action);
        }}
      >
        Confirm {label}
      </button>
      <button type="button" className="quiet" onClick={() => setAsking(false)}>
        Cancel
      </button>
    </span>
  );
}

function Controls({ status }: { status: Status | null }) {
  const regular = useCommand();
  const missing = status?.missing_for_start ?? [];
  const state = status?.control_state;
  const common = { busy: regular.busy, onSend: (a: ControlAction) => void regular.send(a) };
  return (
    <section className="card" aria-labelledby="ctl">
      <h3 id="ctl">Controls</h3>
      <div className="row">
        <ConfirmButton
          label="Start"
          action="start"
          disabled={missing.length > 0 || state === "RUNNING"}
          {...common}
        />
        <ConfirmButton label="Pause" action="pause" disabled={state !== "RUNNING"} {...common} />
        <ConfirmButton
          label="Finish Cycle"
          action="finish-cycle"
          disabled={state !== "RUNNING" && state !== "PAUSED"}
          {...common}
        />
        {status?.risk_locked && (
          <ConfirmButton
            label="Approve recovery"
            action="approve-recovery"
            disabled={false}
            {...common}
          />
        )}
      </div>
      {missing.length > 0 && <p className="muted">Start needs: {missing.join(", ")}</p>}
      <CommandOutcome state={regular.state} />
    </section>
  );
}

function EmergencyZone() {
  const stop = useCommand();
  const [asking, setAsking] = useState(false);
  const [typed, setTyped] = useState("");
  return (
    <section className="card emergency" aria-labelledby="em">
      <h3 id="em">Emergency</h3>
      <p className="muted">
        Closes the bot's positions now. It is done only when the bot confirms every close.
      </p>
      {!asking ? (
        <button
          type="button"
          className="danger"
          disabled={stop.busy}
          onClick={() => setAsking(true)}
        >
          Emergency Stop
        </button>
      ) : (
        <div className="row">
          <label>
            Type STOP to confirm
            <input value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" />
          </label>
          <button
            type="button"
            className="danger"
            disabled={typed !== "STOP"}
            onClick={() => {
              setAsking(false);
              setTyped("");
              void stop.send("emergency-stop");
            }}
          >
            Confirm Emergency Stop
          </button>
          <button type="button" className="quiet" onClick={() => setAsking(false)}>
            Cancel
          </button>
        </div>
      )}
      <CommandOutcome state={stop.state} />
    </section>
  );
}

export function TradingView() {
  const { status } = useApp();
  return (
    <>
      <h2>Trading</h2>
      <div className="grid">
        <AccountCard status={status} />
        <CycleCard />
        <PositionsCard />
        <Controls status={status} />
      </div>
      <EmergencyZone />
    </>
  );
}
