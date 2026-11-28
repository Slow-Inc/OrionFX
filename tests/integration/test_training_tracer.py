"""The training-loop tracer (#127, PRD #120): a trivial model runs the whole chain on a
fixture with a daily break and a weekend.

bars -> CandleStore -> MarketRecorder BACKFILL (session=) -> BarLog labels -> export_dataset
(one walk-forward fold plus a sealed holdout) -> train on the fold's train window and
calibrate on its test window -> native artifact + card with a training cutoff -> register
-> evaluation load -> model-backed evidence -> replay(session=) on the holdout ->
EvidenceStore.record_run and evaluate.

Stubs and shortcuts, each deliberate:
- The import link is stubbed until #128: bars are written to the CandleStore directly.
- The fixture's library is the default one cut to the techniques the chain reads
  (`tracer.TECHNIQUES`), and M5 is stored only from the train split on, while M15/H1
  history starts a week earlier, so H1 ADX (85 bars) is warm. Recording every M5 bar of
  that week with the full library cost ~45 s.
- The negative control runs the same chain on a few hours of M5 around the daily break.
- The trainer is a three-class softmax on two M04 transforms, fitted to the
  triple-barrier label (`tracer.train`); #130 brings the real one behind the same adapter.
"""

import functools
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.support import tracer
from tests.support.tracer import H1, M5, M15

from app.core.bet import LABEL_VERSION
from app.core.contracts import CandleClosed, EligibilityRecord, ParameterBound, sha256_ref
from app.core.enums import DecisionAction, Eligibility, ForecastLineage, Mode
from app.decision.strategies import ADX_H1, STRATEGIES, STRUCT_M15
from app.learning.bars import BarLog, Horizon, Provenance
from app.learning.export import export_dataset, horizon_key
from app.learning.lifecycle import DatasetStore, ModelLifecycle, Split
from app.market_data.session import XAUUSD_NY_1700, SessionSchedule
from app.market_data.sources import SourceRegistry
from app.market_data.store import CandleStore
from app.prediction.card import ModelCard
from app.prediction.engine import load_for_evaluation
from app.prediction.transforms import TRANSFORM_SCHEMA
from app.runtime.market_recorder import MarketRecorder
from app.storage.artifacts import ArtifactLoader
from app.storage.db import connect, migrate, transaction
from app.storage.registry import ArtifactRegistry, subject_ref
from app.validation.evidence import DEFAULT_POLICY, EvidenceStore, ValidationRun, Verdict
from app.validation.replay import CostModel, EvaluationEvidence, ReplayResult, replay

D = Decimal
U = UTC
HISTORY_FROM = datetime(2026, 1, 4, 23, tzinfo=U)  # Sunday 18:00 New York: the week opens
EMBARGO = timedelta(hours=1)
"""At least the longest horizon (export rule (a))."""
TRAIN = Split(name="train", start=datetime(2026, 1, 9, 8, tzinfo=U),
              end=datetime(2026, 1, 12, 5, tzinfo=U))  # spans the weekend  # fmt: skip
VALIDATION = Split(name="validation", start=TRAIN.end + EMBARGO,
                   end=datetime(2026, 1, 12, 20, tzinfo=U))  # fmt: skip
"""The one fold's test window: the calibration slice."""
TEST = Split(name="holdout", start=VALIDATION.end + EMBARGO,
             end=datetime(2026, 1, 13, 14, tzinfo=U))  # fmt: skip
"""The sealed holdout, replayed as the test fold: it spans the daily break of Monday
17:00-18:00 New York."""
END = TEST.end
WEEKEND_REOPEN = datetime(2026, 1, 11, 23, tzinfo=U)
BREAK_REOPEN = datetime(2026, 1, 12, 23, tzinfo=U)
HORIZONS = [Horizon(M15, 1), Horizon(H1, 1)]
LABEL = Horizon(H1, 1)
"""The model's horizon: `trend_following` proposes on H1, and EV needs the two to match."""
STRATEGY = STRATEGIES["trend_following"]
STRATEGY_REF = subject_ref(STRATEGY.manifest.strategy_id, STRATEGY.manifest.version)
COST = CostModel(contract_size=D(100), commission_per_lot=D("3.5"), slippage=D("0.05"),
                 default_spread=D("0.20"))  # fmt: skip


@functools.cache
def _fixture() -> tuple[list[CandleClosed], list[CandleClosed]]:
    """(M5, then M15 and H1): seeded, so every chain gets the same bars."""
    m5 = tracer.m5_bars(XAUUSD_NY_1700, start=HISTORY_FROM, end=END)
    return m5, [*tracer.resample(m5, M15), *tracer.resample(m5, H1)]


def _candles(conn: sqlite3.Connection, m5: tuple[datetime, datetime]) -> CandleStore:
    """The import link, stubbed until #128: resampled bars written straight to the store,
    M5 only inside the `m5` window."""
    bars, context = _fixture()
    store = CandleStore(conn, dataset_version="tracer-v1")
    with transaction(conn):
        for b in [*(b for b in bars if m5[0] <= b.open_time < m5[1]), *context]:
            store.save(b)
    return store


def _record(conn: sqlite3.Connection, candles: CandleStore, session: SessionSchedule | None
            ) -> BarLog:  # fmt: skip
    MarketRecorder(conn, candles=candles, symbol="XAUUSD", timeframe=M5,
                   context_timeframes=[M15, H1], horizons=HORIZONS,
                   provenance=Provenance.BACKFILL, clock=lambda: END + timedelta(hours=2),
                   session=session, techniques=tracer.TECHNIQUES).tick()  # fmt: skip
    return BarLog(conn)


def _assert_features_usable(log: BarLog) -> None:
    """Every H1/M15 feature the chain reads is set on every non-stale bar of the test fold,
    and on the first bars after the weekend and the daily break have reopened (H1 ADX
    reads 85 H1 bars and M15 structure 100 M15 bars: both windows span a closure)."""
    keys = [k for k in tracer.CHAIN_KEYS if k.startswith(("H1.", "M15."))]
    rows = log.snapshots("XAUUSD", M5, start=WEEKEND_REOPEN, end=END)
    test = [r.snapshot for r in rows
            if r.snapshot.as_of > TEST.start and r.snapshot.quality.value != "STALE"]  # fmt: skip
    after = [next(r.snapshot for r in rows if r.snapshot.as_of >= t + timedelta(hours=1))
             for t in (WEEKEND_REOPEN, BREAK_REOPEN)]  # fmt: skip
    unusable = {s.as_of: k for s in [*test, *after] for k in keys if s.values.get(k) is None}
    assert test and not unusable, f"H1/M15 features unusable: {sorted(unusable.items())[:3]}"


def _export(conn: sqlite3.Connection, out: Path, session: SessionSchedule | None
            ) -> list[dict[str, Any]]:  # fmt: skip
    # A version recorded outside `import_history` is bound to its source before export.
    SourceRegistry(conn).register("tracer-v1", "dukascopy")
    manifest = export_dataset(conn, symbol="XAUUSD", timeframe=M5, horizons=HORIZONS,
                              dataset_versions=["tracer-v1"], window_start=TRAIN.start,
                              window_end=END, train=TRAIN.end - TRAIN.start,
                              test=VALIDATION.end - VALIDATION.start, embargo=EMBARGO,
                              holdout=TEST.end - TEST.start, dataset_id="tracer",
                              version="1", out=out, transforms=True,
                              session=session)  # fmt: skip
    assert [(f.train_start, f.train_end, f.test_start, f.test_end) for f in manifest.folds] == [
        (TRAIN.start, TRAIN.end, VALIDATION.start, VALIDATION.end)
    ]
    assert (manifest.splits[-1].start, manifest.splits[-1].end) == (TEST.start, TEST.end)
    data = out.read_bytes()
    assert manifest.content_hash == sha256_ref(data)
    return [json.loads(line) for line in data.decode("utf-8").splitlines()]


def _artifact(rows: list[dict[str, Any]], folder: Path) -> None:
    """Train, then write the native artifact: a card and a JSON weights file."""
    weights, stated = tracer.train(rows, horizon_key(LABEL))
    card = ModelCard(
        model_id="tracer-softmax", model_version="1",
        artifact_hash=tracer.ADAPTER_HASH,
        lineage=ForecastLineage.CHART_ONLY, feature_schema=TRANSFORM_SCHEMA,
        required_features=tracer.MODEL_FEATURES,
        feature_ranges={k: ParameterBound(low=D(-20), high=D(20)) for k in tracer.MODEL_FEATURES},
        horizon=LABEL.timeframe, horizon_bars=LABEL.bars,
        semantics="P(UP, DOWN, FLAT) of the triple-barrier label over 1 H1 bar,"
                  " temperature-scaled on the validation split",
        calibrated=True, validation=stated["validation"],
        evidence_refs=["dataset:tracer@1#validation"], label_version=LABEL_VERSION,
        training_cutoff=stated["training_cutoff"], weights_hash=sha256_ref(weights),
    )  # fmt: skip
    (folder / "card.json").write_text(card.model_dump_json(), encoding="utf-8")
    (folder / "weights.json").write_bytes(weights)


def _approve_strategy(conn: sqlite3.Connection) -> ArtifactRegistry:
    """`trend_following` is approved for SHADOW before the fixture starts."""
    reg = ArtifactRegistry(conn, clock=lambda: HISTORY_FROM - timedelta(days=1))
    m = STRATEGY.manifest
    reg.register("strategy", m, actor="m07")
    reg.set_eligibility(EligibilityRecord(
        subject_hash=m.artifact_hash, subject_kind="strategy",
        subject_ref=STRATEGY_REF, user_enabled=True,
        eligibility=Eligibility.SHADOW_ONLY, approved_modes=[Mode.SHADOW], context={},
        evidence_refs=["tracer"], policy_version="tracer", expires_at=None, revoked_at=None,
    ), actor="owner")  # fmt: skip
    return reg


@dataclass
class Chain:
    log: BarLog
    rows: list[dict[str, Any]]
    card: ModelCard
    result: ReplayResult
    readiness: Any


def _chain(conn: sqlite3.Connection, folder: Path, session: SessionSchedule | None,
           m5: tuple[datetime, datetime] = (TRAIN.start, END)) -> Chain:  # fmt: skip
    candles = _candles(conn, m5)
    log = _record(conn, candles, session)
    _assert_features_usable(log)
    rows = _export(conn, folder / "dataset.jsonl", session)
    _artifact(rows, folder)
    strategies = _approve_strategy(conn)
    # Registered today (the real clock), long after the fixture: only an evaluation load
    # can score it on that history.
    registry = ArtifactRegistry(conn)
    card = ModelCard.model_validate_json((folder / "card.json").read_text(encoding="utf-8"))
    lifecycle = ModelLifecycle(conn, registry, DatasetStore(conn))
    lifecycle.propose_candidate(card, dataset_refs=["tracer@1"])
    model = load_for_evaluation(
        ArtifactLoader(registry, allowed_packages=("app.prediction.adapters",)), registry,
        card=card, weights=(folder / "weights.json").read_bytes(),
        module=tracer.ADAPTER)  # fmt: skip
    result = replay(candles, symbol="XAUUSD", timeframe=M5, context_timeframes=[M15, H1],
                    start=TEST.start, end=TEST.end, strategies=[STRATEGY],
                    enabled=[STRATEGY.manifest.strategy_id], registry=strategies,
                    initial_lot=D("0.01"), cost=COST,
                    evidence=EvaluationEvidence(model, candles, session),
                    session=session, techniques=tracer.TECHNIQUES)  # fmt: skip
    store = EvidenceStore(conn)
    store.save_policy(DEFAULT_POLICY)
    ref = store.record_run(ValidationRun(
        kind="replay", subject_refs=[f"model:{card.ref}", f"strategy:{STRATEGY_REF}"],
        config={"dataset": "tracer@1", "split": TEST.name, "session": str(session)},
        regimes=["TREND"], window_start=TEST.start, window_end=TEST.end,
        trades=len(result.trades), metrics=result.metrics,
    ))  # fmt: skip
    return Chain(log, rows, card, result, store.evaluate([ref]))


@pytest.fixture(scope="module")
def chain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Chain]:
    folder = tmp_path_factory.mktemp("tracer")
    conn = connect(folder / "tracer.db")
    migrate(conn)
    yield _chain(conn, folder, XAUUSD_NY_1700)
    conn.close()


def test_every_bar_is_labelled_or_marked_unlabellable_at_a_closure(chain: Chain) -> None:
    recorded = [r.snapshot.as_of for r in chain.log.snapshots("XAUUSD", M5, start=TRAIN.start,
                                                              end=END)]  # fmt: skip
    for h in HORIZONS:
        due = [t for t in recorded if t + h.length <= END - timedelta(hours=1)]
        labelled = {o.as_of for o in chain.log.outcomes("XAUUSD", h, start=TRAIN.start,
                                                        end=END)}  # fmt: skip
        missing = set(chain.log.unlabellable("XAUUSD", h, dataset_version="tracer-v1"))
        closed = {t for t in due if not XAUUSD_NY_1700.expects(t + h.length - timedelta(
            minutes=5), timedelta(minutes=5))}  # fmt: skip
        assert {t for t in due if t not in labelled} == closed == missing & set(due), h
        assert closed  # the fixture does cross closures


def test_the_card_is_trained_before_the_test_fold_and_calibrated_on_later_data(
    chain: Chain,
) -> None:
    card = chain.card
    assert card.training_cutoff is not None
    assert VALIDATION.end - timedelta(hours=1) < card.training_cutoff < TEST.start
    assert card.calibrated and card.skill > 0
    assert {r["split"] for r in chain.rows} == {"development", "holdout"}


def test_the_session_aware_replay_lets_the_strategy_propose(chain: Chain) -> None:
    """Replay builds its snapshots with the session too: `trend_following` (H1 ADX, M15
    structure) gets past the chart gates on some bars of the test fold."""
    chart_gates = {"NO_OPPORTUNITY", "CONFLICT_STRATEGIES", "STALE_INPUT"}
    assert any(not chart_gates & set(d.reason_codes) for d in chain.result.decisions)


def test_the_model_drives_at_least_one_entry_in_the_test_fold(chain: Chain) -> None:
    enters = [d for d in chain.result.decisions if d.action is DecisionAction.ENTER]
    assert enters, sorted({c for d in chain.result.decisions for c in d.reason_codes})
    assert all(d.confidence is not None and d.confidence.calibrated for d in enters)
    assert all(d.expected_value_usd is not None and d.expected_value_usd > 0 for d in enters)
    assert chain.result.trades


def test_readiness_stays_not_ready_under_the_uncertified_default_policy(chain: Chain) -> None:
    assert chain.readiness.verdict is Verdict.NOT_READY
    assert "POLICY_UNCERTIFIED" in chain.readiness.reasons


def test_without_the_session_the_long_h1_and_m15_features_never_come_back(
    tmp_path: Path,
) -> None:
    """Negative control: the same chain with session=None fails its feature assertion,
    while the same small window with the session passes it, so the pass-through of
    `session=` alone is what makes the features usable."""
    window = (TEST.start, BREAK_REOPEN + timedelta(hours=2))
    with_session = connect(tmp_path / "with-session.db")
    migrate(with_session)
    try:
        _assert_features_usable(_record(with_session, _candles(with_session, window),
                                        XAUUSD_NY_1700))  # fmt: skip
    finally:
        with_session.close()
    conn = connect(tmp_path / "control.db")
    migrate(conn)
    try:
        with pytest.raises(AssertionError, match="H1/M15 features unusable"):
            _chain(conn, tmp_path, None, m5=window)
        values = [r.snapshot.values for r in BarLog(conn).snapshots("XAUUSD", M5, start=TEST.start,
                                                                     end=END)]  # fmt: skip
        assert values and all(v[ADX_H1] is None and v[STRUCT_M15] is None for v in values)
    finally:
        conn.close()
