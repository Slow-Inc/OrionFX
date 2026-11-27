"""X02 slice C: evidence store, protected readiness policy and gate, live-qualification refs
(#68; VAL-04, VAL-05, VAL-06; AT-16, AT-24). Undecided means not ready; only a passing gate
under a certified policy issues a Live qualification, and M07 checks it exists."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.enums import Mode
from app.validation.evidence import (
    DEFAULT_POLICY,
    Criterion,
    EvidenceStore,
    ReadinessPolicy,
    Verdict,
    evaluate,
)
from app.validation.metrics import Metric

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    from app.storage.db import connect, migrate

    c = connect(tmp_path / "x02.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def store(conn: sqlite3.Connection) -> EvidenceStore:
    return EvidenceStore(conn, clock=lambda: T0 + 100 * DAY)


def _run(store: EvidenceStore, regime: str, *, expectancy: str = "1.20", pf: str = "1.5",
         trades: int = 120, days: int = 40, start: datetime = T0) -> str:  # fmt: skip
    return store.record_run(
        kind="replay", subject_refs=["model:xgb-dir@1", "strategy:trend_following@1.0.0"],
        config={"cost": "spread 0.20, commission 3.5"}, regimes=[regime],
        window_start=start, window_end=start + days * DAY,
        metrics={"expectancy": Metric(D(expectancy), trades),
                 "profit_factor": Metric(D(pf), trades)},
    )  # fmt: skip


CERTIFIED = ReadinessPolicy(
    version="readiness-1", certified=True,
    criteria=[Criterion(metric="expectancy", at_least=D("0.50")),
              Criterion(metric="profit_factor", at_least=D("1.20"))],
    min_trades=200, min_days=60, required_regimes=["TREND", "RANGE"],
)  # fmt: skip


# --- evidence ---------------------------------------------------------------------------------


def test_runs_are_stored_immutably_with_a_content_ref(
    conn: sqlite3.Connection, store: EvidenceStore
) -> None:
    ref = _run(store, "TREND")
    assert ref.startswith("x02:run:") and _run(store, "TREND") == ref  # same content, same ref
    run = store.run(ref)
    assert run is not None and run.metrics["expectancy"].value == D("1.20")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE validation_runs SET body = '{}'")


# --- the gate ---------------------------------------------------------------------------------


def test_the_default_policy_is_uncertified_and_undecided_so_never_ready(
    store: EvidenceStore,
) -> None:
    refs = [_run(store, "TREND"), _run(store, "RANGE")]
    store.save_policy(DEFAULT_POLICY)
    r = evaluate(store, DEFAULT_POLICY.version, refs)
    assert r.verdict is Verdict.NOT_READY and r.qualification_ref is None
    assert "POLICY_UNCERTIFIED" in r.reasons
    assert any(reason.startswith("THRESHOLD_TBD") for reason in r.reasons)
    assert "MIN_TRADES_TBD" in r.reasons and "MIN_DAYS_TBD" in r.reasons


def test_every_failed_criterion_is_reported(store: EvidenceStore) -> None:
    store.save_policy(CERTIFIED)
    refs = [_run(store, "TREND", expectancy="0.10", trades=50, days=20)]
    r = evaluate(store, CERTIFIED.version, refs)
    assert r.verdict is Verdict.NOT_READY
    assert {"METRIC_BELOW:expectancy", "MIN_TRADES", "MIN_DAYS", "REGIME_MISSING:RANGE"} <= set(
        r.reasons
    )


def test_a_certified_passing_gate_issues_a_live_qualification(store: EvidenceStore) -> None:
    store.save_policy(CERTIFIED)
    refs = [_run(store, "TREND"), _run(store, "RANGE", start=T0 + 40 * DAY)]
    r = evaluate(store, CERTIFIED.version, refs)
    assert r.verdict is Verdict.READY and r.reasons == []
    assert r.qualification_ref is not None
    assert r.qualification_ref.startswith("x02:live-qualification:")
    assert store.verify_live_qualification(r.qualification_ref)
    assert not store.verify_live_qualification("x02:live-qualification:made-up")


def test_unknown_runs_or_policies_are_refused(store: EvidenceStore) -> None:
    store.save_policy(CERTIFIED)
    r = evaluate(store, CERTIFIED.version, ["x02:run:nope"])
    assert "RUN_UNKNOWN:x02:run:nope" in r.reasons and r.verdict is Verdict.NOT_READY
    with pytest.raises(KeyError):
        evaluate(store, "no-such-policy", [])


def test_policies_are_protected_and_a_new_version_invalidates_old_qualifications(
    store: EvidenceStore,
) -> None:
    store.save_policy(CERTIFIED)
    store.save_policy(CERTIFIED)  # identical: a no-op
    looser = CERTIFIED.model_copy(update={"min_trades": 10})
    with pytest.raises(ValueError, match="immutable"):
        store.save_policy(looser)  # same version, other content: refused
    refs = [_run(store, "TREND"), _run(store, "RANGE", start=T0 + 40 * DAY)]
    ref = evaluate(store, CERTIFIED.version, refs).qualification_ref
    assert ref is not None and store.verify_live_qualification(ref)
    store.save_policy(looser.model_copy(update={"version": "readiness-2"}))
    assert store.current_policy_version() == "readiness-2"
    assert not store.verify_live_qualification(ref)  # earned under a policy no longer current


# --- M07 link ---------------------------------------------------------------------------------


def test_m07_refuses_a_live_ref_the_evidence_store_never_issued(
    conn: sqlite3.Connection, store: EvidenceStore
) -> None:
    from app.core.enums import ForecastLineage, Timeframe
    from app.learning.lifecycle import (
        DatasetManifest,
        DatasetStore,
        LifecycleRefused,
        ModelLifecycle,
        Split,
    )
    from app.prediction.card import ModelCard
    from app.storage.registry import ArtifactRegistry

    life = ModelLifecycle(conn, ArtifactRegistry(conn), DatasetStore(conn),
                          live_verifier=store.verify_live_qualification)  # fmt: skip
    life.datasets.save(DatasetManifest(
        dataset_id="d", version="1", source_refs=[], window_start=T0, window_end=T0 + 9 * DAY,
        splits=[Split(name="train", start=T0, end=T0 + 5 * DAY),
                Split(name="validation", start=T0 + 5 * DAY, end=T0 + 9 * DAY)],
        content_hash="sha256:" + "d" * 64,
    ))  # fmt: skip
    card = ModelCard(
        model_id="m", model_version="1", artifact_hash="sha256:" + "e" * 64,
        lineage=ForecastLineage.CHART_ONLY, feature_schema="s", required_features=["k"],
        feature_ranges={}, horizon=Timeframe.H1, horizon_bars=1, semantics="s",
        calibrated=False, validation={}, evidence_refs=[],
        supported_modes=[Mode.SHADOW, Mode.DEMO, Mode.LIVE],
    )  # fmt: skip
    life.propose_candidate(card, dataset_refs=["d@1"])
    args = {"modes": [Mode.LIVE], "evidence_refs": ["x02:run:r"], "policy_ref": "p",
            "approved_by": "owner", "approval_ref": "a"}  # fmt: skip
    with pytest.raises(LifecycleRefused, match="not issued"):
        life.promote("m", "1", live_qualification_ref="x02:live-qualification:typed", **args)  # type: ignore[arg-type]
    store.save_policy(CERTIFIED)
    refs = [_run(store, "TREND"), _run(store, "RANGE", start=T0 + 40 * DAY)]
    real = evaluate(store, CERTIFIED.version, refs).qualification_ref
    life.promote("m", "1", live_qualification_ref=real, **args)  # type: ignore[arg-type]
    assert life.champion("m") == "1"
