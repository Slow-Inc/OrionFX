"""Model card and predictor port (#46; PRE-02, PRE-04, UST-02).

A card is what a predictor promises: which features it reads (and the ranges it
was validated on), its horizon, what its score means and whether it is
calibrated (only with evidence), its validation numbers, and the artifact hash
the registry must hold. The built-in baseline has no artifact (hash None).

A trained model (#127) is one reviewed adapter module serving many versions: the card
names the adapter as its artifact, pins its weights file by sha256 and states its training
cutoff. The card then lives outside the hashed module, so its manifest pins the card's own
hash: the registry entry fixes adapter, card and, through the card, the weights.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Protocol, Self

from pydantic import Field, model_validator

from app.core.contracts import (
    CatalogId,
    ContractModel,
    ParameterBound,
    Sha256Ref,
    StrategyManifest,
    canonical_json,
    sha256_ref,
)
from app.core.enums import Direction, ForecastLineage, Mode, Timeframe
from app.core.types import UtcDatetime
from app.storage.registry import subject_ref

BRIER = "brier"
BASELINE_BRIER = "baseline_brier"
CARD_PIN = "card:"
"""Prefix of the card's own hash in its manifest's `dependencies` (adapter models only)."""


def unique_leader(scores: Mapping[Direction, Decimal]) -> tuple[Direction, Decimal] | None:
    """The direction with the strictly highest score, or None on a tie or no scores."""
    if not scores:
        return None
    top = max(scores.values())
    leaders = [d for d, s in scores.items() if s == top]
    return (leaders[0], top) if len(leaders) == 1 else None


class TrainingRecord(ContractModel):
    """How a trained model was made (#130): enough to rebuild it from the same inputs."""

    dataset_ref: str = Field(min_length=1)
    """The exported dataset (`dataset_id@version`) it was trained and calibrated on."""
    config_hash: Sha256Ref
    """sha256 of the canonical JSON of the training config."""
    seeds: list[int] = Field(min_length=1)
    libraries: dict[str, str]
    """Library versions the weights were produced with, e.g. {"lightgbm": "4.7.0"}."""
    code_commit: str = Field(min_length=1)
    """The commit the training code ran from (passed in: a `git archive` has no .git)."""


class ModelCard(ContractModel):
    model_id: str = Field(pattern=r"^[^@\s]+$")
    model_version: str = Field(pattern=r"^[^@\s]+$")
    artifact_hash: Sha256Ref | None
    """None only for the built-in, artifact-free baseline."""
    lineage: ForecastLineage
    feature_schema: str
    required_features: list[str] = Field(min_length=1)
    feature_ranges: dict[str, ParameterBound]
    horizon: Timeframe
    horizon_bars: int = Field(gt=0)
    semantics: str = Field(min_length=1)
    calibrated: bool
    validation: dict[str, Decimal]
    evidence_refs: list[str]
    label_version: str | None = None
    """The triple-barrier label a calibrated three-class model was calibrated on (#129).
    Its scores are then P(UP), P(DOWN) and P(FLAT, as NEUTRAL) of that label, which
    is the same for both sides, and the forecast carries them as class probabilities."""
    supported_modes: list[Mode] = Field(default_factory=lambda: [Mode.SHADOW, Mode.DEMO])
    catalog_ids: list[CatalogId] = Field(default_factory=lambda: ["CAT-A01-05"])
    training_cutoff: UtcDatetime | None = None
    """When the latest label it was trained or calibrated on became known (that label's
    `available_at`). `predict` refuses a snapshot with `as_of` at or before it, so it is
    never scored in-sample. None only for the baseline and models from before #127."""
    weights_hash: Sha256Ref | None = None
    """The weights file a shared adapter reads (`verified_weights`); None when the module
    holds the whole model."""
    training: TrainingRecord | None = None
    """Dataset, config hash, seeds, library versions and commit of a trained model (#130);
    None for the baseline and hand-written models."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.calibrated and not self.evidence_refs:
            raise ValueError("a calibrated claim needs evidence refs")
        if not set(self.feature_ranges) <= set(self.required_features):
            raise ValueError("feature_ranges may only cover required features")
        return self

    @property
    def ref(self) -> str:
        return subject_ref(self.model_id, self.model_version)

    @property
    def skill(self) -> Decimal:
        """Validation skill over the baseline: `baseline_brier - brier` (0 when either is
        missing or the model is no better). The card owns its metric names."""
        brier, base = self.validation.get(BRIER), self.validation.get(BASELINE_BRIER)
        if brier is None or base is None:
            return Decimal(0)
        return max(base - brier, Decimal(0))

    def manifest(self) -> StrategyManifest:
        """The registry entry for this model's artifact: the registration contract M07
        uses when it publishes an approved model (#48)."""
        if self.artifact_hash is None:
            raise ValueError("the baseline has no artifact to register")
        # An adapter's card is not inside the hashed module: pin the card itself.
        pins = [] if self.weights_hash is None else [CARD_PIN + self.content_hash]
        return StrategyManifest(
            strategy_id=self.model_id,
            version=self.model_version,
            artifact_hash=self.artifact_hash,
            family="model",
            catalog_ids=self.catalog_ids,
            technique_refs=[],
            timeframes=[self.horizon],
            warmup_bars=0,
            parameters={},
            risk_tags=[],
            supported_modes=self.supported_modes,
            dependencies=pins,
        )

    @property
    def content_hash(self) -> str:
        """sha256 of the card's canonical JSON."""
        return sha256_ref(canonical_json(self.model_dump(mode="json")))


def verified_weights(card: ModelCard, weights: bytes) -> bytes:
    """The weights, only if they are the file the card pins: an adapter calls this before
    parsing anything."""
    if card.weights_hash is None or sha256_ref(weights) != card.weights_hash:
        raise ValueError(f"{card.ref}: the weights are not the ones its card pins")
    return weights


class Predictor(Protocol):
    @property
    def card(self) -> ModelCard: ...

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        """Score per direction, each 0..1, in the card's stated semantics."""
        ...
