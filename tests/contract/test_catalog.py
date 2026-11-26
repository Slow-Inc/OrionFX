"""V1.1 catalog coverage matrix and strategy/technique contracts (#21; master plan
§15.5, §16.2-16.4; AT-37). The matrix is checked against the master plan table, so
a row can never be silently dropped, renamed, or reported as delivered without
evidence."""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.catalog import (
    CatalogClass,
    CoverageState,
    Eligibility,
    coverage_summary,
    load_coverage,
)
from app.core.contracts import (
    EligibilityRecord,
    ParameterBound,
    StrategyManifest,
    StrategyProposal,
    TechniqueObservation,
)
from app.core.enums import Mode

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)
ROW = re.compile(
    r"^\| (CAT-[A-Z]\d{2}-\d{2}) \| (.+?) \| (\S+) \| (\S+) \| ([RCDX]) \| (.*) \|$", re.MULTILINE
)


def _plan_rows() -> dict[str, tuple[str, str, str, str]]:
    text = (REPO / "docs/plans/master_planV1.1.md").read_text(encoding="utf-8")
    rows = {m[1]: (m[2].strip(), m[3], m[4], m[5]) for m in ROW.finditer(text)}
    assert len(rows) == len(ROW.findall(text)), "duplicate CAT id in the master plan"
    return rows


# --- AT-37: one disposition per catalog row ---------------------------------------------


def test_matrix_has_exactly_the_master_plan_rows() -> None:
    plan = _plan_rows()
    matrix = load_coverage()
    assert len(plan) == 201
    assert [e.catalog_id for e in matrix] == sorted(plan, key=list(plan).index)
    for e in matrix:
        capability, owner, phase, cls = plan[e.catalog_id]
        assert (e.capability, e.owner, e.phase, e.catalog_class) == (capability, owner, phase, cls)


def test_excluded_rows_are_excluded_and_nothing_else_is() -> None:
    for e in load_coverage():
        is_x = e.catalog_class is CatalogClass.X
        assert (e.coverage_state is CoverageState.EXCLUDED) == is_x, e.catalog_id


def test_nothing_is_certified_and_live_is_never_claimed() -> None:
    for e in load_coverage():
        assert e.coverage_state is not CoverageState.CERTIFIED, e.catalog_id
        assert e.eligibility is not Eligibility.LIVE_ELIGIBLE, e.catalog_id


def test_research_only_rows_never_allow_live() -> None:
    for e in load_coverage():
        if e.catalog_class is CatalogClass.D:
            assert Mode.LIVE not in e.allowed_modes, e.catalog_id


def _test_exists(ref: str) -> bool:
    path, _, name = ref.partition("::")
    file = REPO / path
    return file.is_file() and f"def {name}(" in file.read_text(encoding="utf-8")


def test_implemented_and_tested_rows_carry_checkable_evidence() -> None:
    for e in load_coverage():
        if e.coverage_state in (CoverageState.IMPLEMENTED, CoverageState.TESTED):
            assert e.implementation_refs, e.catalog_id
            for ref in e.implementation_refs:
                assert (REPO / ref).exists(), (e.catalog_id, ref)
        if e.coverage_state is CoverageState.TESTED:
            assert e.test_ids, e.catalog_id
        for ref in e.test_ids:
            assert _test_exists(ref), (e.catalog_id, ref)


def test_blocked_rows_name_blocker_owner_and_review_trigger() -> None:
    for e in load_coverage():
        if e.coverage_state is CoverageState.BLOCKED:
            assert e.blocker_reason and e.resolution_owner and e.review_trigger, e.catalog_id


def test_summary_never_counts_blocked_or_excluded_as_delivered() -> None:
    s = coverage_summary(load_coverage())
    assert s["total"] == 201
    assert sum(s["by_class"].values()) == 201
    assert sum(s["by_state"].values()) == 201
    assert s["delivered"] == s["by_state"].get("implemented", 0) + s["by_state"].get(
        "tested", 0
    ) + s["by_state"].get("certified", 0)
    assert s["delivered"] < 201  # no 100% coverage claim (§16.4)


# --- contracts (§15.5) ---------------------------------------------------------------------


def _manifest(**over: object) -> StrategyManifest:
    base: dict[str, object] = {
        "strategy_id": "trend_following",
        "version": "1.0.0",
        "artifact_hash": "sha256:" + "0" * 64,
        "family": "CAT-S01",
        "catalog_ids": ["CAT-S01-01"],
        "technique_refs": ["ema@1.0.0"],
        "timeframes": ["M5", "M15", "H1"],
        "warmup_bars": 200,
        "parameters": {"fast": ParameterBound(low=Decimal(5), high=Decimal(50))},
        "risk_tags": [],
        "supported_modes": [Mode.SHADOW, Mode.DEMO],
    }
    base.update(over)
    return StrategyManifest.model_validate(base)


def test_manifest_rejects_inverted_parameter_bounds() -> None:
    with pytest.raises(ValidationError):
        _manifest(parameters={"fast": {"low": "50", "high": "5"}})


def test_manifest_rejects_unknown_catalog_ids() -> None:
    with pytest.raises(ValidationError):
        _manifest(catalog_ids=["S01-01"])


def test_observation_must_expire_after_it_is_available() -> None:
    with pytest.raises(ValidationError):
        TechniqueObservation(
            technique_id="ema",
            technique_version="1.0.0",
            input_refs=["candle-1"],
            available_at=NOW,
            valid_until=NOW,
            values={"ema_20": Decimal("2000.1")},
            unit="USD",
            uncertainty=None,
            quality_flags=[],
            reason_codes=[],
        )


def test_proposal_is_not_a_broker_order() -> None:
    proposal = StrategyProposal(
        proposal_id="p-1",
        strategy_id="trend_following",
        strategy_version="1.0.0",
        technique_refs=["ema@1.0.0"],
        composition_id=None,
        decision_id="d-1",
        direction="UP",
        action="OPEN",
        horizon="M15",
        expected_value_usd=Decimal("0.5"),
        cost_estimate_usd=Decimal("0.2"),
        requested_lot=Decimal("0.01"),
        valid_until=NOW + timedelta(minutes=1),
        rationale="ema cross",
        constraints=[],
    )
    assert not hasattr(proposal, "order_id")


def test_live_eligibility_needs_evidence_and_policy() -> None:
    with pytest.raises(ValidationError):
        EligibilityRecord(
            subject_hash="sha256:" + "1" * 64,
            subject_kind="strategy",
            subject_ref="trend_following@1.0.0",
            user_enabled=True,
            eligibility=Eligibility.LIVE_ELIGIBLE,
            approved_modes=[Mode.LIVE],
            context={},
            evidence_refs=[],
            policy_version="p",
            expires_at=None,
            revoked_at=None,
        )
