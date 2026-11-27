"""Dataset manifests, model promotion with evidence, rollback and drift (#61; LRN-04/05/06;
AT-13, AT-16, AT-35). Nothing trains here and nothing promotes itself.

- `DatasetManifest`: an immutable, hashed data window. Its splits (train, validation,
  optional holdout) are time-ordered, non-overlapping and inside the window, so
  training cannot see validation or holdout data.
- `ModelLifecycle` on the M08 registry:
  - `propose_candidate` registers a model version that names known datasets and
    returns a `model.candidate`.
  - `promote` makes it the champion. It needs evidence refs, a policy ref and an
    approval by the owner; an automated actor (a module, the system) is refused. The
    eligibility follows the modes: LIVE needs a live-qualification evidence ref and is
    never implied by DEMO approval. The previous champion is disabled.
  - `rollback` restores the previous champion with its own recorded approval and
    disables the current one, again only with the owner.
  - Every change is audited and kept in an append-only promotion history.
- Drift: `psi` (Population Stability Index) per feature, over reference-quantile bins.
  PSI > RETRAIN_PSI [PROPOSED] recommends a retrain. That recommendation is an audit
  entry for the owner; nothing retrains on its own.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import model_validator

from app.core.contracts import (
    ContractModel,
    EligibilityRecord,
    ModelApproved,
    ModelCandidate,
    Sha256Ref,
    StrategyManifest,
)
from app.core.enums import Eligibility, Mode
from app.core.time import Clock, utc_now
from app.core.types import UtcDatetime
from app.storage.audit import audit
from app.storage.db import transaction
from app.storage.registry import ArtifactRegistry, subject_ref

D = Decimal
RETRAIN_PSI = D("0.25")
PSI_FLOOR = D("0.0001")
"""Empty-bin proportion floor, so ln() stays finite."""
LIVE_EVIDENCE_MARK = "live-qualification"
AUTOMATED_ACTORS = frozenset(
    {"system", "ai", "m01", "m02", "m03", "m04", "m05", "m06", "m07", "m08", "x01", "x02"}
)
REQUIRED_SPLITS = ("train", "validation")


class LifecycleRefused(ValueError):
    pass


class Split(ContractModel):
    name: str
    start: UtcDatetime
    end: UtcDatetime


class DatasetManifest(ContractModel):
    dataset_id: str
    version: str
    source_refs: list[str]
    window_start: UtcDatetime
    window_end: UtcDatetime
    splits: list[Split]
    content_hash: Sha256Ref

    @model_validator(mode="after")
    def _isolated(self) -> Self:
        names = [s.name for s in self.splits]
        for required in REQUIRED_SPLITS:
            if required not in names:
                raise ValueError(f"a dataset needs a {required} split")
        if len(set(names)) != len(names):
            raise ValueError("split names must be unique")
        previous_end = self.window_start
        for s in self.splits:
            if not (self.window_start <= s.start < s.end <= self.window_end):
                raise ValueError(f"split {s.name} is not inside the window")
            if s.start < previous_end:
                raise ValueError(f"split {s.name} overlaps or precedes the one before it")
            previous_end = s.end
        return self

    @property
    def ref(self) -> str:
        return f"{self.dataset_id}@{self.version}"


class DatasetStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def save(self, manifest: DatasetManifest) -> None:
        body = manifest.model_dump_json()
        with transaction(self._conn):
            existing = self.get(manifest.dataset_id, manifest.version)
            if existing is not None:
                if existing != manifest:
                    raise LifecycleRefused(f"dataset {manifest.ref} exists with other content")
                return
            self._conn.execute(
                "INSERT INTO learning_datasets (dataset_id, version, body, saved_at)"
                " VALUES (?, ?, ?, ?)",
                (manifest.dataset_id, manifest.version, body, self._clock().isoformat()),
            )

    def get(self, dataset_id: str, version: str) -> DatasetManifest | None:
        row = self._conn.execute(
            "SELECT body FROM learning_datasets WHERE dataset_id = ? AND version = ?",
            (dataset_id, version),
        ).fetchone()
        return None if row is None else DatasetManifest.model_validate_json(row[0])


def _eligibility_for(modes: list[Mode]) -> Eligibility:
    if Mode.LIVE in modes:
        return Eligibility.LIVE_ELIGIBLE
    if Mode.DEMO in modes:
        return Eligibility.DEMO_ELIGIBLE
    return Eligibility.SHADOW_ONLY


def _require_owner(actor: str) -> None:
    if not actor.strip() or actor.strip().lower() in AUTOMATED_ACTORS:
        raise LifecycleRefused(f"{actor!r} cannot approve: the owner must")


class ModelLifecycle:
    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: ArtifactRegistry,
        datasets: DatasetStore,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._conn = conn
        self.registry = registry
        self.datasets = datasets
        self._clock = clock

    # --- candidates and champions ---------------------------------------------------------

    def propose_candidate(
        self,
        manifest: StrategyManifest,
        *,
        dataset_refs: list[str],
        evaluation: dict[str, Decimal | None],
    ) -> ModelCandidate:
        for ref in dataset_refs:
            dataset_id, _, version = ref.partition("@")
            if self.datasets.get(dataset_id, version) is None:
                raise LifecycleRefused(f"unknown dataset {ref}")
        self.registry.register("model", manifest, actor="m07")
        return ModelCandidate(
            model_id=manifest.strategy_id,
            model_version=manifest.version,
            dataset_refs=dataset_refs,
            artifact_sha256=manifest.artifact_hash.removeprefix("sha256:"),
            evaluation=evaluation,
        )

    def _history(self, model_id: str) -> list[tuple[str, EligibilityRecord]]:
        rows = self._conn.execute(
            "SELECT version, record FROM learning_promotions WHERE model_id = ? ORDER BY id",
            (model_id,),
        ).fetchall()
        return [(v, EligibilityRecord.model_validate_json(r)) for v, r in rows]

    def champion(self, model_id: str) -> str | None:
        history = self._history(model_id)
        return history[-1][0] if history else None

    def _disable(self, model_id: str, version: str, *, actor: str) -> None:
        current = self.registry.eligibility("model", subject_ref(model_id, version))
        if current is None:
            return
        self.registry.set_eligibility(
            current.model_copy(update={
                "eligibility": Eligibility.DISABLED, "approved_modes": [],
                "revoked_at": self._clock(),
            }),
            actor=actor,
        )  # fmt: skip

    def _crown(self, model_id: str, version: str, record: EligibilityRecord, *, action: str,
               approved_by: str, approval: str) -> None:  # fmt: skip
        previous = self.champion(model_id)
        if previous is not None and previous != version:
            self._disable(model_id, previous, actor=approved_by)
        self.registry.set_eligibility(record, actor=approved_by)
        self._conn.execute(
            "INSERT INTO learning_promotions (model_id, version, action, record, approved_by,"
            " approval, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (model_id, version, action, record.model_dump_json(), approved_by, approval,
             self._clock().isoformat()),
        )  # fmt: skip
        audit(self._conn, actor=approved_by, action=f"learning.{action.lower()}",
              detail=f"model {model_id}@{version} {approval}")  # fmt: skip

    def promote(
        self,
        model_id: str,
        version: str,
        *,
        modes: list[Mode],
        evidence_refs: list[str],
        policy_ref: str,
        approved_by: str,
        approval_ref: str,
        expires_at: datetime | None = None,
    ) -> ModelApproved:
        _require_owner(approved_by)
        if not evidence_refs:
            raise LifecycleRefused("promotion needs evidence refs")
        if not modes:
            raise LifecycleRefused("promotion needs at least one mode")
        manifest = self.registry.get("model", model_id, version)
        if manifest is None:
            raise LifecycleRefused(f"{model_id}@{version} was never proposed as a candidate")
        if Mode.LIVE in modes and not any(LIVE_EVIDENCE_MARK in e for e in evidence_refs):
            raise LifecycleRefused("live approval needs live-qualification evidence")
        record = EligibilityRecord(
            subject_hash=manifest.artifact_hash, subject_kind="model",
            subject_ref=subject_ref(model_id, version), user_enabled=True,
            eligibility=_eligibility_for(modes), approved_modes=modes, context={},
            evidence_refs=evidence_refs, policy_version=policy_ref, expires_at=expires_at,
            revoked_at=None,
        )  # fmt: skip
        with transaction(self._conn):
            self._crown(model_id, version, record, action="PROMOTE", approved_by=approved_by,
                        approval=approval_ref)  # fmt: skip
        return ModelApproved(model_id=model_id, model_version=version, approval_ref=approval_ref,
                             evidence_refs=evidence_refs, policy_ref=policy_ref,
                             allowed_modes=modes)  # fmt: skip

    def rollback(self, model_id: str, *, approved_by: str, reason: str) -> str:
        """Restore the champion before the current one; returns its version."""
        _require_owner(approved_by)
        history = self._history(model_id)
        current = history[-1][0] if history else None
        earlier = [(v, r) for v, r in history if v != current]
        if current is None or not earlier:
            raise LifecycleRefused(f"{model_id} has no previous champion to roll back to")
        version, record = earlier[-1]
        with transaction(self._conn):
            self._crown(model_id, version, record, action="ROLLBACK", approved_by=approved_by,
                        approval=f"rollback from {current}: {reason}")  # fmt: skip
        return version

    # --- drift ---------------------------------------------------------------------------

    def record_drift(self, model_id: str, report: "DriftReport") -> None:
        action = "learning.retrain_recommended" if report.retrain_recommended else "learning.drift"
        detail = ", ".join(f"{k}={v}" for k, v in sorted(report.psi.items()))
        audit(self._conn, actor="m07", action=action, detail=f"model {model_id}: {detail}")


def _bin(x: Decimal, edges: list[Decimal]) -> int:
    return sum(x >= e for e in edges)


def psi(reference: list[Decimal], recent: list[Decimal], *, bins: int = 10) -> Decimal:
    """Population Stability Index over bins cut at the reference quantiles."""
    if not reference or not recent:
        raise ValueError("PSI needs two non-empty windows")
    ref = sorted(reference)
    edges = sorted({ref[i * len(ref) // bins] for i in range(1, bins)})
    total = D(0)
    for b in range(len(edges) + 1):
        e = max(D(sum(_bin(x, edges) == b for x in reference)) / len(reference), PSI_FLOOR)
        r = max(D(sum(_bin(x, edges) == b for x in recent)) / len(recent), PSI_FLOOR)
        total += (r - e) * (r / e).ln()
    return total.quantize(D("0.0001"))


@dataclass(frozen=True)
class DriftReport:
    psi: dict[str, Decimal]
    drifted: list[str]

    @property
    def retrain_recommended(self) -> bool:
        return bool(self.drifted)


def drift_report(
    reference: dict[str, list[Decimal]], recent: dict[str, list[Decimal]]
) -> DriftReport:
    values = {k: psi(reference[k], recent[k]) for k in sorted(reference) if k in recent}
    return DriftReport(values, [k for k, v in values.items() if v > RETRAIN_PSI])
