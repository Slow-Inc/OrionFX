"""Colab runner (#134; PRD #120 slice 7): guards in front of the VM, the stop that always
runs, and the local verification that decides whether anything is kept."""

import gzip
import io
import json
import signal
import sqlite3
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.support.colab import FakeColab, run_stub

from app.core.contracts import StrategyManifest, sha256_ref
from app.core.enums import Mode
from app.learning import colab_runner
from app.learning import colab_vm as vm
from app.learning.colab_job import CODE, DATASET, JOB, BundleRefused, JobSpec
from app.learning.colab_runner import (
    RUN_RECORD,
    ColabCli,
    ColabError,
    ColabRunner,
    ColabTimeout,
    CuFloorRefused,
    ReturnRefused,
    RunInterrupted,
    RunRefused,
    RunTimeout,
    VmStopFailed,
    parse_balance,
)
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

HAPPY_CALLS = [
    "usage", "new", "upload", "exec",
    "download:orionfx-run.log", "download:orionfx-out.tar", "stop",
]  # fmt: skip
TARGET = ("models", "stub-colab", "0.0.1")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "registry.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def job_path(tmp_path: Path) -> Path:
    """A minimal, valid bundle (tiny code archive; no git needed)."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    data = b'{"split":"train","x":1}\n{"split":"train","x":2}\n'
    (bundle / DATASET).write_bytes(data)
    with tarfile.open(bundle / CODE, "w") as tar:
        info = tarfile.TarInfo("backend/app/__init__.py")
        tar.addfile(info, io.BytesIO(b""))
    job = JobSpec(
        job_id="smoke-1",
        protocol_hash=sha256_ref(b"protocol"),
        config={"learning_rate": 0.1},
        dataset_path=DATASET,
        dataset_hash=sha256_ref(data),
        code_sha="0" * 40,
        code_hash=sha256_ref((bundle / CODE).read_bytes()),
        seeds=[7],
        model_id="stub-colab",
        model_version="0.0.1",
        timeout_s=600,
    )
    (bundle / JOB).write_text(job.model_dump_json(), encoding="utf-8")
    return bundle / JOB


def _same_code(tmp_path: Path) -> Any:
    """Stands in for `git_archive`: the fixture's code.tar is the archive of its pin."""
    return lambda sha: (tmp_path / "bundle" / CODE).read_bytes()


def _runner(fake: FakeColab, conn: sqlite3.Connection, tmp_path: Path, **kw: Any) -> ColabRunner:
    kw.setdefault("archive", _same_code(tmp_path))
    kw.setdefault("known_secrets", {})
    return ColabRunner(
        fake, ArtifactRegistry(conn), artifacts=tmp_path / "models", work=tmp_path / "runs", **kw
    )


def _nothing_kept(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert ArtifactRegistry(conn).get("model", "stub-colab", "0.0.1") is None
    assert not tmp_path.joinpath(*TARGET).exists()


def test_happy_path_keeps_the_verified_files_and_a_run_record_but_registers_nothing(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab()
    result = _runner(fake, conn, tmp_path).run(job_path)

    assert fake.calls == HAPPY_CALLS
    assert fake.sessions == set()  # stopped
    target = tmp_path.joinpath(*TARGET)
    assert result.artifact_dir == target
    assert sorted(p.name for p in target.iterdir()) == sorted([*vm.RETURN_FILES, RUN_RECORD])
    # Option A: registration waits for #130's trusted adapter path.
    assert ArtifactRegistry(conn).get("model", "stub-colab", "0.0.1") is None
    record = json.loads((target / RUN_RECORD).read_text(encoding="utf-8"))
    assert record["registered"] is False
    assert record["supported_modes"] == ["SHADOW"]
    assert record["files"][vm.WEIGHTS] == sha256_ref((target / vm.WEIGHTS).read_bytes())
    assert record["job"]["code_hash"] == json.loads(job_path.read_text())["code_hash"]
    assert record["session"] == result.session
    assert result.card.supported_modes == [Mode.SHADOW]
    assert (result.run_dir / "run.log").read_bytes() == b"fake driver log\n"


def test_the_uploaded_bundle_is_built_from_the_scanned_bytes(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    scanned = (job_path.parent / DATASET).read_bytes()

    def swap_after_scan(sha: str) -> bytes:  # runs after the scan, before the upload
        (job_path.parent / DATASET).write_bytes(b'{"swapped":true}\n')
        return _same_code(tmp_path)(sha)

    fake = FakeColab()
    _runner(fake, conn, tmp_path, archive=swap_after_scan).run(job_path)
    with tarfile.open(fileobj=io.BytesIO(fake.remote[vm.REMOTE_BUNDLE])) as tar:
        f = tar.extractfile(DATASET)
        assert f is not None and f.read() == scanned


def test_the_cu_floor_refuses_before_any_vm_starts(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(balance=Decimal("19.99"))
    with pytest.raises(CuFloorRefused, match=r"19\.99.*20"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls == ["usage"]  # no `new`, so nothing to stop
    _nothing_kept(conn, tmp_path)


def test_a_balance_exactly_at_the_floor_proceeds(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(balance=Decimal("20"))
    _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls == HAPPY_CALLS


def test_an_unreadable_balance_is_a_cu_refusal(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(fail={"usage": ColabError("cannot read the compute-unit balance")})
    with pytest.raises(CuFloorRefused, match="balance"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls == ["usage"]


def test_the_cu_floor_is_configurable_upwards(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(balance=Decimal("30"))
    with pytest.raises(CuFloorRefused):
        _runner(fake, conn, tmp_path, min_cu=Decimal("50")).run(job_path)


@pytest.mark.parametrize("value", ["19.99", "0", "-1", "NaN", "Infinity"])
def test_the_cu_floor_cannot_be_lowered_or_made_non_finite(
    conn: sqlite3.Connection, tmp_path: Path, value: str
) -> None:
    with pytest.raises(ValueError, match="min_cu"):
        _runner(FakeColab(), conn, tmp_path, min_cu=Decimal(value))


def test_an_already_registered_version_is_refused_before_spending(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    ArtifactRegistry(conn).register(
        "model",
        StrategyManifest(
            strategy_id="stub-colab",
            version="0.0.1",
            artifact_hash=sha256_ref(b"w"),
            family="model",
            catalog_ids=["CAT-A01-05"],
            technique_refs=[],
            timeframes=["M15"],
            warmup_bars=0,
            parameters={},
            risk_tags=[],
            supported_modes=[Mode.SHADOW],
        ),
        actor="test",
    )
    fake = FakeColab()
    with pytest.raises(RunRefused, match="already"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls == []


def test_a_code_archive_that_is_not_the_pinned_commit_is_refused_before_spending(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab()
    with pytest.raises(BundleRefused, match="not the git archive of code_sha"):
        _runner(fake, conn, tmp_path, archive=lambda sha: b"another commit").run(job_path)
    assert fake.calls == []
    _nothing_kept(conn, tmp_path)


def test_a_bundle_contaminated_after_it_was_built_is_refused_at_run_time(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    data = b'{"x":"hunter2-battery"}\n'
    (job_path.parent / DATASET).write_bytes(data)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["dataset_hash"] = sha256_ref(data)
    job_path.write_text(json.dumps(job), encoding="utf-8")
    fake = FakeColab()
    with pytest.raises(BundleRefused, match="MT5_PASSWORD") as err:
        _runner(fake, conn, tmp_path, known_secrets={"MT5_PASSWORD": "hunter2-battery"}).run(
            job_path
        )
    assert "hunter2-battery" not in str(err.value)
    assert fake.calls == []


def test_an_artifact_directory_already_on_disk_is_refused_before_spending(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    tmp_path.joinpath(*TARGET).mkdir(parents=True)
    fake = FakeColab()
    with pytest.raises(RunRefused, match="already"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls == []


def test_a_failed_copy_leaves_no_partial_artifact_directory(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def half_copy(src: Path, dst: Path) -> None:
        Path(dst).mkdir(parents=True)
        (Path(dst) / vm.CARD).write_text("{}")
        raise OSError("disk full")

    monkeypatch.setattr(colab_runner.shutil, "copytree", half_copy)
    with pytest.raises(OSError, match="disk full"):
        _runner(FakeColab(), conn, tmp_path).run(job_path)
    _nothing_kept(conn, tmp_path)


@pytest.mark.parametrize("step", ["new", "upload", "exec"])
def test_a_colab_failure_still_stops_the_vm_and_keeps_nothing(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, step: str
) -> None:
    fake = FakeColab(fail={step: ColabError(f"{step} broke")})
    with pytest.raises(ColabError, match="broke"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_kept(conn, tmp_path)


def test_a_failed_trainer_returns_no_output_and_keeps_nothing(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    def crash(fake: FakeColab) -> None:
        fake.remote[vm.REMOTE_LOG] = b"Traceback: boom\n"  # no out.tar

    fake = FakeColab(on_exec=crash)
    with pytest.raises(ColabError, match=r"orionfx-out.tar"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_kept(conn, tmp_path)


def test_a_colab_timeout_stops_the_vm(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(fail={"exec": ColabTimeout("exec exceeded 600s")})
    with pytest.raises(ColabTimeout):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_kept(conn, tmp_path)


def test_the_hard_deadline_stops_the_vm_once_the_job_timeout_is_spent(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    now = [0.0]

    def slow_exec(fake: FakeColab) -> None:
        now[0] = 601.0  # past the job's 600 s

    fake = FakeColab(on_exec=slow_exec)
    with pytest.raises(RunTimeout, match="600"):
        _runner(fake, conn, tmp_path, clock=lambda: now[0]).run(job_path)
    assert fake.calls == ["usage", "new", "upload", "exec", "stop"]
    _nothing_kept(conn, tmp_path)


def test_a_deadline_spent_before_new_still_attempts_the_stop(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    times = iter([0.0])  # the start; every later read is past the deadline
    fake = FakeColab()
    with pytest.raises(RunTimeout):
        _runner(fake, conn, tmp_path, clock=lambda: next(times, 601.0)).run(job_path)
    assert fake.calls == ["usage", "stop"]


def test_an_exec_failure_is_not_masked_when_the_deadline_passes_during_the_log_download(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    # Clock reads: run start, new, upload, exec, the finally's check (still inside), then
    # past the deadline when the log download asks `left()`.
    times = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    fake = FakeColab(fail={"exec": ColabError("exec broke")})
    with pytest.raises(ColabError, match="exec broke"):
        _runner(fake, conn, tmp_path, clock=lambda: next(times, 601.0)).run(job_path)
    assert fake.calls[-1] == "stop"


def test_a_signal_routes_into_the_stop(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    def interrupted(fake: FakeColab) -> None:
        signal.raise_signal(signal.SIGINT)

    before = signal.getsignal(signal.SIGINT)
    fake = FakeColab(on_exec=interrupted)
    with pytest.raises(RunInterrupted):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    assert signal.getsignal(signal.SIGINT) is before  # handlers restored
    _nothing_kept(conn, tmp_path)


def test_a_signal_during_the_stop_finishes_the_stop_then_interrupts(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    def ctrl_c(fake: FakeColab) -> None:
        signal.raise_signal(signal.SIGINT)  # a second Ctrl-C while stopping

    fake = FakeColab(on_stop=ctrl_c)
    with pytest.raises(RunInterrupted, match="during colab stop"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.sessions == set()  # the stop completed
    _nothing_kept(conn, tmp_path)  # and nothing was kept afterwards


def test_a_stop_that_fails_once_is_retried(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(fail_once={"stop": ColabError("transient")})
    _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-2:] == ["stop", "stop"]
    assert fake.sessions == set()


def test_a_failed_stop_is_loud(conn: sqlite3.Connection, tmp_path: Path, job_path: Path) -> None:
    fake = FakeColab(fail={"stop": ColabError("stop broke")})
    with pytest.raises(VmStopFailed, match=r"may still be billing.*--config .*colab-state\.json"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls.count("stop") == 2  # retried once
    _nothing_kept(conn, tmp_path)


def test_a_failed_stop_after_a_started_vm_is_loud_even_when_a_step_failed(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(fail={"exec": ColabError("exec broke"), "stop": ColabError("stop broke")})
    with pytest.raises(VmStopFailed, match="may still be billing") as err:
        _runner(fake, conn, tmp_path).run(job_path)
    assert isinstance(err.value.__context__, ColabError)  # the exec failure is chained
    _nothing_kept(conn, tmp_path)


def test_a_failed_new_keeps_its_own_error_when_the_stop_also_fails(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(fail={"new": ColabError("quota exceeded"), "stop": ColabError("no such")})
    with pytest.raises(ColabError, match="quota exceeded") as err:
        _runner(fake, conn, tmp_path).run(job_path)
    assert not isinstance(err.value, VmStopFailed)  # the real cause is not masked
    assert fake.calls[-1] == "stop"  # the stop was still attempted
    notes = " ".join(getattr(err.value, "__notes__", []))
    assert "may still be billing" in notes and "colab-state.json" in notes
    _nothing_kept(conn, tmp_path)


def _edit_json(path: Path, **changes: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def _rehash_card(out: Path, **changes: Any) -> None:
    """Change the card and fix up the manifest so only the card check can catch it."""
    _edit_json(out / vm.CARD, **changes)
    manifest = json.loads((out / vm.MANIFEST).read_text(encoding="utf-8"))
    manifest["files"][vm.CARD] = sha256_ref((out / vm.CARD).read_bytes())
    (out / vm.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def _manifest_files(out: Path, **extra: str) -> None:
    manifest = json.loads((out / vm.MANIFEST).read_text(encoding="utf-8"))
    manifest["files"].update(extra)
    (out / vm.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


def _rebind(out: Path, **changes: Any) -> None:
    manifest = json.loads((out / vm.MANIFEST).read_text(encoding="utf-8"))
    manifest["job"].update(changes)
    (out / vm.MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


TAMPERS = {
    "hash-mismatch": (lambda out: (out / vm.WEIGHTS).write_text('{"kind":"swapped"}'), "hash"),
    "extra-file": (lambda out: (out / "notes.txt").write_text("hi"), "notes.txt"),
    "missing-file": (lambda out: (out / vm.CARD).unlink(), "card.json"),
    "directory": (lambda out: (out / "sub").mkdir(), "sub"),
    "unbound": (lambda out: _edit_json(out / vm.MANIFEST, job={"job_id": "other"}), "bound"),
    "other-config": (lambda out: _rebind(out, config={"learning_rate": 9}), "bound"),
    "other-entrypoint": (lambda out: _rebind(out, entrypoint="app.x"), "bound"),
    "bad-card": (lambda out: _rehash_card(out, horizon_bars=0), "not a valid ModelCard"),
    "manifest-not-an-object": (lambda out: (out / vm.MANIFEST).write_text("[]"), "not bound"),
    "manifest-files-extra-key": (
        lambda out: _manifest_files(out, extra="sha256:" + "0" * 64),
        "the manifest lists",
    ),
    "card-names-other-weights": (
        lambda out: _rehash_card(out, artifact_hash="sha256:" + "0" * 64),
        "artifact_hash",
    ),
    "card-claims-live": (lambda out: _rehash_card(out, supported_modes=["SHADOW", "LIVE"]), "LIVE"),
    "card-claims-calibrated": (
        lambda out: _rehash_card(out, calibrated=True, evidence_refs=["x"]),
        "calibrated",
    ),
    "card-claims-evidence": (lambda out: _rehash_card(out, evidence_refs=["x"]), "evidence_refs"),
    "card-claims-validation": (
        lambda out: _rehash_card(out, validation={"brier": "0.1"}),
        "validation",
    ),
}


def _rehash_weights(out: Path, text: str) -> None:
    (out / vm.WEIGHTS).write_text(text, encoding="utf-8")
    digest = sha256_ref((out / vm.WEIGHTS).read_bytes())
    _rehash_card(out, artifact_hash=digest)
    _manifest_files(out, **{vm.WEIGHTS: digest})


def test_weights_too_deep_to_parse_are_refused_not_crashed(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # How deep the parser can go depends on the platform's stack, so force the error.
    original = json.loads

    def too_deep(s: str | bytes, *args: Any, **kwargs: Any) -> Any:
        if s == b'{"deep":1}':
            raise RecursionError("maximum recursion depth exceeded")
        return original(s, *args, **kwargs)

    fake = FakeColab(tamper=lambda out: _rehash_weights(out, '{"deep":1}'))
    monkeypatch.setattr(json, "loads", too_deep)
    with pytest.raises(ReturnRefused, match="unreadable return"):
        _runner(fake, conn, tmp_path).run(job_path)
    _nothing_kept(conn, tmp_path)


@pytest.mark.parametrize(("tamper", "reason"), list(TAMPERS.values()), ids=list(TAMPERS))
def test_a_return_that_fails_verification_is_refused_and_nothing_is_kept(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, tamper: Any, reason: str
) -> None:
    fake = FakeColab(tamper=tamper)
    with pytest.raises(ReturnRefused, match=reason):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_kept(conn, tmp_path)


def test_a_card_for_another_model_is_refused(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(tamper=lambda out: _rehash_card(out, model_id="other"))
    with pytest.raises(ReturnRefused, match="another model"):
        _runner(fake, conn, tmp_path).run(job_path)
    _nothing_kept(conn, tmp_path)


def test_a_compressed_return_is_refused(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    def gzipped(fake: FakeColab) -> None:
        run_stub(fake)
        fake.remote[vm.REMOTE_OUT] = gzip.compress(fake.remote[vm.REMOTE_OUT])

    fake = FakeColab(on_exec=gzipped)
    with pytest.raises(ReturnRefused, match="unreadable return"):
        _runner(fake, conn, tmp_path).run(job_path)
    _nothing_kept(conn, tmp_path)


def test_a_return_over_the_size_cap_is_refused_before_it_is_opened(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(colab_runner, "MAX_RETURN_BYTES", 1024)
    with pytest.raises(ReturnRefused, match="size cap"):
        _runner(FakeColab(), conn, tmp_path).run(job_path)
    _nothing_kept(conn, tmp_path)


def test_a_return_member_over_the_size_cap_is_refused(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(colab_runner, "MAX_MEMBER_BYTES", 16)
    with pytest.raises(ReturnRefused, match="unexpected"):
        _runner(FakeColab(), conn, tmp_path).run(job_path)
    _nothing_kept(conn, tmp_path)


def _main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, fake: FakeColab, *extra: str
) -> int:
    monkeypatch.setattr(colab_runner, "ColabCli", lambda **kw: fake)
    monkeypatch.setattr(colab_runner, "git_archive", lambda repo, sha: _same_code(tmp_path)(sha))
    return colab_runner.main(
        ["run", str(job_path), "--repo", str(tmp_path), "--db", str(tmp_path / "cli.db"),
         "--artifacts", str(tmp_path / "models"), "--work", str(tmp_path / "runs"), *extra]
    )  # fmt: skip


def test_main_exits_3_on_the_cu_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, capsys: Any
) -> None:
    fake = FakeColab(balance=Decimal("19"))
    assert _main(monkeypatch, tmp_path, job_path, fake) == 3
    assert "stop and report to the owner" in capsys.readouterr().err
    assert fake.calls == ["usage"]


def test_main_exits_2_on_a_refused_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, capsys: Any
) -> None:
    (job_path.parent / "extra.txt").write_text("x")
    fake = FakeColab()
    assert _main(monkeypatch, tmp_path, job_path, fake) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert fake.calls == []


def test_main_refuses_values_from_the_repo_dotenv_without_printing_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, capsys: Any
) -> None:
    (tmp_path / ".env").write_text("MT5_LOGIN=train\n", encoding="utf-8")  # in the dataset
    fake = FakeColab()
    assert _main(monkeypatch, tmp_path, job_path, fake) == 2
    err = capsys.readouterr().err
    assert "MT5_LOGIN" in err and "=train" not in err
    assert fake.calls == []


@pytest.mark.parametrize("value", ["5", "NaN", "abc"])
def test_main_rejects_a_lower_or_invalid_cu_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, value: str
) -> None:
    fake = FakeColab()
    with pytest.raises(SystemExit) as exit_:
        _main(monkeypatch, tmp_path, job_path, fake, "--min-cu", value)
    assert exit_.value.code == 2
    assert fake.calls == []


def test_main_requires_an_explicit_db(tmp_path: Path, job_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_:
        colab_runner.main(["run", str(job_path)])
    assert exit_.value.code == 2


def test_main_prints_the_summary_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, capsys: Any
) -> None:
    fake = FakeColab()
    assert _main(monkeypatch, tmp_path, job_path, fake) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["verified"] == "stub-colab@0.0.1"
    assert summary["registered"] is False
    assert summary["balance_before"] == "136.64"
    assert fake.sessions == set()


def test_parse_balance_reads_the_real_usage_output() -> None:
    text = "Current balance: 136.64 compute units\nUsage rate: 0.00/hr\nActive assignments: 0\n"
    assert parse_balance(text) == Decimal("136.64")


def test_an_unreadable_balance_is_refused() -> None:
    with pytest.raises(ColabError, match="balance"):
        parse_balance("Please log in")


def test_the_cli_adapter_uses_an_argv_list_and_never_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("MT5_PASSWORD", "never-forwarded")
    monkeypatch.setenv("ORIONFX_PLAIN", "kept")
    cli = ColabCli(state=tmp_path / "state.json", exe="colab")
    cli.new("orionfx-a", timeout=60)
    cli.exec_file("orionfx-a", Path("driver.py"), timeout=120)
    cli.stop("orionfx-a", timeout=30)

    (new_argv, new_kw), (exec_argv, _), (_, stop_kw) = seen
    assert new_argv == ["colab", "--config", str(tmp_path / "state.json"),
                        "new", "-s", "orionfx-a", "--high-mem"]  # fmt: skip
    assert exec_argv[3:] == ["exec", "-s", "orionfx-a", "-f", "driver.py", "--timeout", "120"]
    assert "shell" not in new_kw
    assert new_kw["timeout"] == 60
    assert new_kw["env"]["PYTHONIOENCODING"] == "utf-8"
    assert "MT5_PASSWORD" not in new_kw["env"]  # secret-named variables never reach colab
    assert new_kw["env"]["ORIONFX_PLAIN"] == "kept"
    # Only the stop runs in its own process group, so a second Ctrl-C cannot kill it.
    if sys.platform == "win32":
        assert stop_kw["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert new_kw["creationflags"] == 0
    else:
        assert stop_kw["start_new_session"] is True
        assert new_kw["start_new_session"] is False


def test_the_cli_adapter_maps_a_timeout_and_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_slow(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    monkeypatch.setattr(subprocess, "run", too_slow)
    cli = ColabCli(state=tmp_path / "s.json", exe="colab")
    with pytest.raises(ColabTimeout):
        cli.stop("orionfx-a", timeout=5)

    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "401")
    )
    with pytest.raises(ColabError, match="401"):
        cli.balance()
