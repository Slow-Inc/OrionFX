// AI Insights (#75; UI-03). Every decision with its action, strategy, reasons and
// evidence (DESIGN.md: "show every AI decision with its reason and evidence link"), and
// every forecast with what its score means. An uncalibrated score is labelled so, and an
// expired forecast is labelled expired: a score is never shown as more than it is.
import { useNow } from "./app";
import { signed } from "./format";
import { useSnapshot } from "./snapshot";
import type { Decision, Prediction } from "./types";

function EvidenceList({ refs }: { refs: string[] }) {
  if (refs.length === 0) return <span className="muted">none</span>;
  return (
    <ul className="refs">
      {refs.map((r) => (
        <li key={r}>
          <code>{r}</code>
        </li>
      ))}
    </ul>
  );
}

function DecisionItem({ d }: { d: Decision }) {
  const ev = d.expected_value_usd === null ? null : signed(d.expected_value_usd);
  return (
    <li className="card">
      <p>
        <strong>{d.action}</strong>
        {d.side && ` ${d.side}`} · <span>{d.strategy_id ?? "no strategy"}</span>
      </p>
      <dl>
        <dt>Expected value</dt>
        <dd>{ev ? `${ev.text} USD (${ev.word}, estimate)` : "unknown"}</dd>
        <dt>Reasons</dt>
        <dd>
          {d.reason_codes.map((r) => (
            <span key={r} className="pill">
              {r}
            </span>
          ))}
        </dd>
        <dt>Evidence</dt>
        <dd>
          <EvidenceList refs={d.evidence_refs} />
        </dd>
      </dl>
    </li>
  );
}

function PredictionItem({ p, now }: { p: Prediction; now: Date }) {
  const expired = new Date(p.valid_until).getTime() <= now.getTime();
  return (
    <li className="card">
      <p>
        <strong>{p.direction}</strong> over {p.horizon_bars} × {p.horizon} ·{" "}
        <code>
          {p.model_id}@{p.model_version}
        </code>{" "}
        {expired && <span className="pill warn">Expired</span>}
      </p>
      <dl>
        <dt>Score</dt>
        <dd>
          {p.probability.value}{" "}
          <span className={`pill ${p.probability.calibrated ? "ok" : "warn"}`}>
            {p.probability.calibrated ? "Calibrated" : "Uncalibrated"}
          </span>
        </dd>
        <dt>Means</dt>
        <dd>{p.probability.semantics}</dd>
        <dt>Data</dt>
        <dd>
          {p.data_quality} · {p.lineage}
        </dd>
      </dl>
    </li>
  );
}

export function InsightsView() {
  const decisions = useSnapshot((api) => api.decisions(30));
  const predictions = useSnapshot((api) => api.predictions(30));
  const now = useNow();
  return (
    <>
      <h2>AI Insights</h2>
      <section aria-labelledby="dec">
        <h3 id="dec">Decisions</h3>
        {decisions.error !== null && <p role="alert">Could not read decisions. Retrying…</p>}
        {decisions.data?.items.length === 0 && <p className="muted">No decisions yet.</p>}
        <ul className="list">
          {decisions.data?.items.map((d) => (
            <DecisionItem key={d.decision_id} d={d} />
          ))}
        </ul>
      </section>
      <section aria-labelledby="fc">
        <h3 id="fc">Forecasts</h3>
        {predictions.error !== null && <p role="alert">Could not read forecasts. Retrying…</p>}
        {predictions.data?.items.length === 0 && <p className="muted">No forecasts yet.</p>}
        <ul className="list">
          {predictions.data?.items.map((p) => (
            <PredictionItem key={p.prediction_id} p={p} now={now} />
          ))}
        </ul>
      </section>
    </>
  );
}
