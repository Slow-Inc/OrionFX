"""Builders for the system-backtest tests (#131): straight-line bars, a strategy that always
proposes one side, and a constant three-class model loaded for evaluation. A support
module, not a test module. Fixture data, never market data: it proves nothing about edge.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tests.support.tracer import ADAPTER, ADAPTER_HASH

from app.core.bet import LABEL_VERSION
from app.core.contracts import CandleClosed, StrategyManifest, StrategyProposal, sha256_ref
from app.core.enums import Direction, ForecastLineage, IntentAction, Mode, Timeframe
from app.core.ids import new_id
from app.core.time import TIMEFRAME_DURATION
from app.decision.context import DecisionContext
from app.market_data.features import ATR_M15, FEATURE_SCHEMA
from app.market_data.store import CandleStore
from app.prediction.card import ModelCard
from app.prediction.engine import EvaluationPredictor, load_for_evaluation
from app.storage.artifacts import ArtifactLoader
from app.storage.db import transaction
from app.storage.registry import ArtifactRegistry

D = Decimal
M15 = Timeframe.M15
T0 = datetime(2026, 1, 5, tzinfo=UTC)
LATENCY = timedelta(seconds=1)


BASE, SPREAD = D(2000), D("0.20")


def line(n: int, *, step: Decimal, start: datetime = T0, base: Decimal = BASE,
         spread: Decimal | None = SPREAD) -> list[CandleClosed]:  # fmt: skip
    """M15 bars on a straight line: each closes `step` from its open, wicks 0.5 beyond
    both ends, so every true range is |step| + 1."""
    out, price, size = [], base, TIMEFRAME_DURATION[M15]
    for i in range(n):
        t, close = start + size * i, price + step
        out.append(CandleClosed(
            symbol="XAUUSD", timeframe=M15, open_time=t, close_time=t + size, open=price,
            high=max(price, close) + D("0.5"), low=min(price, close) - D("0.5"), close=close,
            volume=D(100), spread=spread, available_at=t + size + LATENCY,
        ))  # fmt: skip
        price = close
    return out


def no_trade(bar: CandleClosed, previous: CandleClosed) -> CandleClosed:
    """`bar` as #128's importer fills a minute with no trade: flat at the previous close,
    volume 0, flagged NO_TRADE."""
    c = previous.close
    return bar.model_copy(update={"open": c, "high": c, "low": c, "close": c, "volume": D(0),
                                  "quality_flags": ["NO_TRADE"]})  # fmt: skip


def store(conn: sqlite3.Connection, bars: list[CandleClosed], version: str = "bt-v1"
          ) -> CandleStore:  # fmt: skip
    candles = CandleStore(conn, dataset_version=version)
    with transaction(conn):
        for b in bars:
            candles.save(b)
    return candles


@dataclass(frozen=True)
class Always:
    """A test strategy: always proposes `direction` on M15 with the initial lot."""

    direction: Direction

    @property
    def manifest(self) -> StrategyManifest:
        sid = f"always_{self.direction.value.lower()}"
        return StrategyManifest(
            strategy_id=sid, version="1", artifact_hash=sha256_ref(sid.encode()), family="T",
            catalog_ids=["CAT-S01-01"], technique_refs=[], timeframes=[M15], warmup_bars=0,
            parameters={}, risk_tags=[], supported_modes=[Mode.SHADOW, Mode.DEMO],
        )  # fmt: skip

    def propose(self, ctx: DecisionContext) -> StrategyProposal | None:
        return StrategyProposal(
            proposal_id=new_id("prop"), strategy_id=self.manifest.strategy_id,
            strategy_version="1", technique_refs=[], composition_id=None, decision_id=None,
            direction=self.direction, action=IntentAction.OPEN, horizon=M15,
            expected_value_usd=None, cost_estimate_usd=None, requested_lot=ctx.initial_lot,
            valid_until=ctx.snapshot.valid_until, rationale="test", constraints=[],
        )  # fmt: skip


def constant_model(conn: sqlite3.Connection, *, favour: Direction, cutoff: datetime
                   ) -> EvaluationPredictor:  # fmt: skip
    """A calibrated barrier softmax that ignores its input: P(favour) = e^2 / (e^2 + 2)
    (about 0.79), the other barrier and FLAT about 0.11 each. Registered and loaded for
    evaluation, as a trained model would be."""
    up, down = ("2", "0") if favour is Direction.UP else ("0", "2")
    weights = json.dumps({"features": [ATR_M15], "intercept": [up, down, "0"],
                          "coef": [["0"], ["0"], ["0"]], "temperature": "1"}).encode()  # fmt: skip
    card = ModelCard(
        model_id=f"constant-{favour.value.lower()}", model_version="1",
        artifact_hash=ADAPTER_HASH, lineage=ForecastLineage.CHART_ONLY,
        feature_schema=FEATURE_SCHEMA, required_features=[ATR_M15], feature_ranges={},
        horizon=M15, horizon_bars=1, semantics="constant P(UP, DOWN, FLAT) of the label",
        calibrated=True, validation={}, evidence_refs=["test:constant"],
        label_version=LABEL_VERSION, training_cutoff=cutoff, weights_hash=sha256_ref(weights),
    )  # fmt: skip
    registry = ArtifactRegistry(conn)
    registry.register("model", card.manifest(), actor="m07")
    loader = ArtifactLoader(registry, allowed_packages=("app.prediction.adapters",))
    return load_for_evaluation(loader, registry, card=card, weights=weights, module=ADAPTER)
