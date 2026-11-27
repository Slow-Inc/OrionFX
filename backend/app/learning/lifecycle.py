"""Dataset manifests, model promotion with evidence, rollback and drift (#61; LRN-04/05/06;
AT-13, AT-16, AT-35). Nothing trains here and nothing promotes itself.

- `DatasetManifest`: an immutable, hashed data window. Its splits are exactly train,
  then validation, then an optional holdout, in time order, non-overlapping and
  inside the window, so training cannot see later data.
- `ModelLifecycle` on the M08 registry:
  - `propose_candidate` takes a `ModelCard` (the one source of hash, modes and
    validation metrics), needs at least one known dataset, registers the card's
    manifest and stores the candidate. It returns a `model.candidate`.
  - `promote` makes a stored candidate the champion. Only the owner may approve, by
    allow-list (OWNER_ACTOR); no module or system actor can. It needs evidence refs and
    a policy ref, and the modes must be ones the model supports. LIVE additionally
    needs a separate `x02:live-qualification:` ref; it is never implied by DEMO.
    The previous champion is disabled.
  - `rollback` (owner only) restores the champion that preceded the current one, using
    a champion stack: rolling back twice never brings back the version just rolled
    away from. A restored approval that has expired is refused, not silently written.
  - `learning_promotions` is the champion-pointer log; the registry's eligibility
    records stay the authority on what may run. Every change is audited.
- Drift: `psi` (Population Stability Index) per feature over reference-quantile bins.
  PSI > RETRAIN_PSI [PROPOSED], or a feature missing from either window, recommends a
  retrain. That recommendation is an audit entry for the owner; nothing retrains.
"""

import bisect
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
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
    minimum_eligibility,
)
from app.core.enums import Eligibility, Mode
from app.core.time import Clock, utc_now
from app.core.types import UtcDatetime
from app.prediction.card import ModelCard
from app.storage.audit import audit
from app.storage.db import transaction
from app.storage.registry import ArtifactRegistry, UnknownSubject, split_ref, subject_ref

D = Decimal
RETRAIN_PSI = D("0.25")
PSI_FLOOR = D("0.0001")
"""Empty-bin proportion floor, so ln() stays finite."""
OWNER_ACTOR = "owner"
LIVE_EVIDENCE_PREFIX = "x02:live-qualification:"
SPLIT_ORDER = ("train", "validation", "holdout")


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
        names = tuple(s.name for s in self.splits)
        if names not in (SPLIT_ORDER[:2], SPLIT_ORDER):
            raise ValueError("splits must be train, validation[, holdout] in that order")
        previous_end = self.window_start
        for s in self.splits:
            if not (self.window_start <= s.start < s.end <= self.window_end):
                raise ValueError(f"split {s.name} is not inside the window")
            if s.start < previous_end:
                raise ValueError(f"split {s.name} overlaps the one before it")
            previous_end = s.end
        return self

    @property
    def ref(self) -> str:
        return subject_ref(self.dataset_id, self.version)


class DatasetStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock = utc_now) -> None:
        self._conn = conn
        self._clock = clock

    def save(self, manifest: DatasetManifest, *, actor: str = "m07") -> None:
        with transaction(self._conn):
            existing = self.get(manifest.dataset_id, manifest.version)
            if existing is not None:
                if existing != manifest:
                    raise LifecycleRefused(f"dataset {manifest.ref} exists with other content")
                return
            self._conn.execute(
                "INSERT INTO learning_datasets (dataset_id, version, body, saved_at)"
                " VALUES (?, ?, ?, ?)",
                (manifest.dataset_id, manifest.version, manifest.model_dump_json(),
                 self._clock().isoformat()),
            )  # fmt: skip
            audit(self._conn, actor=actor, action="learning.dataset",
                  detail=f"{manifest.ref} {manifest.content_hash}")  # fmt: skip

    def get(self, dataset_id: str, version: str) -> DatasetManifest | None:
        row = self._conn.execute(
            "SELECT body FROM learning_datasets WHERE dataset_id = ? AND version = ?",
            (dataset_id, version),
        ).fetchone()
        return None if row is None else DatasetManifest.model_validate_json(row[0])


def _require_owner(actor: str) -> None:
    if actor != OWNER_ACTOR:
        raise LifecycleRefused(f"{actor!r} cannot approve: only the owner can")


@dataclass(frozen=True)
class _Entry:
    action: str
    version: str
    record: EligibilityRecord


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

    # --- candidates ------------------------------------------------------------------------

    def propose_candidate(self, card: ModelCard, *, dataset_refs: list[str]) -> ModelCandidate:
        if not dataset_refs:
            raise LifecycleRefused("a candidate needs the dataset it was trained on")
        for ref in dataset_refs:
            try:
                dataset_id, version = split_ref(ref)
            except UnknownSubject:
                raise LifecycleRefused(f"malformed dataset ref {ref!r}") from None
            if self.datasets.get(dataset_id, version) is None:
                raise LifecycleRefused(f"unknown dataset {ref}")
        manifest = card.manifest()
        candidate = ModelCandidate(
            model_id=card.model_id, model_version=card.model_version, dataset_refs=dataset_refs,
            artifact_sha256=manifest.artifact_hash.removeprefix("sha256:"),
            evaluation=dict(card.validation),
        )  # fmt: skip
        with transaction(self._conn):
            self.registry.register("model", manifest, actor="m07")
            self._conn.execute(
                "INSERT INTO learning_candidates (model_id, version, body, proposed_at)"
                " VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (card.model_id, card.model_version, candidate.model_dump_json(),
                 self._clock().isoformat()),
            )  # fmt: skip
        return candidate

    def candidate(self, model_id: str, version: str) -> ModelCandidate | None:
        row = self._conn.execute(
            "SELECT body FROM learning_candidates WHERE model_id = ? AND version = ?",
            (model_id, version),
        ).fetchone()
        return None if row is None else ModelCandidate.model_validate_json(row[0])

    # --- champions -------------------------------------------------------------------------

    def champion(self, model_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT version FROM learning_promotions WHERE model_id = ? ORDER BY id DESC LIMIT 1",
            (model_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    def _history(self, model_id: str) -> list[_Entry]:
        rows = self._conn.execute(
            "SELECT action, version, record FROM learning_promotions WHERE model_id = ?"
            " ORDER BY id",
            (model_id,),
        ).fetchall()
        return [_Entry(a, v, EligibilityRecord.model_validate_json(r)) for a, v, r in rows]

    @staticmethod
    def _stack(history: list[_Entry]) -> list[str]:
        """Champions in order; a rollback pops the version it rolled away from."""
        stack: list[str] = []
        for e in history:
            if e.action == "ROLLBACK":
                stack.pop()
            else:
                if e.version in stack:
                    stack.remove(e.version)
                stack.append(e.version)
        return stack

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
               approval: str) -> None:  # fmt: skip
        previous = self.champion(model_id)
        if previous is not None and previous != version:
            self._disable(model_id, previous, actor=OWNER_ACTOR)
        self.registry.set_eligibility(record, actor=OWNER_ACTOR)
        self._conn.execute(
            "INSERT INTO learning_promotions (model_id, version, action, record, approved_by,"
            " approval, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (model_id, version, action, record.model_dump_json(), OWNER_ACTOR, approval,
             self._clock().isoformat()),
        )  # fmt: skip
        audit(self._conn, actor=OWNER_ACTOR, action=f"learning.{action.lower()}",
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
        live_qualification_ref: str | None = None,
        expires_at: datetime | None = None,
    ) -> ModelApproved:
        _require_owner(approved_by)
        if not evidence_refs:
            raise LifecycleRefused("promotion needs evidence refs")
        if not modes:
            raise LifecycleRefused("promotion needs at least one mode")
        manifest = self.registry.get("model", model_id, version)
        if manifest is None or self.candidate(model_id, version) is None:
            raise LifecycleRefused(f"{model_id}@{version} was never proposed as a candidate")
        unsupported = set(modes) - set(manifest.supported_modes)
        if unsupported:
            raise LifecycleRefused(f"{model_id}@{version} does not support {sorted(unsupported)}")
        evidence = list(evidence_refs)
        if Mode.LIVE in modes:
            if live_qualification_ref is None or not live_qualification_ref.startswith(
                LIVE_EVIDENCE_PREFIX
            ):
                raise LifecycleRefused(f"live approval needs a {LIVE_EVIDENCE_PREFIX} ref")
            evidence.append(live_qualification_ref)
        record = EligibilityRecord(
            subject_hash=manifest.artifact_hash, subject_kind="model",
            subject_ref=subject_ref(model_id, version), user_enabled=True,
            eligibility=minimum_eligibility(modes), approved_modes=modes, context={},
            evidence_refs=evidence, policy_version=policy_ref, expires_at=expires_at,
            revoked_at=None,
        )  # fmt: skip
        with transaction(self._conn):
            self._crown(model_id, version, record, action="PROMOTE", approval=approval_ref)
        return ModelApproved(model_id=model_id, model_version=version, approval_ref=approval_ref,
                             evidence_refs=evidence, policy_ref=policy_ref,
                             allowed_modes=modes)  # fmt: skip

    def rollback(self, model_id: str, *, approved_by: str, reason: str) -> str:
        """Restore the champion before the current one; returns its version."""
        _require_owner(approved_by)
        history = self._history(model_id)
        stack = self._stack(history)
        if len(stack) < 2:
            raise LifecycleRefused(f"{model_id} has no previous champion to roll back to")
        current, version = stack[-1], stack[-2]
        record = next(e.record for e in reversed(history)
                      if e.version == version and e.action == "PROMOTE")  # fmt: skip
        if not any(record.approves(m, self._clock()) for m in record.approved_modes):
            raise LifecycleRefused(f"{model_id}@{version}'s approval has expired: promote it anew")
        with transaction(self._conn):
            self._crown(model_id, version, record, action="ROLLBACK",
                        approval=f"rollback from {current}: {reason}")  # fmt: skip
        return version

    # --- drift -----------------------------------------------------------------------------

    def record_drift(self, model_id: str, report: "DriftReport") -> None:
        action = "learning.retrain_recommended" if report.retrain_recommended else "learning.drift"
        detail = ", ".join(f"{k}={v}" for k, v in sorted(report.psi.items()))
        missing = f"; missing: {', '.join(report.missing)}" if report.missing else ""
        detail = f"model {model_id}: {detail}{missing} (threshold {report.threshold})"
        audit(self._conn, actor="m07", action=action, detail=detail)


def psi(reference: list[Decimal], recent: list[Decimal], *, bins: int = 10) -> Decimal:
    """Population Stability Index over bins cut at the reference quantiles."""
    if not reference or not recent:
        raise ValueError("PSI needs two non-empty windows")
    ref = sorted(reference)
    edges = sorted({ref[i * len(ref) // bins] for i in range(1, bins)})
    ref_counts = Counter(bisect.bisect_right(edges, x) for x in reference)
    new_counts = Counter(bisect.bisect_right(edges, x) for x in recent)
    total = D(0)
    for b in range(len(edges) + 1):
        e = max(D(ref_counts[b]) / len(reference), PSI_FLOOR)
        r = max(D(new_counts[b]) / len(recent), PSI_FLOOR)
        total += (r - e) * (r / e).ln()
    return total.quantize(D("0.0001"))


@dataclass(frozen=True)
class DriftReport:
    psi: dict[str, Decimal]
    drifted: list[str]
    missing: list[str] = field(default_factory=list)
    """Features present in only one window: a broken feed is uncertainty, not calm."""
    threshold: Decimal = RETRAIN_PSI

    @property
    def retrain_recommended(self) -> bool:
        return bool(self.drifted or self.missing)


def drift_report(
    reference: dict[str, list[Decimal]], recent: dict[str, list[Decimal]]
) -> DriftReport:
    both = sorted(set(reference) & set(recent))
    values = {k: psi(reference[k], recent[k]) for k in both}
    return DriftReport(
        psi=values,
        drifted=[k for k, v in values.items() if v > RETRAIN_PSI],
        missing=sorted(set(reference) ^ set(recent)),
    )
