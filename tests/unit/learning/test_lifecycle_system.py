"""ADR 0001 in the lifecycle (#133): a policy-bound system actor promotes, rolls back,
withdraws and renews models in SHADOW and DEMO only. The owner path is unchanged.

The four refusals ADR 0001 asks for live in two places: the lifecycle refuses LIVE and a
restored Live approval to the system actor (here), and the autonomy gate refuses an
uncertified or changed policy and an incomplete evidence set (`test_autonomy.py`). Never
rolling forward is the champion stack's rule, checked here for the system actor."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.learning.test_lifecycle import DAY, T0, _dataset, _model, _x02

from app.core.enums import Eligibility, Mode
from app.learning.lifecycle import (
    OWNER_ACTOR,
    SYSTEM_ACTOR,
    DatasetStore,
    LifecycleRefused,
    ModelLifecycle,
)
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "life.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def now() -> list[datetime]:
    return [T0 + DAY]


@pytest.fixture
def life(conn: sqlite3.Connection, now: list[datetime]) -> ModelLifecycle:
    reg = ArtifactRegistry(conn, clock=lambda: now[0])
    lc = ModelLifecycle(conn, reg, DatasetStore(conn), clock=lambda: now[0])
    lc.datasets.save(_dataset())
    for v in ("1", "2", "3"):
        lc.propose_candidate(_model(v, v), dataset_refs=["xau-h1@1"])
    return lc


def _promote(life: ModelLifecycle, version: str, *, by: str = SYSTEM_ACTOR,
             modes: list[Mode] | None = None) -> None:  # fmt: skip
    life.promote("xgb-dir", version, modes=modes or [Mode.SHADOW, Mode.DEMO],
                 evidence_refs=[f"x02:run:{version}"], policy_ref="protocol:abc",
                 approved_by=by, approval_ref=f"auto-{version}",
                 expires_at=T0 + 30 * DAY)  # fmt: skip


def test_the_system_actor_promotes_to_shadow_and_demo_and_is_audited_as_itself(
    conn: sqlite3.Connection, life: ModelLifecycle
) -> None:
    _promote(life, "1")
    assert life.champion("xgb-dir") == "1"
    rec = life.registry.eligibility("model", "xgb-dir@1")
    assert rec is not None and rec.approved_modes == [Mode.SHADOW, Mode.DEMO]
    actors = {r[0] for r in conn.execute(
        "SELECT actor FROM audit_log WHERE action IN ('learning.promote',"
        " 'registry.eligibility')")}  # fmt: skip
    assert actors == {SYSTEM_ACTOR}
    row = conn.execute("SELECT approved_by FROM learning_promotions").fetchone()
    assert row[0] == SYSTEM_ACTOR


def test_the_system_actor_can_never_promote_to_live(life: ModelLifecycle) -> None:
    for modes in ([Mode.LIVE], [Mode.DEMO, Mode.LIVE]):
        with pytest.raises(LifecycleRefused, match="SHADOW and DEMO"):
            life.promote("xgb-dir", "1", modes=modes, evidence_refs=["x02:run:1"],
                         policy_ref="p", approved_by=SYSTEM_ACTOR, approval_ref="a",
                         live_qualification_ref="x02:live-qualification:r7")  # fmt: skip
    assert life.champion("xgb-dir") is None


def test_the_system_actor_never_restores_or_withdraws_a_live_approval(
    conn: sqlite3.Connection, now: list[datetime]
) -> None:
    """The owner's Live champion is out of the system's reach: the system can neither
    roll back onto it nor withdraw it."""
    reg = ArtifactRegistry(conn, clock=lambda: now[0])
    life = ModelLifecycle(conn, reg, DatasetStore(conn), clock=lambda: now[0],
                          live_verifier=_x02)  # fmt: skip
    life.datasets.save(_dataset())
    for v in ("1", "2"):
        life.propose_candidate(_model(v, v), dataset_refs=["xau-h1@1"])
    life.promote("xgb-dir", "1", modes=[Mode.DEMO, Mode.LIVE], evidence_refs=["x02:r"],
                 policy_ref="p", approved_by=OWNER_ACTOR, approval_ref="o",
                 live_qualification_ref="x02:live-qualification:r7")  # fmt: skip
    with pytest.raises(LifecycleRefused, match="SHADOW and DEMO"):
        life.withdraw("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill")
    _promote(life, "2", by=OWNER_ACTOR)  # a DEMO champion above the owner's Live one
    with pytest.raises(LifecycleRefused, match="SHADOW and DEMO"):
        life.rollback("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill")
    assert life.champion("xgb-dir") == "2"
    life.withdraw("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill")  # no Live in it
    assert life.champion("xgb-dir") is None


def test_the_system_actor_cannot_roll_forward(life: ModelLifecycle) -> None:
    _promote(life, "1")
    _promote(life, "2")
    assert life.rollback("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill: brier") == "1"
    with pytest.raises(LifecycleRefused, match="previous"):
        life.rollback("xgb-dir", approved_by=SYSTEM_ACTOR, reason="again")
    assert life.champion("xgb-dir") == "1"
    v2 = life.registry.eligibility("model", "xgb-dir@2")
    assert v2 is not None and v2.eligibility is Eligibility.DISABLED


def test_withdraw_leaves_no_champion_and_disables_the_model(life: ModelLifecycle) -> None:
    _promote(life, "1")
    life.withdraw("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill: psi")
    assert life.champion("xgb-dir") is None
    rec = life.registry.eligibility("model", "xgb-dir@1")
    assert rec is not None and rec.eligibility is Eligibility.DISABLED
    with pytest.raises(LifecycleRefused, match="no champion"):
        life.withdraw("xgb-dir", approved_by=SYSTEM_ACTOR, reason="again")
    with pytest.raises(LifecycleRefused, match="no champion"):
        life.rollback("xgb-dir", approved_by=SYSTEM_ACTOR, reason="nothing to roll from")


def test_a_withdrawn_version_is_never_restored_by_a_later_rollback(
    life: ModelLifecycle,
) -> None:
    _promote(life, "1")
    _promote(life, "2")
    life.withdraw("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill")
    _promote(life, "3")
    assert life.rollback("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill") == "1"


def test_renew_extends_only_the_current_unexpired_champion(
    life: ModelLifecycle, now: list[datetime]
) -> None:
    _promote(life, "1")
    life.renew("xgb-dir", approved_by=SYSTEM_ACTOR, expires_at=T0 + 60 * DAY, reason="ok")
    rec = life.registry.eligibility("model", "xgb-dir@1")
    assert rec is not None and rec.expires_at == T0 + 60 * DAY
    assert life.champion("xgb-dir") == "1"
    now[0] = T0 + 61 * DAY
    with pytest.raises(LifecycleRefused, match="expired"):
        life.renew("xgb-dir", approved_by=SYSTEM_ACTOR, expires_at=T0 + 90 * DAY, reason="x")


def test_a_rollback_restores_the_latest_renewed_record(
    life: ModelLifecycle, now: list[datetime]
) -> None:
    _promote(life, "1")
    life.renew("xgb-dir", approved_by=SYSTEM_ACTOR, expires_at=T0 + 60 * DAY, reason="ok")
    _promote(life, "2")
    now[0] = T0 + 40 * DAY  # past v1's first approval, inside its renewal
    assert life.rollback("xgb-dir", approved_by=SYSTEM_ACTOR, reason="kill") == "1"
    rec = life.registry.eligibility("model", "xgb-dir@1")
    assert rec is not None and rec.approves(Mode.DEMO, now[0])


def test_tenure_is_when_the_champion_took_over(life: ModelLifecycle, now: list[datetime]) -> None:
    assert life.tenure("xgb-dir") is None
    _promote(life, "1")
    start = now[0]
    now[0] = start + timedelta(hours=5)
    life.renew("xgb-dir", approved_by=SYSTEM_ACTOR, expires_at=T0 + 60 * DAY, reason="ok")
    assert life.tenure("xgb-dir") == ("1", start)  # a renewal is not a new tenure


def test_the_system_actor_never_replaces_an_owner_live_champion(
    conn: sqlite3.Connection, now: list[datetime]
) -> None:
    reg = ArtifactRegistry(conn, clock=lambda: now[0])
    life = ModelLifecycle(conn, reg, DatasetStore(conn), clock=lambda: now[0],
                          live_verifier=_x02)  # fmt: skip
    life.datasets.save(_dataset())
    for v in ("1", "2"):
        life.propose_candidate(_model(v, v), dataset_refs=["xau-h1@1"])
    life.promote("xgb-dir", "1", modes=[Mode.DEMO, Mode.LIVE], evidence_refs=["x02:r"],
                 policy_ref="p", approved_by=OWNER_ACTOR, approval_ref="o",
                 live_qualification_ref="x02:live-qualification:r7")  # fmt: skip
    with pytest.raises(LifecycleRefused, match="SHADOW and DEMO"):
        _promote(life, "2")
    with pytest.raises(LifecycleRefused, match="SHADOW and DEMO"):
        _promote(life, "1")  # re-promoting the Live champion itself would overwrite it
    assert life.champion("xgb-dir") == "1"
    rec = reg.eligibility("model", "xgb-dir@1")
    assert rec is not None and rec.approves(Mode.LIVE, now[0])


def test_a_shadowing_candidate_is_approved_for_shadow_only_and_never_crowned(
    life: ModelLifecycle,
) -> None:
    _promote(life, "1")
    life.shadow("xgb-dir", "2", approved_by=SYSTEM_ACTOR, evidence_refs=["x02:run:2"],
                policy_ref="protocol:abc", expires_at=T0 + 90 * DAY)  # fmt: skip
    rec = life.registry.eligibility("model", "xgb-dir@2")
    assert rec is not None and rec.approved_modes == [Mode.SHADOW]
    assert rec.eligibility is Eligibility.SHADOW_ONLY
    assert life.champion("xgb-dir") == "1"
    with pytest.raises(LifecycleRefused, match="champion"):
        life.shadow("xgb-dir", "1", approved_by=SYSTEM_ACTOR, evidence_refs=["e"],
                    policy_ref="p", expires_at=T0 + 90 * DAY)  # fmt: skip
    with pytest.raises(LifecycleRefused, match="champion"):
        life.end_shadow("xgb-dir", "1", approved_by=SYSTEM_ACTOR, reason="x")
    life.end_shadow("xgb-dir", "2", approved_by=SYSTEM_ACTOR, reason="rejected")
    rec = life.registry.eligibility("model", "xgb-dir@2")
    assert rec is not None and rec.eligibility is Eligibility.DISABLED
    with pytest.raises(LifecycleRefused, match="only the owner"):
        life.shadow("xgb-dir", "3", approved_by="m09", evidence_refs=["e"], policy_ref="p",
                    expires_at=T0 + 90 * DAY)  # fmt: skip


def test_migration_0017_keeps_every_promotion_and_stays_append_only(tmp_path: Path) -> None:
    """The champion log is rebuilt to allow WITHDRAW and RENEW: its rows survive, and so do
    its append-only triggers."""
    from app.storage.db import MIGRATIONS_DIR

    before = tmp_path / "before"
    before.mkdir()
    for f in MIGRATIONS_DIR.glob("*.sql"):
        if f.name < "0017":
            (before / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
    c = connect(tmp_path / "m.db")
    try:
        migrate(c, before)
        lc = ModelLifecycle(c, ArtifactRegistry(c, clock=lambda: T0), DatasetStore(c),
                            clock=lambda: T0 + DAY)  # fmt: skip
        lc.datasets.save(_dataset())
        lc.propose_candidate(_model("1", "1"), dataset_refs=["xau-h1@1"])
        lc.promote("xgb-dir", "1", modes=[Mode.DEMO], evidence_refs=["e"], policy_ref="p",
                   approved_by=OWNER_ACTOR, approval_ref="a")  # fmt: skip
        rows = c.execute("SELECT * FROM learning_promotions").fetchall()
        assert migrate(c) == ["0017_learning_autonomy"]
        assert c.execute("SELECT * FROM learning_promotions").fetchall() == rows
        assert lc.champion("xgb-dir") == "1"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute("UPDATE learning_promotions SET version = '9'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            c.execute("DELETE FROM learning_promotions")
    finally:
        c.close()
