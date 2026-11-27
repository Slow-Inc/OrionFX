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
    DELIVERED,
    FACTS,
    PLAN_PATH,
    CoverageEntry,
    coverage_summary,
    load_coverage,
    parse_plan_rows,
)
from app.core.contracts import (
    EligibilityRecord,
    ParameterBound,
    StrategyManifest,
    StrategyProposal,
    TechniqueObservation,
)
from app.core.enums import Eligibility, Mode

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 1, tzinfo=UTC)
PLAN = parse_plan_rows(PLAN_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def matrix() -> list[CoverageEntry]:
    return load_coverage()  # validation runs every per-row rule (CoverageEntry._consistent)


# --- AT-37: one disposition per catalog row ---------------------------------------------


def test_plan_has_201_unique_rows() -> None:
    ids = [r["catalog_id"] for r in PLAN]
    assert len(ids) == 201 and len(set(ids)) == 201


def test_matrix_mirrors_the_master_plan_rows(matrix: list[CoverageEntry]) -> None:
    assert [{k: str(getattr(e, k)) for k in FACTS} for e in matrix] == PLAN


def _test_exists(ref: str) -> bool:
    path, _, name = ref.partition("::")
    file = REPO / path
    if not (path.startswith("tests/") and file.is_file()):
        return False
    pattern = rf"^def {re.escape(name)}\("
    return re.search(pattern, file.read_text(encoding="utf-8"), re.MULTILINE) is not None


def _source_file(ref: str) -> bool:
    file = (REPO / ref).resolve()
    return ref.startswith(("backend/", "scripts/")) and file.is_file() and REPO in file.parents


def test_evidence_refs_exist_in_the_repo(matrix: list[CoverageEntry]) -> None:
    for e in matrix:
        for ref in e.implementation_refs:
            assert _source_file(ref), (e.catalog_id, ref)
        for ref in e.test_ids:
            assert _test_exists(ref), (e.catalog_id, ref)


def test_summary_never_counts_blocked_or_excluded_as_delivered(
    matrix: list[CoverageEntry],
) -> None:
    s = coverage_summary(matrix)
    assert s["total"] == sum(s["by_class"].values()) == sum(s["by_state"].values()) == 201
    assert s["delivered"] == sum(s["by_state"].get(st.value, 0) for st in DELIVERED)
    assert s["delivered"] < 201  # no 100% coverage claim (§16.4)
    assert s["excluded"] == s["by_state"]["excluded"] == s["by_class"]["X"]
    assert sum(sum(v.values()) for v in s["by_phase_state"].values()) == 201
    assert sum(sum(v.values()) for v in s["by_family_state"].values()) == 201
    assert s["by_family_state"]["R01"]["tested"] == 1  # Partial Close


def test_evidence_check_rejects_fake_references() -> None:
    assert not _test_exists("tests/unit/risk/test_veto.py::test_nope")
    assert not _test_exists("backend/app/core/catalog.py::parse_plan_rows")
    assert not _source_file("backend")
    assert not _source_file("")


def _row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "catalog_id": "CAT-S01-01",
        "capability": "Trend Following",
        "owner": "M05",
        "phase": "MVP",
        "catalog_class": "R",
        "dependency": "x",
        "coverage_state": "planned",
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    "over",
    [
        {"coverage_state": "excluded"},  # an R row cannot be excluded
        {"catalog_class": "X"},  # an X row must be excluded
        {"coverage_state": "certified", "implementation_refs": ["a"], "test_ids": ["b"]},
        {"coverage_state": "implemented"},  # no implementation refs
        {"coverage_state": "tested", "implementation_refs": ["a"]},  # no test ids
        {"coverage_state": "blocked", "blocker_reason": "no feed"},  # no owner/trigger
    ],
)
def test_entry_rules_reject_dishonest_rows(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CoverageEntry.model_validate(_row(**over))


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
    with pytest.raises(ValidationError):
        StrategyProposal.model_validate(proposal.model_dump() | {"order_id": "123"})
    with pytest.raises(ValidationError):  # an entry needs a direction and a size
        StrategyProposal.model_validate(proposal.model_dump() | {"direction": "NEUTRAL"})
    with pytest.raises(ValidationError):
        StrategyProposal.model_validate(proposal.model_dump() | {"requested_lot": None})


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


def _eligibility(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "subject_hash": "sha256:" + "1" * 64,
        "subject_kind": "strategy",
        "subject_ref": "trend_following@1.0.0",
        "user_enabled": True,
        "eligibility": "demo_eligible",
        "approved_modes": ["SHADOW", "DEMO"],
        "context": {},
        "evidence_refs": ["x02:run-1"],
        "policy_version": "p",
        "expires_at": None,
        "revoked_at": None,
    }
    base.update(over)
    return base


def test_consistent_eligibility_record_is_accepted() -> None:
    EligibilityRecord.model_validate(_eligibility())


@pytest.mark.parametrize(
    "over",
    [
        {"revoked_at": "2026-01-01T00:00:00Z"},  # revoked but still eligible
        {"user_enabled": False},  # disabled by the owner but still eligible
        {"eligibility": "disabled"},  # disabled with approved modes
        {"eligibility": "shadow_only"},  # shadow only but DEMO approved
        {"approved_modes": ["LIVE"]},  # demo eligible but LIVE approved
        {"policy_version": ""},
    ],
)
def test_contradictory_eligibility_records_are_rejected(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EligibilityRecord.model_validate(_eligibility(**over))


@pytest.mark.parametrize("model", ["NewsAssessment", "LlmReasoning"])
def test_an_observation_must_become_available_before_it_expires(model: str) -> None:
    """#83: one rule for "usable over [available_at, valid_until)" windows."""
    from tests.support.decision import LATER, _news, _reasoning

    import app.core.contracts as c
    from app.core.enums import Direction

    sample = _news() if model == "NewsAssessment" else _reasoning(Direction.UP, [])
    with pytest.raises(ValidationError, match="available_at"):
        getattr(c, model).model_validate({**sample.model_dump(), "available_at": LATER})
