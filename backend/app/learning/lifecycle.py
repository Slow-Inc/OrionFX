"""Dataset manifests, model promotion with evidence, rollback and drift (#61; LRN-04/05/06;
AT-13, AT-16, AT-35). Nothing trains here and nothing promotes itself.

- `DatasetManifest`: an immutable, hashed data window. Its splits are exactly train,
  then validation, then an optional holdout, in time order, non-overlapping and
  inside the window, so training cannot see later data.
- `ModelLifecycle` on the M08 registry:
  - `propose_candidate` takes a `ModelCard` (the one source of hash, modes and
    validation metrics), needs at least one known dataset, registers the card's
    manifest and stores the candidate. It returns a `model.candidate`.
  - `promote` makes a stored candidate the champion. Two approvers, by allow-list: the
    owner (OWNER_ACTOR), for any mode; and the policy-bound system actor (SYSTEM_ACTOR,
    ADR 0001, #133), for SHADOW and DEMO only, which never handles a Live approval. No
    other module can approve. It needs evidence refs and a policy ref, and the modes
    must be ones the model supports. LIVE additionally
    needs a `x02:live-qualification:` ref that X02's verifier confirms was issued for
    this model under the current policy; with no verifier wired, LIVE is refused.
    The previous champion is disabled.
  - `rollback` restores the champion that preceded the current one, using a champion
    stack: rolling back twice never brings back the version just rolled away from. A
    restored approval that has expired is refused, not silently written, and the system
    actor never restores one that approves LIVE.
  - `withdraw` disables the current champion and leaves none (M05 waits); the withdrawn
    version is off the stack, so no rollback brings it back. `renew` extends the current,
    unexpired champion's approval. The system actor may do either only to an approval
    without LIVE. `tenure` is the champion and when it took over (a renewal is not one).
  - `learning_promotions` is the champion-pointer log; the registry's eligibility
    records stay the authority on what may run. Every change is audited.
- Drift: `psi` (Population Stability Index) per feature, over per-value bins for a
  feature declared categorical and reference-quantile bins otherwise.
  PSI > RETRAIN_PSI [PROPOSED], or a feature missing from either window, recommends a
  retrain. That recommendation is an audit entry for the owner; nothing retrains.
"""

import bisect
import sqlite3
from collections import Counter
from collections.abc import Callable, Collection, Hashable
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
from app.validation.evidence import QUALIFICATION_PREFIX

D = Decimal
RETRAIN_PSI = D("0.25")
PSI_FLOOR = D("0.0001")
"""Empty-bin proportion floor, so ln() stays finite."""
OWNER_ACTOR = "owner"
SYSTEM_ACTOR = "m07.autonomy"
"""The policy-bound system actor of ADR 0001: SHADOW and DEMO only."""
SYSTEM_MODES = frozenset({Mode.SHADOW, Mode.DEMO})
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


def _require_approver(actor: str, modes: Collection[Mode]) -> None:
    """The owner approves anything; the system actor only SHADOW and DEMO (ADR 0001)."""
    if actor == SYSTEM_ACTOR:
        if not set(modes) <= SYSTEM_MODES:
            raise LifecycleRefused(f"{actor} acts on SHADOW and DEMO only, not "
                                   f"{sorted(set(modes) - SYSTEM_MODES)}")  # fmt: skip
    elif actor != OWNER_ACTOR:
        raise LifecycleRefused(f"{actor!r} cannot approve: only the owner can")


@dataclass(frozen=True)
class _Entry:
    action: str
    version: str
    record: EligibilityRecord
    at: datetime


class ModelLifecycle:
    def __init__(
        self,
        conn: sqlite3.Connection,
        registry: ArtifactRegistry,
        datasets: DatasetStore,
        *,
        clock: Clock = utc_now,
        live_verifier: Callable[[str, str], bool] | None = None,
    ) -> None:
        """`live_verifier(ref, subject)` is X02's `EvidenceStore.verify_live_qualification`:
        it checks that a Live qualification was issued for this model under the current
        readiness policy. Without it, Live promotion is refused (fails closed)."""
        self._conn = conn
        self.registry = registry
        self.datasets = datasets
        self._clock = clock
        self._live_verifier = live_verifier

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

    def shadow(self, model_id: str, version: str, *, approved_by: str, evidence_refs: list[str],
               policy_ref: str, expires_at: datetime) -> None:  # fmt: skip
        """Approve a candidate, never the champion, for SHADOW only until `expires_at`, so
        its predictions can be recorded beside the champion's. Only the champion pointer
        feeds decisions, so this approval never makes it trade."""
        _require_approver(approved_by, [Mode.SHADOW])
        manifest = self.registry.get("model", model_id, version)
        if manifest is None or self.candidate(model_id, version) is None:
            raise LifecycleRefused(f"{model_id}@{version} was never proposed as a candidate")
        if self.champion(model_id) == version:
            raise LifecycleRefused(f"{model_id}@{version} is the champion, not a candidate")
        record = EligibilityRecord(
            subject_hash=manifest.artifact_hash, subject_kind="model",
            subject_ref=subject_ref(model_id, version), user_enabled=True,
            eligibility=Eligibility.SHADOW_ONLY, approved_modes=[Mode.SHADOW],
            context={"role": "shadow-candidate"}, evidence_refs=evidence_refs,
            policy_version=policy_ref, expires_at=expires_at, revoked_at=None,
        )  # fmt: skip
        with transaction(self._conn):
            self.registry.set_eligibility(record, actor=approved_by)
            audit(self._conn, actor=approved_by, action="learning.shadow",
                  detail=f"model {model_id}@{version} until {expires_at.isoformat()}")  # fmt: skip

    def end_shadow(self, model_id: str, version: str, *, approved_by: str, reason: str) -> None:
        """Disable a shadowing candidate's approval (never the champion's)."""
        _require_approver(approved_by, [Mode.SHADOW])
        if self.champion(model_id) == version:
            raise LifecycleRefused(f"{model_id}@{version} is the champion, not a candidate")
        with transaction(self._conn):
            self._disable(model_id, version, actor=approved_by)
            audit(self._conn, actor=approved_by, action="learning.shadow_end",
                  detail=f"model {model_id}@{version}: {reason}")  # fmt: skip

    # --- champions -------------------------------------------------------------------------

    def champion(self, model_id: str) -> str | None:
        tenure = self.tenure(model_id)
        return None if tenure is None else tenure[0]

    def tenure(self, model_id: str) -> tuple[str, datetime] | None:
        """The champion and when it took over (its PROMOTE or ROLLBACK); None after a
        WITHDRAW, until the next promotion."""
        history = self._history(model_id)
        if not history or history[-1].action == "WITHDRAW":
            return None
        version = self._stack(history)[-1]
        since = next(e.at for e in reversed(history)
                     if e.version == version and e.action in ("PROMOTE", "ROLLBACK"))  # fmt: skip
        return version, since

    def _history(self, model_id: str) -> list[_Entry]:
        rows = self._conn.execute(
            "SELECT action, version, record, at FROM learning_promotions WHERE model_id = ?"
            " ORDER BY id",
            (model_id,),
        ).fetchall()
        return [_Entry(a, v, EligibilityRecord.model_validate_json(r), datetime.fromisoformat(t))
                for a, v, r, t in rows]  # fmt: skip

    @staticmethod
    def _stack(history: list[_Entry]) -> list[str]:
        """Champions in order; a rollback pops the version it rolled away from, and a
        withdrawal the version withdrawn."""
        stack: list[str] = []
        for e in history:
            if e.action in ("ROLLBACK", "WITHDRAW"):
                stack.pop()
            else:
                if e.version in stack:
                    stack.remove(e.version)
                stack.append(e.version)
        return stack

    def _disable(self, model_id: str, version: str, *, actor: str) -> EligibilityRecord | None:
        current = self.registry.eligibility("model", subject_ref(model_id, version))
        if current is None:
            return None
        disabled = current.model_copy(update={
            "eligibility": Eligibility.DISABLED, "approved_modes": [], "revoked_at": self._clock(),
        })  # fmt: skip
        self.registry.set_eligibility(disabled, actor=actor)
        return disabled

    def _log(self, model_id: str, version: str, record: EligibilityRecord, *, action: str,
             actor: str, approval: str) -> None:  # fmt: skip
        self._conn.execute(
            "INSERT INTO learning_promotions (model_id, version, action, record, approved_by,"
            " approval, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (model_id, version, action, record.model_dump_json(), actor, approval,
             self._clock().isoformat()),
        )  # fmt: skip
        audit(self._conn, actor=actor, action=f"learning.{action.lower()}",
              detail=f"model {model_id}@{version} {approval}")  # fmt: skip

    def _crown(self, model_id: str, version: str, record: EligibilityRecord, *, action: str,
               actor: str, approval: str) -> None:  # fmt: skip
        previous = self.champion(model_id)
        if previous is not None and previous != version:
            current = self.registry.eligibility("model", subject_ref(model_id, previous))
            if current is not None:  # the system actor never replaces an owner's Live approval
                _require_approver(actor, current.approved_modes)
            self._disable(model_id, previous, actor=actor)
        self.registry.set_eligibility(record, actor=actor)
        self._log(model_id, version, record, action=action, actor=actor, approval=approval)

    def _current(self, model_id: str, actor: str) -> tuple[str, EligibilityRecord]:
        """The champion and its current approval, if `actor` may act on that approval."""
        version = self.champion(model_id)
        if version is None:
            raise LifecycleRefused(f"{model_id} has no champion")
        record = self.registry.eligibility("model", subject_ref(model_id, version))
        if record is None or not any(record.approves(m, self._clock())
                                     for m in record.approved_modes):  # fmt: skip
            raise LifecycleRefused(f"{model_id}@{version}'s approval has expired or was revoked")
        _require_approver(actor, record.approved_modes)
        return version, record

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
        if not modes:
            raise LifecycleRefused("promotion needs at least one mode")
        _require_approver(approved_by, modes)
        if not evidence_refs:
            raise LifecycleRefused("promotion needs evidence refs")
        manifest = self.registry.get("model", model_id, version)
        if manifest is None or self.candidate(model_id, version) is None:
            raise LifecycleRefused(f"{model_id}@{version} was never proposed as a candidate")
        unsupported = set(modes) - set(manifest.supported_modes)
        if unsupported:
            raise LifecycleRefused(f"{model_id}@{version} does not support {sorted(unsupported)}")
        evidence = list(evidence_refs)
        if Mode.LIVE in modes:
            if live_qualification_ref is None:
                raise LifecycleRefused(f"live approval needs a {QUALIFICATION_PREFIX} ref")
            if self._live_verifier is None:
                raise LifecycleRefused("live approval needs X02's verifier wired in")
            if not self._live_verifier(live_qualification_ref, f"model:{model_id}@{version}"):
                raise LifecycleRefused(
                    f"{live_qualification_ref} was not issued by X02 for this model under the"
                    " current policy"
                )
            evidence.append(live_qualification_ref)
        record = EligibilityRecord(
            subject_hash=manifest.artifact_hash, subject_kind="model",
            subject_ref=subject_ref(model_id, version), user_enabled=True,
            eligibility=minimum_eligibility(modes), approved_modes=modes, context={},
            evidence_refs=evidence, policy_version=policy_ref, expires_at=expires_at,
            revoked_at=None,
        )  # fmt: skip
        with transaction(self._conn):
            self._crown(model_id, version, record, action="PROMOTE", actor=approved_by,
                        approval=approval_ref)  # fmt: skip
        return ModelApproved(model_id=model_id, model_version=version, approval_ref=approval_ref,
                             evidence_refs=evidence, policy_ref=policy_ref,
                             allowed_modes=modes)  # fmt: skip

    def rollback(self, model_id: str, *, approved_by: str, reason: str) -> str:
        """Restore the champion before the current one; returns its version."""
        _require_approver(approved_by, ())
        current = self.champion(model_id)
        if current is None:
            raise LifecycleRefused(f"{model_id} has no champion to roll back from")
        history = self._history(model_id)
        stack = self._stack(history)
        if len(stack) < 2:
            raise LifecycleRefused(f"{model_id} has no previous champion to roll back to")
        version = stack[-2]
        # Its latest approval: a renewal may have extended the one it was promoted with.
        record = next(e.record for e in reversed(history)
                      if e.version == version and e.action != "WITHDRAW")  # fmt: skip
        if not any(record.approves(m, self._clock()) for m in record.approved_modes):
            raise LifecycleRefused(f"{model_id}@{version}'s approval has expired: promote it anew")
        _require_approver(approved_by, record.approved_modes)
        with transaction(self._conn):
            self._crown(model_id, version, record, action="ROLLBACK", actor=approved_by,
                        approval=f"rollback from {current}: {reason}")  # fmt: skip
        return version

    def withdraw(self, model_id: str, *, approved_by: str, reason: str) -> str:
        """Disable the champion and leave none: M05 waits. Returns the withdrawn version."""
        with transaction(self._conn):
            version, _ = self._current(model_id, approved_by)
            disabled = self._disable(model_id, version, actor=approved_by)
            assert disabled is not None  # _current read it
            self._log(model_id, version, disabled, action="WITHDRAW", actor=approved_by,
                      approval=f"withdraw: {reason}")  # fmt: skip
        return version

    def renew(self, model_id: str, *, approved_by: str, expires_at: datetime, reason: str) -> None:
        """Extend the current, unexpired champion's approval to `expires_at`."""
        with transaction(self._conn):
            version, record = self._current(model_id, approved_by)
            renewed = record.model_copy(update={"expires_at": expires_at})
            self.registry.set_eligibility(renewed, actor=approved_by)
            self._log(model_id, version, renewed, action="RENEW", actor=approved_by,
                      approval=f"renew to {expires_at.isoformat()}: {reason}")  # fmt: skip

    # --- drift -----------------------------------------------------------------------------

    def record_drift(self, model_id: str, report: "DriftReport") -> None:
        action = "learning.retrain_recommended" if report.retrain_recommended else "learning.drift"
        detail = ", ".join(f"{k}={v}" for k, v in sorted(report.psi.items()))
        missing = f"; missing: {', '.join(report.missing)}" if report.missing else ""
        detail = f"model {model_id}: {detail}{missing} (threshold {report.threshold})"
        audit(self._conn, actor="m07", action=action, detail=detail)


def psi(
    reference: list[Decimal], recent: list[Decimal], *, bins: int = 10, categorical: bool = False
) -> Decimal:
    """Population Stability Index. A feature declared `categorical` (a code, a regime)
    gets one bin per reference value, plus one for values the reference never had: a
    new code is drift. Otherwise bins are cut at the reference quantiles, and a value
    filling more than one quantile (a point mass, e.g. a binary or sparse feature's
    zero) gets a bin of its own instead of sharing one with the values above it. An
    ordinal feature reaching a new neighbouring value is therefore not alarmed on."""
    if not reference or not recent:
        raise ValueError("PSI needs two non-empty windows")
    seen = set(reference)
    key: Callable[[Decimal], Hashable]
    if categorical:
        key = lambda x: x if x in seen else None  # noqa: E731
    else:
        ref = sorted(reference)
        cuts = Counter(ref[i * len(ref) // bins] for i in range(1, bins))
        edges = sorted(cuts)
        masses = {c for c, n in cuts.items() if n > 1}
        key = lambda x: (bisect.bisect_right(edges, x), x in masses)  # noqa: E731
    ref_counts = Counter(map(key, reference))
    new_counts = Counter(map(key, recent))
    total = D(0)
    for b in ref_counts.keys() | new_counts.keys():
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
    reference: dict[str, list[Decimal]],
    recent: dict[str, list[Decimal]],
    *,
    categorical: Collection[str] = (),
) -> DriftReport:
    both = sorted(set(reference) & set(recent))
    values = {k: psi(reference[k], recent[k], categorical=k in categorical) for k in both}
    return DriftReport(
        psi=values,
        drifted=[k for k, v in values.items() if v > RETRAIN_PSI],
        missing=sorted(set(reference) ^ set(recent)),
    )
