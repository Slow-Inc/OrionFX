"""Model card and predictor port (#46; PRE-02, PRE-04, UST-02).

A card is what a predictor promises: which features it reads (and the ranges it
was validated on), its horizon, what its score means and whether it is
calibrated (only with evidence), its validation numbers, and the artifact hash
the registry must hold. The built-in baseline has no artifact (hash None).
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
)
from app.core.enums import Direction, ForecastLineage, Mode, Timeframe
from app.storage.registry import subject_ref

BRIER = "brier"
BASELINE_BRIER = "baseline_brier"


def unique_leader(scores: Mapping[Direction, Decimal]) -> tuple[Direction, Decimal] | None:
    """The direction with the strictly highest score, or None on a tie or no scores."""
    if not scores:
        return None
    top = max(scores.values())
    leaders = [d for d, s in scores.items() if s == top]
    return (leaders[0], top) if len(leaders) == 1 else None


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
    supported_modes: list[Mode] = Field(default_factory=lambda: [Mode.SHADOW, Mode.DEMO])
    catalog_ids: list[CatalogId] = Field(default_factory=lambda: ["CAT-A01-05"])

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
        )


class Predictor(Protocol):
    @property
    def card(self) -> ModelCard: ...

    def predict(self, features: dict[str, Decimal]) -> Mapping[Direction, Decimal]:
        """Score per direction, each 0..1, in the card's stated semantics."""
        ...
