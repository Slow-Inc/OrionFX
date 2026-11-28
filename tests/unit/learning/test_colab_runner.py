"""Colab runner (#134; PRD #120 slice 7): guards in front of the VM, the stop that always
runs, and the local verification that decides whether anything is registered."""

import io
import json
import signal
import sqlite3
import subprocess
import tarfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tests.support.colab import FakeColab

from app.core.contracts import sha256_ref
from app.learning import colab_runner
from app.learning import colab_vm as vm
from app.learning.colab_job import CODE, DATASET, JOB, BundleRefused, JobSpec
from app.learning.colab_runner import (
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
    return ColabRunner(
        fake, ArtifactRegistry(conn), artifacts=tmp_path / "models", work=tmp_path / "runs", **kw
    )


def _nothing_registered(conn: sqlite3.Connection, tmp_path: Path) -> None:
    assert ArtifactRegistry(conn).get("model", "stub-colab", "0.0.1") is None
    assert not (tmp_path / "models" / "stub-colab" / "0.0.1").exists()


def test_happy_path_registers_the_verified_artifact_and_stops_the_vm(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab()
    result = _runner(fake, conn, tmp_path).run(job_path)

    assert fake.calls == HAPPY_CALLS
    assert fake.sessions == set()  # stopped
    target = tmp_path / "models" / "stub-colab" / "0.0.1"
    assert result.artifact_dir == target
    assert sorted(p.name for p in target.iterdir()) == sorted(vm.RETURN_FILES)
    registered = ArtifactRegistry(conn).get("model", "stub-colab", "0.0.1")
    assert registered is not None
    assert registered.artifact_hash == sha256_ref((target / vm.WEIGHTS).read_bytes())
    assert result.card.calibrated is False
    assert (result.run_dir / "run.log").read_bytes() == b"fake driver log\n"


def test_the_cu_floor_refuses_before_any_vm_starts(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(balance=Decimal("19.99"))
    with pytest.raises(CuFloorRefused, match=r"19\.99.*20"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls == ["usage"]  # no `new`, so nothing to stop
    _nothing_registered(conn, tmp_path)


def test_the_cu_floor_is_configurable(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(balance=Decimal("30"))
    with pytest.raises(CuFloorRefused):
        _runner(fake, conn, tmp_path, min_cu=Decimal("50")).run(job_path)


def test_an_already_registered_version_is_refused_before_spending(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    _runner(FakeColab(), conn, tmp_path).run(job_path)
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
    _nothing_registered(conn, tmp_path)


def test_an_artifact_directory_already_on_disk_is_refused_before_spending(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    (tmp_path / "models" / "stub-colab" / "0.0.1").mkdir(parents=True)
    fake = FakeColab()
    with pytest.raises(RunRefused, match="already"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls == []
    assert ArtifactRegistry(conn).get("model", "stub-colab", "0.0.1") is None


@pytest.mark.parametrize("step", ["new", "upload", "exec"])
def test_a_colab_failure_still_stops_the_vm_and_registers_nothing(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, step: str
) -> None:
    fake = FakeColab(fail={step: ColabError(f"{step} broke")})
    with pytest.raises(ColabError, match="broke"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_registered(conn, tmp_path)


def test_a_failed_trainer_returns_no_output_and_registers_nothing(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    def crash(fake: FakeColab) -> None:
        fake.remote[vm.REMOTE_LOG] = b"Traceback: boom\n"  # no out.tar

    fake = FakeColab(on_exec=crash)
    with pytest.raises(ColabError, match=r"orionfx-out.tar"):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_registered(conn, tmp_path)


def test_a_colab_timeout_stops_the_vm(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(fail={"exec": ColabTimeout("exec exceeded 600s")})
    with pytest.raises(ColabTimeout):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_registered(conn, tmp_path)


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
    _nothing_registered(conn, tmp_path)


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
    _nothing_registered(conn, tmp_path)


def test_a_failed_stop_is_loud(conn: sqlite3.Connection, tmp_path: Path, job_path: Path) -> None:
    fake = FakeColab(fail={"stop": ColabError("stop broke")})
    with pytest.raises(VmStopFailed, match=r"may still be billing.*--config .*colab-state\.json"):
        _runner(fake, conn, tmp_path).run(job_path)
    _nothing_registered(conn, tmp_path)


def test_a_failed_stop_after_a_started_vm_is_loud_even_when_a_step_failed(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(fail={"exec": ColabError("exec broke"), "stop": ColabError("stop broke")})
    with pytest.raises(VmStopFailed, match="may still be billing") as err:
        _runner(fake, conn, tmp_path).run(job_path)
    assert isinstance(err.value.__context__, ColabError)  # the exec failure is chained
    _nothing_registered(conn, tmp_path)


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
    _nothing_registered(conn, tmp_path)


def _edit_json(path: Path, **changes: Any) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        (lambda out: (out / vm.WEIGHTS).write_text('{"kind":"swapped"}'), "hash"),
        (lambda out: (out / "notes.txt").write_text("hi"), "notes.txt"),
        (lambda out: (out / vm.CARD).unlink(), "card.json"),
        (lambda out: (out / "sub").mkdir(), "sub"),
        (lambda out: _edit_json(out / vm.MANIFEST, job={"job_id": "other"}), "bound"),
        (lambda out: _rehash_card(out, horizon_bars=0), "not a valid ModelCard"),
        (lambda out: (out / vm.MANIFEST).write_text("[]"), "not bound"),
        (lambda out: _manifest_files(out, extra="sha256:" + "0" * 64), "the manifest lists"),
        (lambda out: _rehash_card(out, artifact_hash="sha256:" + "0" * 64), "artifact_hash"),
    ],
    ids=[
        "hash-mismatch",
        "extra-file",
        "missing-file",
        "directory",
        "unbound",
        "bad-card",
        "manifest-not-an-object",
        "manifest-files-extra-key",
        "card-names-other-weights",
    ],
)
def test_a_return_that_fails_verification_is_refused_and_nothing_is_registered(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path, tamper: Any, reason: str
) -> None:
    fake = FakeColab(tamper=tamper)
    with pytest.raises(ReturnRefused, match=reason):
        _runner(fake, conn, tmp_path).run(job_path)
    assert fake.calls[-1] == "stop"
    _nothing_registered(conn, tmp_path)


def test_a_card_for_another_model_is_refused(
    conn: sqlite3.Connection, tmp_path: Path, job_path: Path
) -> None:
    fake = FakeColab(tamper=lambda out: _rehash_card(out, model_id="other"))
    with pytest.raises(ReturnRefused, match="another model"):
        _runner(fake, conn, tmp_path).run(job_path)
    _nothing_registered(conn, tmp_path)


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


def _main(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, fake: FakeColab, *extra: str
) -> int:
    monkeypatch.setattr(colab_runner, "ColabCli", lambda **kw: fake)
    monkeypatch.setattr(colab_runner, "git_archive", lambda repo, sha: _same_code(tmp_path)(sha))
    return colab_runner.main(
        ["run", str(job_path), "--db", str(tmp_path / "cli.db"),
         "--artifacts", str(tmp_path / "models"), "--work", str(tmp_path / "runs"), *extra]
    )  # fmt: skip


def test_main_exits_3_on_the_cu_floor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, capsys: Any
) -> None:
    fake = FakeColab()
    assert _main(monkeypatch, tmp_path, job_path, fake, "--min-cu", "1000") == 3
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


def test_main_prints_the_summary_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_path: Path, capsys: Any
) -> None:
    fake = FakeColab()
    assert _main(monkeypatch, tmp_path, job_path, fake) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["registered"] == "stub-colab@0.0.1"
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
    cli = ColabCli(state=tmp_path / "state.json", exe="colab")
    cli.new("orionfx-a", timeout=60)
    cli.exec_file("orionfx-a", Path("driver.py"), timeout=120)

    (new_argv, new_kw), (exec_argv, _) = seen
    assert new_argv == ["colab", "--config", str(tmp_path / "state.json"),
                        "new", "-s", "orionfx-a", "--high-mem"]  # fmt: skip
    assert exec_argv[3:] == ["exec", "-s", "orionfx-a", "-f", "driver.py", "--timeout", "120"]
    assert "shell" not in new_kw
    assert new_kw["timeout"] == 60
    assert new_kw["env"]["PYTHONIOENCODING"] == "utf-8"


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
