"""M07 slice B: point-in-time similarity retrieval, semantic memory and RAG context (#60;
LRN-01, LRN-02, LRN-03; AT-15). Retrieval never sees an outcome that was not yet known,
and similarity is never presented as a probability of winning."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from tests.support.decision import T0, _ctx

from app.core.contracts import CandleClosed, DecisionCreated
from app.core.enums import DecisionAction, Direction, Mode, Timeframe
from app.learning.experience import RANGE_M15, TREND_M15, ExperienceLog
from app.learning.memory import SIMILARITY_SEMANTICS, rag_context, similar, summarize
from app.market_data.features import feature_key
from app.market_data.store import CandleStore
from app.storage.db import connect, migrate

D = Decimal
H1 = Timeframe.H1
RSI = feature_key(Timeframe.M5, "CAT-T01-04", "rsi")
ADX = feature_key(H1, "CAT-T01-07", "adx")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "mem.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> CandleStore:
    return CandleStore(conn, dataset_version="v1")


@pytest.fixture
def log(conn: sqlite3.Connection, store: CandleStore) -> ExperienceLog:
    return ExperienceLog(conn, store, clock=lambda: T0)


def _seen(log: ExperienceLog, store: CandleStore, did: str, at: datetime, rsi: str, *,
          trend: int = 1, strategy: str = "trend_following", close_then: str = "2000",
          close_after: str = "2005", lean: Direction = Direction.UP) -> None:  # fmt: skip
    """Record a WAIT at `at` with the given RSI, plus the two bars that label it."""
    values = {RSI: D(rsi), ADX: D(30), TREND_M15: D(trend), RANGE_M15: D(1 - trend)}
    snap = _ctx().snapshot.model_copy(update={
        "values": values, "as_of": at, "available_at": at, "valid_until": at + timedelta(minutes=5)
    })  # fmt: skip
    decision = DecisionCreated(
        decision_id=did, action=DecisionAction.WAIT, strategy_id=strategy, side=None,
        expected_value_usd=None, confidence=None, reason_codes=["EV_UNKNOWN"],
        evidence_refs=[], policy_version="p1",
    )  # fmt: skip
    log.record(decision=decision, snapshot=snap, mode=Mode.DEMO, symbol="XAUUSD",
               cycle_id=None, intent_id=None, direction=lean, horizon=H1,
               horizon_bars=1)  # fmt: skip
    for open_time, close in ((at - timedelta(hours=1), close_then), (at, close_after)):
        c = D(close)
        store.save(CandleClosed(
            symbol="XAUUSD", timeframe=H1, open_time=open_time,
            close_time=open_time + timedelta(hours=1), open=c, high=c, low=c, close=c,
            volume=None, available_at=open_time + timedelta(hours=1, seconds=1),
        ))  # fmt: skip


def _query(rsi: str, trend: int = 1) -> dict[str, Decimal | None]:
    from app.learning.experience import vector_of

    values = {RSI: D(rsi), ADX: D(30), TREND_M15: D(trend), RANGE_M15: D(1 - trend)}
    return vector_of(_ctx().snapshot.model_copy(update={"values": values}))


DAY = timedelta(days=1)


def test_nearest_experiences_come_first_with_their_outcomes(
    log: ExperienceLog, store: CandleStore
) -> None:
    _seen(log, store, "near", T0, "55")
    _seen(log, store, "far", T0 + 2 * DAY, "80", close_after="1990")
    log.label_due(now=T0 + 5 * DAY)
    matches = similar(log, _query("56"), regime="TREND", as_of=T0 + 5 * DAY, k=2)
    assert [m.decision_id for m in matches] == ["near", "far"]
    assert matches[0].similarity > matches[1].similarity
    assert matches[0].label.value == D(5) and matches[1].label.value == D(-10)
    assert all(m.semantics == SIMILARITY_SEMANTICS for m in matches)
    assert "not a probability" in SIMILARITY_SEMANTICS


def test_retrieval_never_sees_an_outcome_unknown_at_as_of(
    log: ExperienceLog, store: CandleStore
) -> None:
    _seen(log, store, "old", T0, "55")
    _seen(log, store, "recent", T0 + 2 * DAY, "55")
    log.label_due(now=T0 + 5 * DAY)
    # at T0 + 2 days + 30 min the "recent" horizon has not ended: its label is unknown
    at = T0 + 2 * DAY + timedelta(minutes=30)
    assert [m.decision_id for m in similar(log, _query("55"), regime="TREND", as_of=at, k=5)] == [
        "old"
    ]
    assert similar(log, _query("55"), regime="TREND", as_of=T0, k=5) == []


def test_only_the_same_regime_is_compared(log: ExperienceLog, store: CandleStore) -> None:
    _seen(log, store, "trend", T0, "55", trend=1)
    _seen(log, store, "range", T0 + DAY, "55", trend=0)
    log.label_due(now=T0 + 5 * DAY)
    got = similar(log, _query("55", trend=0), regime="RANGE", as_of=T0 + 5 * DAY, k=5)
    assert [m.decision_id for m in got] == ["range"]


def test_missing_dimensions_are_skipped_not_zeroed(log: ExperienceLog, store: CandleStore) -> None:
    _seen(log, store, "a", T0, "55")
    log.label_due(now=T0 + 5 * DAY)
    # the query has a direction the experience lacks: skipped, not counted as a 0 gap
    q = _query("55") | {feature_key(H1, "CAT-T03-07", "direction"): D(1)}
    (m,) = similar(log, q, regime="TREND", as_of=T0 + 5 * DAY, k=1)
    assert m.similarity == D(1)  # identical on every dimension both sides have
    assert m.dimensions == 2  # RSI and ADX; regime flags are not distance dimensions


def test_semantic_memory_summarises_per_strategy_and_regime(
    log: ExperienceLog, store: CandleStore
) -> None:
    _seen(log, store, "a", T0, "55", close_after="2004")
    _seen(log, store, "b", T0 + DAY, "60", close_after="1998")
    _seen(log, store, "c", T0 + 2 * DAY, "60", strategy="breakout")
    log.label_due(now=T0 + 5 * DAY)
    s = summarize(log, as_of=T0 + 5 * DAY)
    tf = s[("trend_following", "TREND")]
    assert tf.count == 2 and tf.moves == 2 and tf.mean_move == D(1)
    assert tf.moves_with_side == 1  # both leaned UP; one rose, one fell
    assert tf.realized == 0 and tf.mean_realized is None
    assert s[("breakout", "TREND")].count == 1
    assert summarize(log, as_of=T0) == {}  # nothing known yet


def test_rag_context_is_deterministic_cited_and_says_what_similarity_means(
    log: ExperienceLog, store: CandleStore
) -> None:
    _seen(log, store, "near", T0, "55")
    log.label_due(now=T0 + 5 * DAY)
    matches = similar(log, _query("56"), regime="TREND", as_of=T0 + 5 * DAY, k=3)
    ctx = rag_context(matches)
    assert ctx.refs == ["experience:near"]
    assert "experience:near" in ctx.text and SIMILARITY_SEMANTICS in ctx.text
    assert "probability of winning" not in ctx.text.replace(SIMILARITY_SEMANTICS, "")
    assert rag_context(matches) == ctx
    assert rag_context([]).text.startswith("No similar past experience")


def test_too_few_shared_dimensions_or_more_than_k_are_left_out(
    log: ExperienceLog, store: CandleStore
) -> None:
    _seen(log, store, "a", T0, "55")
    _seen(log, store, "b", T0 + DAY, "57")
    log.label_due(now=T0 + 5 * DAY)
    assert len(similar(log, _query("55"), regime="TREND", as_of=T0 + 5 * DAY, k=1)) == 1
    only_rsi = {RSI: D("0.55")}  # one shared dimension is not enough to compare
    assert similar(log, only_rsi, regime="TREND", as_of=T0 + 5 * DAY, k=5) == []


# --- review fixes ---


def test_regime_flags_give_no_free_similarity(log: ExperienceLog, store: CandleStore) -> None:
    _seen(log, store, "empty", T0, "55")
    log.label_due(now=T0 + 5 * DAY)
    regime_only = {TREND_M15: D(1), RANGE_M15: D(0)}  # nothing else in common
    assert similar(log, regime_only, regime="TREND", as_of=T0 + 5 * DAY, k=5) == []


def test_more_shared_features_are_not_penalised(log: ExperienceLog, store: CandleStore) -> None:
    # RMS distance: an equal per-feature gap gives equal similarity, however many features
    _seen(log, store, "a", T0, "55")
    log.label_due(now=T0 + 5 * DAY)
    two = {RSI: D("0.65"), ADX: D("0.40")}  # 0.10 off on each of 2 features
    one_more = {**two, feature_key(H1, "CAT-T03-07", "direction"): None}
    (m2,) = similar(log, two, regime="TREND", as_of=T0 + 5 * DAY, k=1)
    (m3,) = similar(log, one_more, regime="TREND", as_of=T0 + 5 * DAY, k=1)
    assert m2.similarity == m3.similarity == (1 / (1 + D("0.1"))).quantize(D("0.0001"))


def test_k_must_be_positive_and_as_of_must_be_aware(log: ExperienceLog) -> None:
    with pytest.raises(ValueError):
        similar(log, _query("55"), regime="TREND", as_of=T0, k=0)
    with pytest.raises(ValueError):
        similar(log, _query("55"), regime="TREND", as_of=T0.replace(tzinfo=None), k=1)


def test_the_context_judges_the_outcome_against_the_decisions_side(
    log: ExperienceLog, store: CandleStore
) -> None:
    _seen(log, store, "up_then_down", T0, "55", close_after="1990")  # leaned UP, fell 10
    log.label_due(now=T0 + 5 * DAY)
    text = rag_context(similar(log, _query("55"), regime="TREND", as_of=T0 + 5 * DAY, k=1)).text
    assert "leaning UP" in text and "went against the decision" in text and "price move" in text


def test_one_label_per_decision_is_enforced(conn: sqlite3.Connection, log: ExperienceLog,
                                            store: CandleStore) -> None:  # fmt: skip
    _seen(log, store, "a", T0, "55")
    log.label_due(now=T0 + 5 * DAY)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO learning_labels VALUES ('a', 'REALIZED', '1', 'UP', 't', '{}', 't')"
        )


def test_a_down_leaning_decision_is_right_when_the_market_falls(
    log: ExperienceLog, store: CandleStore
) -> None:
    _seen(log, store, "short", T0, "55", close_after="1990", lean=Direction.DOWN)
    log.label_due(now=T0 + 5 * DAY)
    text = rag_context(similar(log, _query("55"), regime="TREND", as_of=T0 + 5 * DAY, k=1)).text
    assert "went the decision's way" in text
    assert summarize(log, as_of=T0 + 5 * DAY)[("trend_following", "TREND")].moves_with_side == 1
