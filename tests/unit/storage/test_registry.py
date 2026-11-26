"""M08 V1.1: versioned registry, trusted artifact loading, isolation (#20; UST-01, UST-07..10;
AT-28; CAT-X04-01)."""

import hashlib
import sqlite3
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.contracts import EligibilityRecord, ParameterBound, StrategyManifest
from app.core.enums import Eligibility, Mode
from app.storage.artifacts import (
    ArtifactLoader,
    BudgetExceeded,
    IsolatedRunFailed,
    UntrustedArtifact,
    run_isolated,
)
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry, RegistryConflict, UnknownSubject

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "reg.db")
    migrate(c)
    yield c
    c.close()


def _manifest(sha: str = "0" * 64, version: str = "1.0.0", **over: object) -> StrategyManifest:
    base: dict[str, object] = {
        "strategy_id": "trend_following",
        "version": version,
        "artifact_hash": f"sha256:{sha}",
        "family": "S01",
        "catalog_ids": ["CAT-S01-01"],
        "technique_refs": [],
        "timeframes": ["M15"],
        "warmup_bars": 50,
        "parameters": {"fast": ParameterBound(low=Decimal(5), high=Decimal(50))},
        "risk_tags": [],
        "supported_modes": [Mode.SHADOW, Mode.DEMO],
    }
    base.update(over)
    return StrategyManifest.model_validate(base)


def _eligibility(sha: str = "0" * 64, **over: object) -> EligibilityRecord:
    base: dict[str, object] = {
        "subject_hash": f"sha256:{sha}",
        "subject_kind": "strategy",
        "subject_ref": "trend_following@1.0.0",
        "user_enabled": True,
        "eligibility": Eligibility.DEMO_ELIGIBLE,
        "approved_modes": [Mode.SHADOW, Mode.DEMO],
        "context": {"symbol": "XAUUSD"},
        "evidence_refs": ["x02:run-1"],
        "policy_version": "p1",
        "expires_at": None,
        "revoked_at": None,
    }
    base.update(over)
    return EligibilityRecord.model_validate(base)


# --- registry ----------------------------------------------------------------------------------


def test_register_is_idempotent_and_versions_are_immutable(conn: sqlite3.Connection) -> None:
    reg = ArtifactRegistry(conn)
    reg.register("strategy", _manifest(), actor="m05")
    reg.register("strategy", _manifest(), actor="m05")  # same hash: no-op
    with pytest.raises(RegistryConflict):
        reg.register("strategy", _manifest(sha="1" * 64), actor="m05")  # same version, new code
    reg.register("strategy", _manifest(sha="1" * 64, version="1.1.0"), actor="m05")
    assert [m.version for m in reg.versions("strategy", "trend_following")] == ["1.0.0", "1.1.0"]
    got = reg.get("strategy", "trend_following", "1.0.0")
    assert got is not None and got.artifact_hash == f"sha256:{'0' * 64}"


def test_unregistered_subject_has_no_eligibility(conn: sqlite3.Connection) -> None:
    reg = ArtifactRegistry(conn)
    assert reg.eligibility("strategy", "trend_following@1.0.0") is None
    with pytest.raises(UnknownSubject):
        reg.set_eligibility(_eligibility(), actor="owner")


def test_eligibility_must_match_the_registered_hash(conn: sqlite3.Connection) -> None:
    reg = ArtifactRegistry(conn)
    reg.register("strategy", _manifest(), actor="m05")
    with pytest.raises(RegistryConflict):
        reg.set_eligibility(_eligibility(sha="9" * 64), actor="owner")


def test_latest_eligibility_wins_and_every_change_is_audited(conn: sqlite3.Connection) -> None:
    reg = ArtifactRegistry(conn)
    reg.register("strategy", _manifest(), actor="m05")
    reg.set_eligibility(_eligibility(), actor="owner")
    revoked = _eligibility(
        eligibility=Eligibility.DISABLED, approved_modes=[], revoked_at=NOW, user_enabled=False
    )
    reg.set_eligibility(revoked, actor="owner")
    current = reg.eligibility("strategy", "trend_following@1.0.0")
    assert current is not None and current.eligibility is Eligibility.DISABLED
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions.count("registry.register") == 1
    assert actions.count("registry.eligibility") == 2


# --- trusted loading (CAT-X04-01) -------------------------------------------------------------


@pytest.fixture
def trusted_pkg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pkg = tmp_path / "trusted_artifacts"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "trend.py").write_text("def signal(x):\n    return x * 2\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield pkg
    for name in [m for m in sys.modules if m.startswith("trusted_artifacts")]:
        del sys.modules[name]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_loads_only_an_allowlisted_module_whose_hash_matches(
    conn: sqlite3.Connection, trusted_pkg: Path
) -> None:
    reg = ArtifactRegistry(conn)
    reg.register("strategy", _manifest(sha=_sha(trusted_pkg / "trend.py")), actor="m05")
    loader = ArtifactLoader(reg, allowed_packages=("trusted_artifacts",))
    module = loader.load("strategy", "trend_following", "1.0.0", "trusted_artifacts.trend")
    assert module.signal(2) == 4


def test_tampered_artifact_is_refused(conn: sqlite3.Connection, trusted_pkg: Path) -> None:
    reg = ArtifactRegistry(conn)
    reg.register("strategy", _manifest(sha=_sha(trusted_pkg / "trend.py")), actor="m05")
    (trusted_pkg / "trend.py").write_text("def signal(x):\n    return 0\n", encoding="utf-8")
    loader = ArtifactLoader(reg, allowed_packages=("trusted_artifacts",))
    with pytest.raises(UntrustedArtifact, match="hash"):
        loader.load("strategy", "trend_following", "1.0.0", "trusted_artifacts.trend")


@pytest.mark.parametrize(
    "module_name",
    ["os", "trusted_artifacts_evil.trend", "trusted_artifacts", "../trusted_artifacts.trend"],
)
def test_modules_outside_the_allowlist_are_refused(
    conn: sqlite3.Connection, trusted_pkg: Path, module_name: str
) -> None:
    reg = ArtifactRegistry(conn)
    reg.register("strategy", _manifest(sha=_sha(trusted_pkg / "trend.py")), actor="m05")
    loader = ArtifactLoader(reg, allowed_packages=("trusted_artifacts",))
    with pytest.raises(UntrustedArtifact):
        loader.load("strategy", "trend_following", "1.0.0", module_name)


def test_unregistered_artifact_is_refused(conn: sqlite3.Connection, trusted_pkg: Path) -> None:
    loader = ArtifactLoader(ArtifactRegistry(conn), allowed_packages=("trusted_artifacts",))
    with pytest.raises(UntrustedArtifact, match="not registered"):
        loader.load("strategy", "trend_following", "1.0.0", "trusted_artifacts.trend")


# --- isolation for research candidates (UST-07) ----------------------------------------------


def _double(x: int) -> int:
    return x * 2


def _forever(_: int) -> int:
    while True:
        pass


def test_isolated_run_returns_the_result() -> None:
    assert run_isolated(_double, 21, timeout_s=10) == 42


def test_isolated_run_is_killed_past_its_time_budget() -> None:
    with pytest.raises(BudgetExceeded):
        run_isolated(_forever, 0, timeout_s=1)


def _boom(_: int) -> int:
    raise RuntimeError("bad candidate")


def test_a_failing_candidate_is_reported_not_mistaken_for_a_timeout() -> None:
    with pytest.raises(IsolatedRunFailed, match="bad candidate"):
        run_isolated(_boom, 0, timeout_s=10)
