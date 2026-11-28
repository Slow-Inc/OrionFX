"""The autonomous champion/candidate loop, SHADOW and DEMO only (#133; PRD #120 slice 6;
ADR 0001 with ADR 0002; protocol v1 §1a, §8.4, §9; training-loop.md §7).

Everything here acts as `SYSTEM_ACTOR`, through `ModelLifecycle`, under one certified
protocol hash. It reads the protocol and its claim through `ClaimLedger`, a port onto
#132's run log, and the candidate's card through `ModelArtifacts`.

1. **Admit** (`Autonomy.submit`). A candidate is a registered card plus its weights, plus
   the claim its protocol run made. It shadows only if all of these hold, else every
   reason is recorded:
   - its protocol is the active one, registered and certified;
   - the holdout is consumed;
   - the claim is the card's own config (`card.training.config_hash`), with the model
     tier, the system tier and the holdout passed;
   - the spot-parity check passed;
   - `label_version` is the current `LABEL_VERSION` and the policy's, and
     `feature_schema` is the policy's;
   - the card is the registered one, and the version was never submitted before.

   An admitted candidate gets a SHADOW-only approval until the shadow window's end, so
   the recorder records its predictions with their provenance. Only the champion's
   forecast is journaled (`app.runtime.autonomy`), so a candidate never feeds a decision
   and never trades.
2. **Judge** (`Autonomy.step`), once per candidate, at the first step where the
   pre-declared window has passed and enough resolved labels exist. Scores are on LIVE
   predictions with a known `m07.labels.tb.v2` label, after admission, paired on the bars
   both models predicted. The rule: the candidate's Brier must beat the champion's by at
   least `min_brier_gain` (no champion: the window's climatology). The admission checks
   run again first, so a changed protocol refuses. A win promotes (SHADOW/DEMO, approval
   for `approval_ttl_hours`); a loss or a refusal rejects. A window that ends without
   enough labels expires. Judging once is the pre-declared stop: no re-peeking.
3. **Monitor and kill.** The champion, from its tenure on: CUSUM of its per-bar Brier
   above its promotion-time Brier, binned ECE, PSI per card feature between the window
   before its tenure and the latest one, and realised DEMO expectancy per closed cycle.
   A breach rolls back to the previous champion, or withdraws to no model (M05 waits). A
   killed model never comes back on its own.
4. **Renew** a healthy champion's approval before it expires, only while it has resolved
   LIVE predictions in the last TTL. A stalled loop or feed lets the approval lapse.

**Limits.**
- No numbers in code: every threshold is a named value of the certified protocol
  (`autonomy.<field>`); a missing or unparseable one refuses the policy.
- An account mode outside SHADOW/DEMO refuses everything. The lifecycle refuses LIVE to
  the system actor, and never lets it replace, restore, renew or withdraw a Live approval.
- Nothing here reads LLM output or news, writes the risk config or calls M06. It never
  mints `x02:live-qualification:` and never touches a `ReadinessPolicy`. Every step is
  an append-only `learning_autonomy` record, plus the lifecycle's own audit.
"""

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

import numpy as np

from app.core.bet import LABEL_VERSION
from app.core.contracts import ContractModel, EligibilityRecord
from app.core.enums import Mode, Timeframe
from app.core.time import Clock, utc_now
from app.learning.bars import BarLog, Provenance
from app.learning.evaluation import binned_ece, brier, class_prior
from app.learning.labels import Outcome, prediction_labels
from app.learning.lifecycle import (
    SYSTEM_ACTOR,
    SYSTEM_MODES,
    LifecycleRefused,
    ModelLifecycle,
    psi,
)
from app.market_data.features import FEATURE_SCHEMA
from app.market_data.session import SessionSchedule
from app.market_data.store import CandleStore
from app.prediction.card import BRIER, ModelCard
from app.prediction.transforms import TRANSFORM_SCHEMA, transform_from_store
from app.storage.audit import audit
from app.storage.db import transaction
from app.storage.registry import subject_ref

D = Decimal
PREFIX = "autonomy."
CLASSES = (Outcome.UP, Outcome.DOWN, Outcome.FLAT)
"""The label's class order, as the trainer and `evaluation` use it."""


class AutonomyRefused(Exception):
    """The loop may not act: no certified policy, or an account mode outside SHADOW/DEMO."""


# --- ports -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CertifiedProtocol:
    certified_by: str
    values: Mapping[str, str]
    """The protocol's named values, literal text (#132 `ProtocolValue`)."""


@dataclass(frozen=True)
class Claim:
    """What a protocol's claim run established: its config and each tier's verdict."""

    config_hash: str
    model_tier: bool
    system_tier: bool
    holdout: bool
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class Check:
    passed: bool
    evidence_ref: str


class ClaimLedger(Protocol):
    """A port onto #132's run log (`RunLog`, unmerged). `RunLog.protocol` and
    `holdout_consumed` map directly; `RunLog` records neither the holdout's verdict nor
    which config it was opened for, so `claim` needs that once it lands, and
    `spot_parity` needs the spot feed (#17)."""

    def certified(self, protocol_hash: str) -> CertifiedProtocol | None:
        """The registered protocol with this hash, re-verified; None if unregistered."""
        ...

    def holdout_consumed(self, protocol_hash: str) -> bool: ...

    def claim(self, protocol_hash: str) -> Claim | None: ...

    def spot_parity(self, protocol_hash: str, model_ref: str) -> Check | None: ...


@dataclass(frozen=True)
class ModelFiles:
    card: ModelCard
    weights: bytes
    module: str
    """The reviewed adapter in the allowlisted package that builds it."""


class ModelArtifacts(Protocol):
    """Where a registered model's card and weights are kept (the import of #134)."""

    def files(self, model_id: str, version: str) -> ModelFiles | None: ...


# --- the policy ------------------------------------------------------------------------------


@dataclass(frozen=True)
class PromotionPolicy:
    """The loop's numbers, read only from the certified protocol's named values
    (`autonomy.<field>`). Nothing has a default in code."""

    protocol_hash: str
    certified_by: str
    label_version: str
    feature_schema: str
    modes: tuple[Mode, ...]
    shadow_min_days: int
    shadow_max_days: int
    shadow_min_resolved: int
    min_brier_gain: Decimal
    cusum_k: Decimal
    cusum_h: Decimal
    ece_max: Decimal
    ece_min_resolved: int
    psi_max: Decimal
    psi_window_days: int
    psi_min_rows: int
    demo_expectancy_floor_usd: Decimal
    demo_min_cycles: int
    approval_ttl_hours: int
    renew_within_hours: int

    @property
    def ref(self) -> str:
        return f"protocol:{self.protocol_hash}"

    @property
    def ttl(self) -> timedelta:
        return timedelta(hours=self.approval_ttl_hours)

    @classmethod
    def from_protocol(
        cls, protocol_hash: str, protocol: CertifiedProtocol | None
    ) -> "PromotionPolicy":
        if protocol is None:
            raise AutonomyRefused(f"PROTOCOL_UNREGISTERED:{protocol_hash}")
        if not protocol.certified_by.strip():
            raise AutonomyRefused(f"PROTOCOL_UNCERTIFIED:{protocol_hash}")
        parsed: dict[str, Any] = {"protocol_hash": protocol_hash,
                                  "certified_by": protocol.certified_by}  # fmt: skip
        bad = []
        for f in fields(cls)[2:]:
            try:
                parsed[f.name] = _parse(f.type, protocol.values.get(PREFIX + f.name))
            except (ValueError, ArithmeticError):
                bad.append(PREFIX + f.name)
        if bad:
            raise AutonomyRefused(f"POLICY_INCOMPLETE:{','.join(bad)}")
        policy = cls(**parsed)
        if not policy.modes or not set(policy.modes) <= SYSTEM_MODES:
            raise AutonomyRefused("POLICY_MODES: SHADOW and DEMO only")
        if policy.shadow_max_days <= policy.shadow_min_days:
            raise AutonomyRefused("POLICY_WINDOW: shadow_max_days must exceed shadow_min_days")
        return policy


def _parse(kind: object, text: str | None) -> object:
    """Literal protocol text to a value; never negative, never empty."""
    value = (text or "").strip()
    if not value:
        raise ValueError("missing")
    if kind is str:
        return value
    if kind is int:
        number: int | Decimal = int(value)
    elif kind is Decimal:
        number = D(value)
        if not number.is_finite():
            raise ValueError(value)
    else:
        return tuple(Mode(m.strip()) for m in value.split(","))
    if number < 0:
        raise ValueError(value)
    return number


# --- the audit record ------------------------------------------------------------------------


class Action(StrEnum):
    ADMIT = "ADMIT"
    REFUSE = "REFUSE"
    PROMOTE = "PROMOTE"
    REJECT = "REJECT"
    EXPIRE = "EXPIRE"
    ROLLBACK = "ROLLBACK"
    WITHDRAW = "WITHDRAW"
    RENEW = "RENEW"


SHADOW_ENDS = (Action.PROMOTE, Action.REJECT, Action.EXPIRE)


class AutonomyRecord(ContractModel):
    """One autonomous step: who acted, on what evidence, under which protocol, and the
    champion and its approval before and after."""

    action: Action
    actor: str = SYSTEM_ACTOR
    model_id: str
    version: str
    protocol_hash: str
    account_mode: Mode
    evidence_refs: list[str]
    champion_before: str | None
    champion_after: str | None
    approval_before: EligibilityRecord | None
    approval_after: EligibilityRecord | None
    reasons: list[str]
    metrics: dict[str, str]


# --- scoring ---------------------------------------------------------------------------------

Scored = dict[datetime, tuple[tuple[float, float, float], int]]
"""Per bar: a model's class probabilities and the bar's class label (index in CLASSES)."""
Resolver = Callable[[str, str, datetime, datetime], Scored]
"""(model_id, version, start, now) -> its LIVE predictions with a known label."""
Windows = tuple[dict[str, list[Decimal]], dict[str, list[Decimal]]]
"""(reference, recent) values per card feature, for PSI."""


def label_resolver(conn: sqlite3.Connection, candles: CandleStore, *, symbol: str,
                   timeframe: Timeframe, session: SessionSchedule | None) -> Resolver:  # fmt: skip
    """Recorded predictions scored on the training label (`prediction_labels`, tb.v2):
    LIVE provenance only, a class label (not AMBIGUOUS), probabilities of LABEL_VERSION."""

    def resolve(model_id: str, version: str, start: datetime, now: datetime) -> Scored:
        out: Scored = {}
        for rec, lab in prediction_labels(conn, candles, symbol, model_id, timeframe,
                                          start=start, end=now, now=now, session=session,
                                          model_version=version):  # fmt: skip
            c = rec.prediction.class_probabilities
            if (rec.provenance is not Provenance.LIVE or lab is None or not lab.labelled
                    or c is None or c.label_version != LABEL_VERSION):  # fmt: skip
                continue
            out[rec.as_of] = ((float(c.up), float(c.down), float(c.flat)),
                              CLASSES.index(lab.outcome))  # fmt: skip
        return out

    return resolve


def _arrays(rows: Sequence[tuple[tuple[float, float, float], int]]) -> tuple[Any, Any]:
    return (np.asarray([p for p, _ in rows], dtype=np.float64),
            np.asarray([y for _, y in rows], dtype=np.int64))  # fmt: skip


@dataclass(frozen=True)
class ShadowScore:
    n: int
    candidate_brier: float | None
    reference_brier: float | None
    reference: str
    """`champion:<ref>` (paired on common bars) or `climatology` (no champion)."""

    @property
    def gain(self) -> float | None:
        if self.candidate_brier is None or self.reference_brier is None:
            return None
        return self.reference_brier - self.candidate_brier

    def metrics(self) -> dict[str, str]:
        out = {"n": str(self.n), "reference": self.reference}
        for name, value in (
            ("candidate_brier", self.candidate_brier),
            ("reference_brier", self.reference_brier),
            ("gain", self.gain),
        ):
            if value is not None:
                out[name] = f"{value:.6f}"
        return out  # fmt: skip


def shadow_score(
    candidate: Scored, champion: Scored | None, champion_ref: str | None
) -> ShadowScore:
    if champion is None:
        rows = [candidate[t] for t in sorted(candidate)]
        if not rows:
            return ShadowScore(0, None, None, "climatology")
        p, y = _arrays(rows)
        prior = np.tile(class_prior(y), (len(y), 1))
        return ShadowScore(len(rows), brier(p, y), brier(prior, y), "climatology")
    common = sorted(candidate.keys() & champion.keys())
    if not common:
        return ShadowScore(0, None, None, f"champion:{champion_ref}")
    p, y = _arrays([candidate[t] for t in common])
    q, _ = _arrays([champion[t] for t in common])
    return ShadowScore(len(common), brier(p, y), brier(q, y), f"champion:{champion_ref}")


def breaches(
    policy: PromotionPolicy,
    scored: Scored,
    *,
    reference_brier: float | None,
    features: Windows | None,
    demo_nets: Sequence[Decimal],
) -> tuple[list[str], dict[str, str]]:
    """The kill switches on a champion's forward evidence; (breaches, what was measured).
    `features` is (reference, recent) per card feature, or None when a window is thin."""
    reasons: list[str] = []
    metrics: dict[str, str] = {"resolved": str(len(scored))}
    rows = [scored[t] for t in sorted(scored)]
    if rows and reference_brier is not None:
        p, y = _arrays(rows)
        per_bar = ((p - np.eye(3)[y]) ** 2).sum(axis=1)
        s = peak = 0.0
        for b in per_bar:  # one-sided CUSUM of the Brier above its reference
            s = max(0.0, s + float(b) - reference_brier - float(policy.cusum_k))
            peak = max(peak, s)
        metrics["brier_cusum"] = f"{peak:.6f}"
        if peak > float(policy.cusum_h):
            reasons.append("BRIER_CUSUM")
    if len(rows) >= policy.ece_min_resolved:
        ece = binned_ece(*_arrays(rows))
        metrics["ece"] = f"{ece:.6f}"
        if ece > float(policy.ece_max):
            reasons.append("ECE")
    if features is not None:
        reference, recent = features
        for key in sorted(reference.keys() | recent.keys()):
            ref, new = reference.get(key, []), recent.get(key, [])
            if not ref or not new:  # a feature gone from one window: a broken feed
                reasons.append(f"PSI_MISSING:{key}")
                continue
            value = psi(ref, new)
            metrics[f"psi:{key}"] = str(value)
            if value > policy.psi_max:
                reasons.append(f"PSI:{key}")
    if len(demo_nets) >= policy.demo_min_cycles:
        expectancy = sum(demo_nets, D(0)) / len(demo_nets)
        metrics["demo_expectancy_usd"] = str(expectancy)
        if expectancy < policy.demo_expectancy_floor_usd:
            reasons.append("DEMO_EXPECTANCY")
    return reasons, metrics


# --- the loop --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Shadowing:
    version: str
    since: datetime
    admitted: AutonomyRecord


@dataclass(frozen=True)
class Status:
    """The operator view: what the loop runs now and what it did last."""

    champion: str | None
    since: datetime | None
    approved_until: datetime | None
    shadowing: list[Shadowing]
    recent: list[tuple[datetime, AutonomyRecord]]


def _guard(mode: Mode) -> None:
    if mode not in SYSTEM_MODES:
        raise AutonomyRefused(f"account mode {mode}: the loop runs in SHADOW and DEMO only")


class Autonomy:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        lifecycle: ModelLifecycle,
        ledger: ClaimLedger,
        artifacts: ModelArtifacts,
        candles: CandleStore,
        symbol: str,
        timeframe: Timeframe,
        model_id: str,
        protocol_hash: str,
        session: SessionSchedule | None = None,
        resolver: Resolver | None = None,
        demo_nets: Callable[[datetime], Sequence[Decimal]] = lambda since: (),
        clock: Clock = utc_now,
    ) -> None:
        """`demo_nets(since)`: realised nets of the cycles closed since then (read-only)."""
        self._conn, self._life, self._ledger, self._artifacts = conn, lifecycle, ledger, artifacts
        self._candles, self._symbol, self._timeframe = candles, symbol, timeframe
        self._session, self._model_id, self._protocol_hash = session, model_id, protocol_hash
        self._resolve = resolver or label_resolver(
            conn, candles, symbol=symbol, timeframe=timeframe, session=session
        )
        self._demo_nets, self._clock = demo_nets, clock

    @property
    def model_id(self) -> str:
        return self._model_id

    def policy(self) -> PromotionPolicy:
        digest = self._protocol_hash
        return PromotionPolicy.from_protocol(digest, self._ledger.certified(digest))

    # --- admission -------------------------------------------------------------------------

    def refusals(self, files: ModelFiles | None, protocol_hash: str,
                 policy: PromotionPolicy) -> list[str]:  # fmt: skip
        """Why this candidate may not shadow (or be promoted) under `policy`; [] if it may."""
        if files is None:
            return ["NO_ARTIFACT"]
        card, reasons = files.card, []
        if card.model_id != self._model_id:
            reasons.append("OTHER_MODEL")
        if protocol_hash != policy.protocol_hash:
            reasons.append("PROTOCOL_CHANGED")
        if not self._ledger.holdout_consumed(protocol_hash):
            reasons.append("HOLDOUT_NOT_CONSUMED")
        claim = self._ledger.claim(protocol_hash)
        if claim is None:
            reasons.append("NO_CLAIM")
        else:
            config = None if card.training is None else card.training.config_hash
            if config is None or _bare(config) != _bare(claim.config_hash):
                reasons.append("CLAIM_OF_ANOTHER_CONFIG")
            for tier, passed in (("MODEL_TIER", claim.model_tier),
                                 ("SYSTEM_TIER", claim.system_tier),
                                 ("HOLDOUT", claim.holdout)):  # fmt: skip
                if not passed:
                    reasons.append(f"{tier}_NOT_PASSED")
        parity = self._ledger.spot_parity(protocol_hash, card.ref)
        if parity is None or not parity.passed:
            reasons.append("SPOT_PARITY_NOT_PASSED")
        if card.label_version is None or not (card.label_version == LABEL_VERSION
                                              == policy.label_version):  # fmt: skip
            reasons.append("LABEL_VERSION_MISMATCH")
        if card.feature_schema != policy.feature_schema:
            reasons.append("FEATURE_SCHEMA_MISMATCH")
        manifest = self._life.registry.get("model", card.model_id, card.model_version)
        if (self._life.candidate(card.model_id, card.model_version) is None
                or manifest is None or card.artifact_hash is None
                or manifest != card.manifest()):  # fmt: skip
            reasons.append("CARD_NOT_REGISTERED")
        return reasons

    def _evidence(self, protocol_hash: str, model_ref: str) -> list[str]:
        claim = self._ledger.claim(protocol_hash)
        parity = self._ledger.spot_parity(protocol_hash, model_ref)
        refs = [] if claim is None else list(claim.evidence_refs)
        return refs + ([] if parity is None else [parity.evidence_ref])

    def submit(self, version: str, *, protocol_hash: str, mode: Mode) -> list[str]:
        """Admit a candidate to shadow under the active policy; returns the refusals ([]
        means admitted). Every attempt is recorded. A version is judged once, ever."""
        _guard(mode)
        now = self._clock()
        policy = self.policy()
        files = self._artifacts.files(self._model_id, version)
        reasons = self.refusals(files, protocol_hash, policy)
        if self.history(version):
            reasons.append("ALREADY_SUBMITTED")
        refs = self._evidence(protocol_hash, subject_ref(self._model_id, version))
        until = now + timedelta(days=policy.shadow_max_days)
        with transaction(self._conn):
            if not reasons:
                self._life.shadow(self._model_id, version, approved_by=SYSTEM_ACTOR,
                                  evidence_refs=refs, policy_ref=policy.ref,
                                  expires_at=until)  # fmt: skip
            self._record(Action.REFUSE if reasons else Action.ADMIT, version, protocol_hash,
                         mode, refs, reasons, {})  # fmt: skip
        return reasons

    # --- the scheduled step ----------------------------------------------------------------

    def step(self, *, mode: Mode) -> list[AutonomyRecord]:
        """One pass: monitor the champion (kill or renew), then judge every candidate whose
        window has closed. Returns what it recorded."""
        _guard(mode)
        policy = self.policy()
        now = self._clock()
        done = self._monitor(policy, mode, now)
        for s in self.shadowing():
            done += self._judge(policy, s, mode, now)
        return done

    def _judge(self, policy: PromotionPolicy, s: Shadowing, mode: Mode,
               now: datetime) -> list[AutonomyRecord]:  # fmt: skip
        champion = self._life.champion(self._model_id)
        score = shadow_score(
            self._resolve(self._model_id, s.version, s.since, now),
            None if champion is None else self._resolve(self._model_id, champion, s.since, now),
            None if champion is None else subject_ref(self._model_id, champion),
        )  # fmt: skip
        elapsed = now - s.since
        ready = elapsed >= timedelta(days=policy.shadow_min_days)
        if score.n < policy.shadow_min_resolved or not ready:
            if elapsed < timedelta(days=policy.shadow_max_days):
                return []  # still shadowing
            return [self._end(s, Action.EXPIRE, mode, ["WINDOW_ENDED_WITHOUT_ENOUGH_LABELS"],
                              score.metrics())]  # fmt: skip
        reasons = self.refusals(self._artifacts.files(self._model_id, s.version),
                                s.admitted.protocol_hash, policy)  # fmt: skip
        gain = score.gain
        if gain is None or gain < float(policy.min_brier_gain):
            reasons.append("DOES_NOT_BEAT_THE_CHAMPION")
        if reasons:
            return [self._end(s, Action.REJECT, mode, reasons, score.metrics())]
        refs = s.admitted.evidence_refs
        before = None if champion is None else self._life.registry.eligibility(
            "model", subject_ref(self._model_id, champion))  # fmt: skip
        with transaction(self._conn):
            self._life.promote(self._model_id, s.version, modes=list(policy.modes),
                               evidence_refs=refs, policy_ref=policy.ref,
                               approved_by=SYSTEM_ACTOR, approval_ref=f"autonomy:{policy.ref}",
                               expires_at=now + policy.ttl)  # fmt: skip
            return [self._record(Action.PROMOTE, s.version, policy.protocol_hash, mode, refs,
                                 [], score.metrics(), before=champion,
                                 before_record=before)]  # fmt: skip

    def _end(self, s: Shadowing, action: Action, mode: Mode, reasons: list[str],
             metrics: dict[str, str]) -> AutonomyRecord:  # fmt: skip
        with transaction(self._conn):
            self._life.end_shadow(self._model_id, s.version, approved_by=SYSTEM_ACTOR,
                                  reason=",".join(reasons))  # fmt: skip
            return self._record(action, s.version, s.admitted.protocol_hash, mode,
                                s.admitted.evidence_refs, reasons, metrics)  # fmt: skip

    def _monitor(self, policy: PromotionPolicy, mode: Mode, now: datetime) -> list[AutonomyRecord]:
        tenure = self._life.tenure(self._model_id)
        if tenure is None:
            return []
        version, since = tenure
        ref = subject_ref(self._model_id, version)
        record = self._life.registry.eligibility("model", ref)
        if record is None or not set(record.approved_modes) <= SYSTEM_MODES:
            return []  # lapsed (M05 already waits), or the owner's Live approval: hands off
        files = self._artifacts.files(self._model_id, version)
        scored = self._resolve(self._model_id, version, since, now)
        metrics: dict[str, str] = {}
        if files is None:
            reasons = ["NO_ARTIFACT"]  # cannot be monitored: uncertainty, never calm
        else:
            reasons, metrics = breaches(
                policy, scored, reference_brier=self._reference_brier(version, files.card),
                features=self._features(policy, files.card, since, now),
                demo_nets=self._demo_nets(since) if mode is Mode.DEMO else (),
            )  # fmt: skip
        if reasons:
            return [self._kill(policy, version, mode, reasons, metrics)]
        expires = record.expires_at
        recent = any(t >= now - policy.ttl for t in scored)
        if expires is None or expires - now > timedelta(hours=policy.renew_within_hours):
            return []
        if not recent:
            return []  # no forward evidence lately: let the approval lapse
        with transaction(self._conn):
            self._life.renew(self._model_id, approved_by=SYSTEM_ACTOR,
                             expires_at=now + policy.ttl, reason="monitors clean")  # fmt: skip
            return [self._record(Action.RENEW, version, policy.protocol_hash, mode,
                                 record.evidence_refs, [], metrics, before=version,
                                 before_record=record)]  # fmt: skip

    def _kill(self, policy: PromotionPolicy, version: str, mode: Mode, reasons: list[str],
              metrics: dict[str, str]) -> AutonomyRecord:  # fmt: skip
        before = self._life.registry.eligibility("model", subject_ref(self._model_id, version))
        why = "kill: " + ",".join(reasons)
        with transaction(self._conn):
            try:
                self._life.rollback(self._model_id, approved_by=SYSTEM_ACTOR, reason=why)
                action = Action.ROLLBACK
            except LifecycleRefused as exc:
                metrics = {**metrics, "rollback_refused": str(exc)}
                self._life.withdraw(self._model_id, approved_by=SYSTEM_ACTOR, reason=why)
                action = Action.WITHDRAW
            return self._record(action, version, policy.protocol_hash, mode,
                                [] if before is None else before.evidence_refs, reasons,
                                metrics, before=version, before_record=before)  # fmt: skip

    def _reference_brier(self, version: str, card: ModelCard) -> float | None:
        """The Brier it was promoted on (its shadow score), else its card's validation."""
        for _, rec in reversed(self.history(version)):
            if rec.action is Action.PROMOTE and "candidate_brier" in rec.metrics:
                return float(rec.metrics["candidate_brier"])
        value = card.validation.get(BRIER)
        return None if value is None else float(value)

    def _features(self, policy: PromotionPolicy, card: ModelCard, since: datetime,
                  now: datetime) -> Windows | None:  # fmt: skip
        """(reference, recent) values of the card's features: the window before its tenure
        and the latest one, from the recorded snapshots (transformed for a transform
        model, as the recorder does). None when either window is thin."""
        window = timedelta(days=policy.psi_window_days)
        reference = self._window(card, since - window, since)
        recent = self._window(card, max(since, now - window), now)
        if min(reference[0], recent[0]) < policy.psi_min_rows:
            return None
        return reference[1], recent[1]

    def _window(self, card: ModelCard, start: datetime,
                end: datetime) -> tuple[int, dict[str, list[Decimal]]]:  # fmt: skip
        snaps = [r.snapshot for r in BarLog(self._conn).snapshots(
            self._symbol, self._timeframe, start=start, end=end)
            if r.dataset_version == self._candles.dataset_version
            and r.snapshot.feature_schema == FEATURE_SCHEMA]  # fmt: skip
        if card.feature_schema == TRANSFORM_SCHEMA:
            snaps = [transform_from_store(self._candles, s, session=self._session) for s in snaps]
        return len(snaps), {k: [v for s in snaps if (v := s.values.get(k)) is not None]
                            for k in card.required_features}  # fmt: skip

    # --- records ---------------------------------------------------------------------------

    def _record(self, action: Action, version: str, protocol_hash: str, mode: Mode,
                refs: list[str], reasons: list[str], metrics: dict[str, str], *,
                before: str | None = None,
                before_record: EligibilityRecord | None = None) -> AutonomyRecord:  # fmt: skip
        ref = subject_ref(self._model_id, version)
        after = self._life.champion(self._model_id)
        rec = AutonomyRecord(
            action=action, model_id=self._model_id, version=version, protocol_hash=protocol_hash,
            account_mode=mode, evidence_refs=refs, champion_before=before, champion_after=after,
            approval_before=before_record,
            approval_after=self._life.registry.eligibility("model", ref), reasons=reasons,
            metrics=metrics,
        )  # fmt: skip
        at = self._clock().isoformat()
        self._conn.execute(
            "INSERT INTO learning_autonomy (model_id, version, action, body, at)"
            " VALUES (?, ?, ?, ?, ?)",
            (self._model_id, version, action.value, rec.model_dump_json(), at),
        )
        audit(self._conn, actor=SYSTEM_ACTOR, action=f"learning.autonomy.{action.lower()}",
              detail=f"model {ref} under {protocol_hash}: {','.join(reasons) or 'ok'}")  # fmt: skip
        return rec

    def history(self, version: str | None = None) -> list[tuple[datetime, AutonomyRecord]]:
        rows = self._conn.execute(
            "SELECT at, body FROM learning_autonomy WHERE model_id = ?"
            " AND (? IS NULL OR version = ?) ORDER BY id",
            (self._model_id, version, version),
        ).fetchall()
        return [(datetime.fromisoformat(a), AutonomyRecord.model_validate_json(b)) for a, b in rows]

    def shadowing(self) -> list[Shadowing]:
        """Admitted candidates whose window has not ended, oldest first."""
        open_: dict[str, Shadowing] = {}
        for at, rec in self.history():
            if rec.action is Action.ADMIT:
                open_[rec.version] = Shadowing(rec.version, at, rec)
            elif rec.action in SHADOW_ENDS:
                open_.pop(rec.version, None)
        return list(open_.values())

    def status(self, *, recent: int = 20) -> Status:
        tenure = self._life.tenure(self._model_id)
        record = None if tenure is None else self._life.registry.eligibility(
            "model", subject_ref(self._model_id, tenure[0]))  # fmt: skip
        return Status(
            champion=None if tenure is None else tenure[0],
            since=None if tenure is None else tenure[1],
            approved_until=None if record is None else record.expires_at,
            shadowing=self.shadowing(), recent=self.history()[-recent:],
        )  # fmt: skip


def _bare(config_hash: str) -> str:
    return config_hash.removeprefix("sha256:")
