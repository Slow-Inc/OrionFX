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
def log(conn: sqlite3.Connection) -> ExperienceLog:
    return ExperienceLog(conn, CandleStore(conn, dataset_version="v1"), clock=lambda: T0)


def _seen(log: ExperienceLog, did: str, at: datetime, rsi: str, *, trend: int = 1,
          strategy: str = "trend_following", close_then: str = "2000",
          close_after: str = "2005") -> None:  # fmt: skip
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
               cycle_id=None, intent_id=None, direction=Direction.UP, horizon=H1,
               horizon_bars=1)  # fmt: skip
    store = log.candles
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


def test_nearest_experiences_come_first_with_their_outcomes(log: ExperienceLog) -> None:
    _seen(log, "near", T0, "55")
    _seen(log, "far", T0 + 2 * DAY, "80", close_after="1990")
    log.label_due(now=T0 + 5 * DAY)
    matches = similar(log, _query("56"), regime="TREND", as_of=T0 + 5 * DAY, k=2)
    assert [m.decision_id for m in matches] == ["near", "far"]
    assert matches[0].similarity > matches[1].similarity
    assert matches[0].label.value == D(5) and matches[1].label.value == D(-10)
    assert all(m.semantics == SIMILARITY_SEMANTICS for m in matches)
    assert "not a probability" in SIMILARITY_SEMANTICS


def test_retrieval_never_sees_an_outcome_unknown_at_as_of(log: ExperienceLog) -> None:
    _seen(log, "old", T0, "55")
    _seen(log, "recent", T0 + 2 * DAY, "55")
    log.label_due(now=T0 + 5 * DAY)
    # at T0 + 2 days + 30 min the "recent" horizon has not ended: its label is unknown
    at = T0 + 2 * DAY + timedelta(minutes=30)
    assert [m.decision_id for m in similar(log, _query("55"), regime="TREND", as_of=at, k=5)] == [
        "old"
    ]
    assert similar(log, _query("55"), regime="TREND", as_of=T0, k=5) == []


def test_only_the_same_regime_is_compared(log: ExperienceLog) -> None:
    _seen(log, "trend", T0, "55", trend=1)
    _seen(log, "range", T0 + DAY, "55", trend=0)
    log.label_due(now=T0 + 5 * DAY)
    got = similar(log, _query("55", trend=0), regime="RANGE", as_of=T0 + 5 * DAY, k=5)
    assert [m.decision_id for m in got] == ["range"]


def test_missing_dimensions_are_skipped_not_zeroed(log: ExperienceLog) -> None:
    _seen(log, "a", T0, "55")
    log.label_due(now=T0 + 5 * DAY)
    q = _query("55") | {ADX: None}
    (m,) = similar(log, q, regime="TREND", as_of=T0 + 5 * DAY, k=1)
    assert m.similarity == D(1)  # identical on every dimension both sides have
    assert m.dimensions == 3


def test_semantic_memory_summarises_per_strategy_and_regime(log: ExperienceLog) -> None:
    _seen(log, "a", T0, "55", close_after="2004")
    _seen(log, "b", T0 + DAY, "60", close_after="1998")
    _seen(log, "c", T0 + 2 * DAY, "60", strategy="breakout")
    log.label_due(now=T0 + 5 * DAY)
    s = summarize(log, as_of=T0 + 5 * DAY)
    tf = s[("trend_following", "TREND")]
    assert tf.count == 2 and tf.up == 1 and tf.down == 1 and tf.mean_move == D(1)
    assert s[("breakout", "TREND")].count == 1
    assert summarize(log, as_of=T0) == {}  # nothing known yet


def test_rag_context_is_deterministic_cited_and_says_what_similarity_means(
    log: ExperienceLog,
) -> None:
    _seen(log, "near", T0, "55")
    log.label_due(now=T0 + 5 * DAY)
    matches = similar(log, _query("56"), regime="TREND", as_of=T0 + 5 * DAY, k=3)
    ctx = rag_context(matches)
    assert ctx.refs == ["experience:near"]
    assert "experience:near" in ctx.text and SIMILARITY_SEMANTICS in ctx.text
    assert "probability of winning" not in ctx.text.replace(SIMILARITY_SEMANTICS, "")
    assert rag_context(matches) == ctx
    assert rag_context([]).text.startswith("No similar past experience")


def test_too_few_shared_dimensions_or_more_than_k_are_left_out(log: ExperienceLog) -> None:
    _seen(log, "a", T0, "55")
    _seen(log, "b", T0 + DAY, "57")
    log.label_due(now=T0 + 5 * DAY)
    assert len(similar(log, _query("55"), regime="TREND", as_of=T0 + 5 * DAY, k=1)) == 1
    only_rsi = {RSI: D("0.55")}  # one shared dimension is not enough to compare
    assert similar(log, only_rsi, regime="TREND", as_of=T0 + 5 * DAY, k=5) == []
