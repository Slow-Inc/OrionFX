"""X02 slice C: evidence store, protected readiness policy and gate, live qualifications (#68;
VAL-04, VAL-05, VAL-06; AT-16, AT-24). Undecided means not ready; only a passing gate under
the current certified policy issues a Live qualification, for the subjects it tested."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.enums import Mode
from app.validation.evidence import (
    DEFAULT_POLICY,
    Criterion,
    EvidenceStore,
    ReadinessPolicy,
    ValidationRun,
    Verdict,
)
from app.validation.metrics import Metric

D = Decimal
T0 = datetime(2026, 1, 1, tzinfo=UTC)
DAY = timedelta(days=1)
MODEL = "model:xgb-dir@1"


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
         trades: int = 120, days: int = 40, start: datetime = T0,
         subject: str = MODEL) -> str:  # fmt: skip
    return store.record_run(ValidationRun(
        kind="replay", subject_refs=[subject], config={"cost": "spread 0.20, commission 3.5"},
        regimes=[regime], window_start=start, window_end=start + days * DAY, trades=trades,
        metrics={"expectancy": Metric(D(expectancy), trades),
                 "profit_factor": Metric(D(pf), trades)},
    ))  # fmt: skip


CERTIFIED = ReadinessPolicy(
    version="readiness-1", certified=True,
    criteria=[Criterion(metric="expectancy", at_least=D("0.50")),
              Criterion(metric="profit_factor", at_least=D("1.20"))],
    min_trades=200, min_days=60, required_regimes=["TREND", "RANGE"],
)  # fmt: skip


def _two(store: EvidenceStore) -> list[str]:
    return [_run(store, "TREND"), _run(store, "RANGE", start=T0 + 40 * DAY)]


# --- evidence ---------------------------------------------------------------------------------


def test_runs_are_stored_immutably_with_a_content_ref(
    conn: sqlite3.Connection, store: EvidenceStore
) -> None:
    ref = _run(store, "TREND")
    assert ref.startswith("x02:run:") and _run(store, "TREND") == ref  # same content, same ref
    run = store.run(ref)
    assert run is not None and run.metrics["expectancy"].value == D("1.20") and run.trades == 120
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE validation_runs SET body = '{}'")
    with pytest.raises(ValidationError):
        _run(store, "TREND", days=0)  # a window must end after it starts


# --- the gate ---------------------------------------------------------------------------------


def test_with_no_policy_nothing_is_ready(store: EvidenceStore) -> None:
    assert store.evaluate(_two(store)).reasons == ["NO_POLICY"]


def test_the_default_policy_is_uncertified_and_undecided_so_never_ready(
    store: EvidenceStore,
) -> None:
    store.save_policy(DEFAULT_POLICY)
    r = store.evaluate(_two(store))
    assert r.verdict is Verdict.NOT_READY and r.qualification_ref is None
    assert "POLICY_UNCERTIFIED" in r.reasons
    assert any(reason.startswith("THRESHOLD_TBD") for reason in r.reasons)
    assert "MIN_TRADES_TBD" in r.reasons and "MIN_DAYS_TBD" in r.reasons


def test_every_failed_criterion_is_reported(store: EvidenceStore) -> None:
    store.save_policy(CERTIFIED)
    r = store.evaluate([_run(store, "TREND", expectancy="0.10", trades=50, days=20)])
    assert r.verdict is Verdict.NOT_READY
    assert {"METRIC_BELOW:expectancy", "MIN_TRADES", "MIN_DAYS", "REGIME_MISSING:RANGE"} <= set(
        r.reasons
    )


def test_a_certified_passing_gate_issues_a_live_qualification_for_its_subjects(
    store: EvidenceStore,
) -> None:
    store.save_policy(CERTIFIED)
    r = store.evaluate(_two(store))
    assert r.verdict is Verdict.READY and r.reasons == []
    assert r.qualification_ref is not None
    assert r.qualification_ref.startswith("x02:live-qualification:")
    assert store.verify_live_qualification(r.qualification_ref, MODEL)
    assert not store.verify_live_qualification(r.qualification_ref, "model:other@1")
    assert not store.verify_live_qualification("x02:live-qualification:made-up", MODEL)


def test_unknown_or_duplicated_runs_are_refused(store: EvidenceStore) -> None:
    store.save_policy(CERTIFIED)
    assert "RUN_UNKNOWN:x02:run:nope" in store.evaluate(["x02:run:nope"]).reasons
    big = _run(store, "TREND", trades=120, days=70)
    rng = _run(store, "RANGE", start=T0 + 70 * DAY)
    r = store.evaluate([big, big, rng])  # 120 trades must not count twice
    assert f"RUN_DUPLICATE:{big}" in r.reasons and r.verdict is Verdict.NOT_READY


def test_coverage_counts_distinct_evidence_not_spans_or_repeats(store: EvidenceStore) -> None:
    store.save_policy(CERTIFIED)
    # a replay and a walk-forward of the same window: their trades count once
    same = [_run(store, "TREND", trades=120, days=70),
            _run(store, "RANGE", trades=120, days=70, expectancy="1.30")]  # fmt: skip
    assert "MIN_TRADES" in store.evaluate(same).reasons
    # two short runs a year apart do not cover the year between them
    apart = [_run(store, "TREND", trades=150, days=10),
             _run(store, "RANGE", trades=150, days=10, start=T0 + 300 * DAY)]  # fmt: skip
    assert "MIN_DAYS" in store.evaluate(apart).reasons


def test_policies_are_protected_and_a_new_version_voids_old_qualifications(
    store: EvidenceStore,
) -> None:
    store.save_policy(CERTIFIED)
    store.save_policy(CERTIFIED)  # identical: a no-op
    looser = CERTIFIED.model_copy(update={"min_trades": 10})
    with pytest.raises(ValueError, match="immutable"):
        store.save_policy(looser)  # same version, other content: refused
    ref = store.evaluate(_two(store)).qualification_ref
    assert ref is not None and store.verify_live_qualification(ref, MODEL)
    store.save_policy(looser.model_copy(update={"version": "readiness-2"}))
    assert not store.verify_live_qualification(ref, MODEL)  # its policy is no longer current


def test_a_certified_policy_must_have_criteria_and_regimes() -> None:
    with pytest.raises(ValidationError):
        ReadinessPolicy(version="r", certified=True, criteria=[], min_trades=1, min_days=0,
                        required_regimes=[])  # fmt: skip


def test_re_evaluating_does_not_duplicate_the_audit(
    conn: sqlite3.Connection, store: EvidenceStore
) -> None:
    store.save_policy(CERTIFIED)
    refs = _two(store)
    store.evaluate(refs)
    store.evaluate(refs)
    (n,) = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'validation.qualified'"
    ).fetchone()
    assert n == 1


# --- M07 link ---------------------------------------------------------------------------------


def test_m07_promotes_to_live_only_with_a_qualification_x02_issued_for_that_model(
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
        model_id="xgb-dir", model_version="1", artifact_hash="sha256:" + "e" * 64,
        lineage=ForecastLineage.CHART_ONLY, feature_schema="s", required_features=["k"],
        feature_ranges={}, horizon=Timeframe.H1, horizon_bars=1, semantics="s",
        calibrated=False, validation={}, evidence_refs=[],
        supported_modes=[Mode.SHADOW, Mode.DEMO, Mode.LIVE],
    )  # fmt: skip
    life.propose_candidate(card, dataset_refs=["d@1"])
    args = {"modes": [Mode.LIVE], "evidence_refs": ["x02:run:r"], "policy_ref": "p",
            "approved_by": "owner", "approval_ref": "a"}  # fmt: skip
    with pytest.raises(LifecycleRefused, match="not issued"):
        life.promote("xgb-dir", "1", live_qualification_ref="x02:live-qualification:typed",
                     **args)  # type: ignore[arg-type]  # fmt: skip
    store.save_policy(CERTIFIED)
    real = store.evaluate(_two(store)).qualification_ref
    life.promote("xgb-dir", "1", live_qualification_ref=real, **args)  # type: ignore[arg-type]
    assert life.champion("xgb-dir") == "1"
