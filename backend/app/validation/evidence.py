"""Evidence store, protected readiness policy and gate, live qualifications (#68; VAL-04,
VAL-05, VAL-06; AT-16, AT-24; master plan §11.1).

- A validation run (replay, walk-forward, scenario) is stored append-only. Its ref,
  `x02:run:<hash>`, is a hash of its content, so the same evidence has the same ref.
- A `ReadinessPolicy` lists metric criteria, minimum trades and days, and the regimes
  that must be covered. Anything not yet decided stays None (TBD). Versions are
  immutable; changing a policy means saving a new version, and the newest is current.
  A certified policy must have criteria and regimes. The shipped `DEFAULT_POLICY` is
  uncertified with every threshold TBD, so nothing qualifies until the owner and the
  modules set real thresholds (OD-07, OD-08).
- `EvidenceStore.evaluate` always judges against the current policy. It returns READY
  only if the policy is certified, every threshold is decided and met by every run,
  and coverage is enough. Coverage counts distinct evidence: overlapping run windows
  merge (their trades count once, the largest), gaps between runs are not covered days,
  and a run ref passed twice is refused. Otherwise it returns NOT_READY with every reason.
- READY issues `x02:live-qualification:<hash>` for the tested subjects under that policy
  version. `verify_live_qualification(ref, subject)` holds only for one of those
  subjects and only while that version is still current.
QA evidence never replaces M06's risk authority; a qualification only lets the owner
approve a Live promotion in M07.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from app.core.contracts import ContractModel
from app.core.ids import content_hash
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


class ValidationRun(ContractModel):
    kind: str
    subject_refs: list[str]
    """What was tested, e.g. `model:<id>@<version>`, `strategy:<id>@<version>`."""
    config: dict[str, str]
    regimes: list[str]
    window_start: UtcDatetime
    window_end: UtcDatetime
    trades: int = Field(ge=0)
    metrics: dict[str, Metric]

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("a run window must end after it starts")
        return self


class ValidationQualification(ContractModel):
    policy_version: str
    run_refs: list[str]
    subject_refs: list[str]


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
    def _sound(self) -> Self:
        names = [c.metric for c in self.criteria]
        if len(set(names)) != len(names):
            raise ValueError("one criterion per metric")
        if self.certified and not (self.criteria and self.required_regimes):
            raise ValueError("a certified policy needs criteria and required regimes")
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


def _failures(c: Criterion, value: Decimal) -> list[str]:
    out = []
    if c.at_least is not None and value < c.at_least:
        out.append(f"METRIC_BELOW:{c.metric}")
    if c.at_most is not None and value > c.at_most:
        out.append(f"METRIC_ABOVE:{c.metric}")
    return out


def _coverage(runs: list[ValidationRun]) -> tuple[int, int]:
    """(trades, days) of distinct evidence: overlapping windows merge, keeping the
    largest trade count of the merged runs; gaps between windows add no days."""
    clusters: list[tuple[datetime, datetime, int]] = []
    for r in sorted(runs, key=lambda r: r.window_start):
        if clusters and r.window_start < clusters[-1][1]:
            start, end, n = clusters[-1]
            clusters[-1] = (start, max(end, r.window_end), max(n, r.trades))
        else:
            clusters.append((r.window_start, r.window_end, r.trades))
    trades = sum(n for _, _, n in clusters)
    days = sum((end - start for start, end, _ in clusters), timedelta(0)).days
    return trades, days


def _reasons(policy: ReadinessPolicy, runs: list[ValidationRun]) -> list[str]:
    reasons: list[str] = []
    if not policy.certified:
        reasons.append("POLICY_UNCERTIFIED")
    for c in policy.criteria:
        if not c.decided:
            reasons.append(f"THRESHOLD_TBD:{c.metric}")
            continue
        values = [r.metrics.get(c.metric) for r in runs]
        known = [m.value for m in values if m is not None and m.value is not None]
        if len(known) < len(values):
            reasons.append(f"METRIC_MISSING:{c.metric}")
        reasons += sorted({f for v in known for f in _failures(c, v)})  # every run must pass
    trades, days = _coverage(runs)
    if policy.min_trades is None:
        reasons.append("MIN_TRADES_TBD")
    elif trades < policy.min_trades:
        reasons.append("MIN_TRADES")
    if policy.min_days is None:
        reasons.append("MIN_DAYS_TBD")
    elif days < policy.min_days:
        reasons.append("MIN_DAYS")
    covered = {g for r in runs for g in r.regimes}
    reasons += [f"REGIME_MISSING:{g}" for g in policy.required_regimes if g not in covered]
    return reasons


class EvidenceStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    # --- runs ------------------------------------------------------------------------------

    def record_run(self, run: ValidationRun) -> str:
        body = run.model_dump_json()
        run_id = content_hash(body)
        self._conn.execute(
            "INSERT INTO validation_runs (run_id, kind, body, recorded_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (run_id) DO NOTHING",
            (run_id, run.kind, body, self._clock().isoformat()),
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
        digest = content_hash(body)
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT hash FROM readiness_policies WHERE version = ?", (policy.version,)
            ).fetchone()
            if row is not None:
                if row[0] != digest:
                    raise ValueError(f"policy {policy.version} is immutable: save a new version")
                return
            self._conn.execute(
                "INSERT INTO readiness_policies (version, body, hash, saved_at)"
                " VALUES (?, ?, ?, ?)",
                (policy.version, body, digest, self._clock().isoformat()),
            )
            audit(self._conn, actor=actor, action="validation.policy", detail=policy.version)

    def current_policy(self) -> ReadinessPolicy | None:
        row = self._conn.execute(
            "SELECT body FROM readiness_policies ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else ReadinessPolicy.model_validate_json(row[0])

    # --- the gate and its qualifications ---------------------------------------------------

    def evaluate(self, run_refs: list[str]) -> Readiness:
        """Judge the evidence against the current policy; READY issues a qualification."""
        policy = self.current_policy()
        if policy is None:
            return Readiness(Verdict.NOT_READY, ["NO_POLICY"], None)
        duplicated = sorted({r for r in run_refs if run_refs.count(r) > 1})
        reasons = [f"RUN_DUPLICATE:{r}" for r in duplicated]
        runs: list[ValidationRun] = []
        for ref in dict.fromkeys(run_refs):
            run = self.run(ref)
            if run is None:
                reasons.append(f"RUN_UNKNOWN:{ref}")
            else:
                runs.append(run)
        if not runs:
            reasons.append("NO_EVIDENCE")
        reasons += _reasons(policy, runs)
        if reasons:
            return Readiness(Verdict.NOT_READY, reasons, None)
        subjects = sorted({s for r in runs for s in r.subject_refs})
        return Readiness(Verdict.READY, [], self._issue(policy.version, run_refs, subjects))

    def _issue(self, policy_version: str, run_refs: list[str], subjects: list[str]) -> str:
        qid = content_hash(policy_version, sorted(run_refs))
        body = ValidationQualification(policy_version=policy_version, run_refs=sorted(run_refs),
                                       subject_refs=subjects).model_dump_json()  # fmt: skip
        with transaction(self._conn):
            inserted = self._conn.execute(
                "INSERT INTO live_qualifications (qualification_id, policy_version, body,"
                " issued_at) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (qid, policy_version, body, self._clock().isoformat()),
            ).rowcount
            if inserted:
                audit(self._conn, actor="x02", action="validation.qualified",
                      detail=f"{QUALIFICATION_PREFIX}{qid} under {policy_version}")  # fmt: skip
        return QUALIFICATION_PREFIX + qid

    def verify_live_qualification(self, ref: str, subject: str) -> bool:
        """Issued here for `subject`, under the policy version that is current now."""
        if not ref.startswith(QUALIFICATION_PREFIX):
            return False
        row = self._conn.execute(
            "SELECT body FROM live_qualifications WHERE qualification_id = ?"
            " AND policy_version = (SELECT version FROM readiness_policies ORDER BY id DESC"
            " LIMIT 1)",
            (ref.removeprefix(QUALIFICATION_PREFIX),),
        ).fetchone()
        if row is None:
            return False
        return subject in ValidationQualification.model_validate_json(row[0]).subject_refs
