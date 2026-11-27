// AI Insights (#75; UI-03). The latest decisions with their action, strategy, reasons and
// evidence (DESIGN.md: "show every AI decision with its reason and evidence link"), and
// the latest forecasts with what their score means. An uncalibrated score is labelled so,
// and a forecast past (or with an unreadable) validity is labelled expired: a score is
// never shown as more than it is.
import { useNow } from "./app";
import { useSnapshot } from "./snapshot";
import type { Decision, Prediction } from "./types";
import { Money, ReadError } from "./ui";

const LATEST = 30;
const EXPIRY_CHECK_MS = 30_000;

function EvidenceList({ refs }: { refs: string[] }) {
  if (refs.length === 0) return <span className="muted">none</span>;
  return (
    <ul className="refs">
      {[...new Set(refs)].map((r) => (
        <li key={r}>
          <code>{r}</code>
        </li>
      ))}
    </ul>
  );
}

function DecisionItem({ d }: { d: Decision }) {
  return (
    <li className="card">
      <p>
        <strong>{d.action}</strong>
        {d.side && ` ${d.side}`} · <span>{d.strategy_id ?? "no strategy"}</span>
      </p>
      <dl>
        <dt>Expected value (estimate, USD)</dt>
        <dd>
          <Money value={d.expected_value_usd} empty="unknown" />
        </dd>
        <dt>Reasons</dt>
        <dd>
          {[...new Set(d.reason_codes)].map((r) => (
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

function PredictionItem({ p }: { p: Prediction }) {
  const now = useNow(EXPIRY_CHECK_MS);
  const until = new Date(p.valid_until).getTime();
  const expired = !Number.isFinite(until) || until <= now.getTime();
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
  const decisions = useSnapshot((api) => api.decisions(LATEST));
  const predictions = useSnapshot((api) => api.predictions(LATEST));
  return (
    <>
      <h2>AI Insights</h2>
      <section aria-labelledby="dec">
        <h3 id="dec">Decisions</h3>
        <p className="muted">The latest {LATEST}, newest first.</p>
        {decisions.error !== null && <ReadError what="decisions" />}
        {decisions.data?.items.length === 0 && <p className="muted">No decisions yet.</p>}
        <ul className="list">
          {decisions.data?.items.map((d) => (
            <DecisionItem key={d.decision_id} d={d} />
          ))}
        </ul>
      </section>
      <section aria-labelledby="fc">
        <h3 id="fc">Forecasts</h3>
        <p className="muted">The latest {LATEST}, newest first.</p>
        {predictions.error !== null && <ReadError what="forecasts" />}
        {predictions.data?.items.length === 0 && <p className="muted">No forecasts yet.</p>}
        <ul className="list">
          {predictions.data?.items.map((p) => (
            <PredictionItem key={p.prediction_id} p={p} />
          ))}
        </ul>
      </section>
    </>
  );
}
