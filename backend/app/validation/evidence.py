"""Evidence store, protected readiness policy and gate, live qualifications (#68; VAL-04,
VAL-05, VAL-06; AT-16, AT-24; master plan §11.1).

- A validation run (replay, walk-forward, scenario) is stored append-only; its ref
  `x02:run:<id>` is the hash of its content, so it is the same for the same evidence.
- A `ReadinessPolicy` lists the metric criteria, minimum trades and days, and the
  regimes that must be covered. Anything not yet decided stays None (TBD).
  - Versions are immutable: changing a policy means a new version, and only the newest
    version is current.
  - The shipped `DEFAULT_POLICY` is uncertified with every threshold TBD, so nothing
    qualifies until the owner and the modules set real thresholds (OD-07, OD-08).
- `evaluate` returns READY only under a certified policy with every threshold decided
  and met by every run, enough trades and days in total, and every required regime
  covered. Otherwise it returns NOT_READY with every reason, never just the first.
- READY issues `x02:live-qualification:<id>`, bound to the policy version. It is valid
  only while that version is current, so moving the goalposts voids it.
QA evidence never replaces M06's risk authority; a qualification only allows the owner
to approve a Live promotion in M07.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.core.contracts import ContractModel
from app.core.ids import stable_id
from app.core.time import Clock, utc_now
from app.core.types import Dec, UtcDatetime
from app.storage.audit import audit
from app.storage.db import transaction
from app.validation.metrics import Metric

RUN_PREFIX = "x02:run:"
QUALIFICATION_PREFIX = "x02:live-qualification:"


class Verdict(StrEnum):
    READY = "READY"
    NOT_READY = "NOT_READY"


class MetricValue(ContractModel):
    value: Dec | None
    n: int = Field(ge=0)


class ValidationRun(ContractModel):
    kind: str
    subject_refs: list[str]
    config: dict[str, str]
    regimes: list[str]
    window_start: UtcDatetime
    window_end: UtcDatetime
    metrics: dict[str, MetricValue]


class Criterion(ContractModel):
    metric: str
    at_least: Dec | None = None
    at_most: Dec | None = None

    @property
    def decided(self) -> bool:
        return self.at_least is not None or self.at_most is not None


class ReadinessPolicy(ContractModel):
    version: str = Field(min_length=1)
    certified: bool
    criteria: list[Criterion]
    min_trades: int | None
    min_days: int | None
    required_regimes: list[str]

    @model_validator(mode="after")
    def _criteria_unique(self) -> Self:
        names = [c.metric for c in self.criteria]
        if len(set(names)) != len(names):
            raise ValueError("one criterion per metric")
        return self


DEFAULT_POLICY = ReadinessPolicy(
    version="readiness-0-uncertified",
    certified=False,
    criteria=[Criterion(metric=m) for m in ("expectancy", "profit_factor", "max_drawdown")],
    min_trades=None,
    min_days=None,
    required_regimes=["TREND", "RANGE"],
)
"""Every threshold TBD and uncertified: nothing can qualify until they are decided."""


@dataclass(frozen=True)
class Readiness:
    verdict: Verdict
    reasons: list[str]
    qualification_ref: str | None


class EvidenceStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    # --- runs ------------------------------------------------------------------------------

    def record_run(
        self,
        *,
        kind: str,
        subject_refs: list[str],
        config: dict[str, str],
        regimes: list[str],
        window_start: datetime,
        window_end: datetime,
        metrics: dict[str, Metric],
    ) -> str:
        run = ValidationRun(
            kind=kind, subject_refs=subject_refs, config=config, regimes=sorted(regimes),
            window_start=window_start, window_end=window_end,
            metrics={k: MetricValue(value=m.value, n=m.n) for k, m in metrics.items()},
        )  # fmt: skip
        body = run.model_dump_json()
        run_id = stable_id("run", body).removeprefix("run-")
        self._conn.execute(
            "INSERT INTO validation_runs (run_id, kind, body, recorded_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (run_id) DO NOTHING",
            (run_id, kind, body, self._clock().isoformat()),
        )
        return RUN_PREFIX + run_id

    def run(self, ref: str) -> ValidationRun | None:
        row = self._conn.execute(
            "SELECT body FROM validation_runs WHERE run_id = ?", (ref.removeprefix(RUN_PREFIX),)
        ).fetchone()
        return None if row is None else ValidationRun.model_validate_json(row[0])

    # --- policies --------------------------------------------------------------------------

    def save_policy(self, policy: ReadinessPolicy, *, actor: str = "x02") -> None:
        body = policy.model_dump_json()
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT body FROM readiness_policies WHERE version = ?", (policy.version,)
            ).fetchone()
            if row is not None:
                if ReadinessPolicy.model_validate_json(row[0]) != policy:
                    raise ValueError(f"policy {policy.version} is immutable: publish a new version")
                return
            self._conn.execute(
                "INSERT INTO readiness_policies (version, body, hash, saved_at)"
                " VALUES (?, ?, ?, ?)",
                (policy.version, body, stable_id("policy", body), self._clock().isoformat()),
            )
            audit(self._conn, actor=actor, action="validation.policy", detail=policy.version)

    def policy(self, version: str) -> ReadinessPolicy:
        row = self._conn.execute(
            "SELECT body FROM readiness_policies WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no readiness policy {version}")
        return ReadinessPolicy.model_validate_json(row[0])

    def current_policy_version(self) -> str | None:
        row = self._conn.execute(
            "SELECT version FROM readiness_policies ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else str(row[0])

    # --- qualifications --------------------------------------------------------------------

    def issue(self, policy_version: str, run_refs: list[str]) -> str:
        """Record a qualification; only `evaluate` calls this, on READY."""
        body = json.dumps({"policy": policy_version, "runs": sorted(run_refs)})
        qid = stable_id("q", body).removeprefix("q-")
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO live_qualifications (qualification_id, policy_version, body,"
                " issued_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (qid, policy_version, body, self._clock().isoformat()),
            )
            audit(self._conn, actor="x02", action="validation.qualified",
                  detail=f"{QUALIFICATION_PREFIX}{qid} under {policy_version}")  # fmt: skip
        return QUALIFICATION_PREFIX + qid

    def verify_live_qualification(self, ref: str) -> bool:
        """Issued by this store, under the policy version that is current now."""
        if not ref.startswith(QUALIFICATION_PREFIX):
            return False
        row = self._conn.execute(
            "SELECT policy_version FROM live_qualifications WHERE qualification_id = ?",
            (ref.removeprefix(QUALIFICATION_PREFIX),),
        ).fetchone()
        return row is not None and row[0] == self.current_policy_version()


def _failures(c: Criterion, value: Decimal) -> list[str]:
    out = []
    if c.at_least is not None and value < c.at_least:
        out.append(f"METRIC_BELOW:{c.metric}")
    if c.at_most is not None and value > c.at_most:
        out.append(f"METRIC_ABOVE:{c.metric}")
    return out


def evaluate(store: EvidenceStore, policy_version: str, run_refs: list[str]) -> Readiness:
    policy = store.policy(policy_version)
    reasons: list[str] = []
    if not policy.certified:
        reasons.append("POLICY_UNCERTIFIED")
    reasons += [f"THRESHOLD_TBD:{c.metric}" for c in policy.criteria if not c.decided]
    runs: list[ValidationRun] = []
    for ref in run_refs:
        run = store.run(ref)
        if run is None:
            reasons.append(f"RUN_UNKNOWN:{ref}")
        else:
            runs.append(run)
    if not runs:
        reasons.append("NO_EVIDENCE")
    for c in (c for c in policy.criteria if c.decided):
        values = [r.metrics.get(c.metric) for r in runs]
        known = [v.value for v in values if v is not None and v.value is not None]
        if len(known) < len(values):
            reasons.append(f"METRIC_MISSING:{c.metric}")
        reasons += sorted({f for v in known for f in _failures(c, v)})  # every run must pass
    trades = sum(max((m.n for m in r.metrics.values()), default=0) for r in runs)
    if policy.min_trades is None:
        reasons.append("MIN_TRADES_TBD")
    elif trades < policy.min_trades:
        reasons.append("MIN_TRADES")
    if policy.min_days is None:
        reasons.append("MIN_DAYS_TBD")
    elif not runs or (max(r.window_end for r in runs) - min(r.window_start for r in runs)).days < (
        policy.min_days
    ):
        reasons.append("MIN_DAYS")
    covered = {g for r in runs for g in r.regimes}
    reasons += [f"REGIME_MISSING:{g}" for g in policy.required_regimes if g not in covered]
    if reasons:
        return Readiness(Verdict.NOT_READY, reasons, None)
    return Readiness(Verdict.READY, [], store.issue(policy_version, run_refs))
