"""Colab runner for M04 training jobs (#134; PRD #120 slice 7).

    uv run python -m app.learning.colab_runner bundle <spec.json> <bundle_dir>
    uv run python -m app.learning.colab_runner run <bundle_dir>/job.json [--repo . --db ...]

`run` order, and what each guard protects:
1. `scan_bundle` again (nothing unscanned leaves the machine); refuse a `code.tar` that is
   not byte-identical to a fresh `git archive` of `code_sha` (the manifest is bound to that
   commit); refuse a model version that is already registered or already on disk (no VM
   spend for an unregistrable run).
2. Compute-unit guard: `colab usage` must parse and show at least `--min-cu` (default
   20). A refusal here is the stop condition to report to the owner.
3. On a fresh CPU high-mem VM: upload the bundle as one tar, run `colab_vm` (pinned uv,
   locked deps, the job's entrypoint), download the run log and the output tar. One hard
   deadline (`JobSpec.timeout_s`) bounds every step.
4. `colab stop` always: in a `finally`, which SIGINT/SIGTERM/SIGBREAK also reach
   (the handler raises `RunInterrupted`). A failed stop raises `VmStopFailed` once `new`
   has completed. If `new` itself failed, its error is kept and carries the stop failure
   as a note (a VM may still have been assigned).
5. Local verification, after the VM is gone: exactly the return files, every sha256
   matching the manifest, the manifest bound to this job, the card a valid `ModelCard`
   for this job naming the weights' hash, the weights plain JSON (never pickle).
6. Copy into `<artifacts>/<model_id>/<version>/`, then register the card's manifest in
   the `ArtifactRegistry` (the store of record), last. Any earlier failure registers
   nothing. A registered stub is still only registered: eligibility is a separate,
   owner-governed record.

A hard kill (Windows TerminateProcess, SIGKILL) cannot be caught: recover with
`colab sessions` and `colab --config <work>/colab-state.json stop -s <session>` (the
session is recorded in that state file, not in the default one).
"""

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import Protocol

from app.core.contracts import sha256_ref
from app.learning import colab_vm as vm
from app.learning.colab_job import (
    BUNDLE_FILES,
    CODE,
    BundleRefused,
    JobSpec,
    build_bundle,
    git_archive,
    known_secret_values,
    scan_bundle,
)
from app.prediction.card import ModelCard
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

ACTOR = "m04.colab_runner"
MIN_CU = Decimal(20)
STOP_TIMEOUT_S = 180.0
STATE_FILE = "colab-state.json"
_BALANCE = re.compile(r"Current balance:\s*([0-9]+(?:\.[0-9]+)?)\s*compute units")


class ColabError(Exception):
    """A `colab` command failed."""


class ColabTimeout(ColabError):
    pass


class RunRefused(Exception):
    """Refused before any VM was started."""


class CuFloorRefused(RunRefused):
    pass


class RunTimeout(Exception):
    pass


class RunInterrupted(BaseException):
    """A signal arrived during the remote run (BaseException: never swallowed)."""


class VmStopFailed(Exception):
    pass


class ReturnRefused(Exception):
    """The returned files failed local verification; nothing was registered."""


def parse_balance(text: str) -> Decimal:
    match = _BALANCE.search(text)
    if match is None:
        raise ColabError("cannot read the compute-unit balance from `colab usage`")
    return Decimal(match.group(1))


class ColabPort(Protocol):
    def balance(self) -> Decimal: ...
    def new(self, session: str, *, timeout: float) -> None: ...
    def upload(self, session: str, local: Path, remote: str, *, timeout: float) -> None: ...
    def exec_file(self, session: str, script: Path, *, timeout: float) -> str: ...
    def download(self, session: str, remote: str, local: Path, *, timeout: float) -> None: ...
    def stop(self, session: str, *, timeout: float) -> None: ...


class ColabCli:
    """The real `colab` CLI: argv lists only, never a shell; isolated session state."""

    def __init__(self, *, state: Path, exe: str | None = None) -> None:
        found = exe or shutil.which("colab")
        if found is None:
            raise ColabError("the colab CLI is not on PATH")
        self._exe = found
        self._state = state

    def _run(self, *args: str, timeout: float) -> str:
        argv = [self._exe, "--config", str(self._state), *args]
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, stdin=subprocess.DEVNULL,
                timeout=timeout,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            raise ColabTimeout(f"colab {args[0]} exceeded {timeout:.0f}s") from None
        if done.returncode != 0:
            tail = (done.stdout + done.stderr).strip()[-2000:]
            raise ColabError(f"colab {args[0]} failed ({done.returncode}): {tail}")
        return done.stdout + done.stderr

    def balance(self) -> Decimal:
        return parse_balance(self._run("usage", timeout=60))

    def new(self, session: str, *, timeout: float) -> None:
        self._run("new", "-s", session, "--high-mem", timeout=timeout)

    def upload(self, session: str, local: Path, remote: str, *, timeout: float) -> None:
        self._run("upload", "-s", session, str(local), remote, timeout=timeout)

    def exec_file(self, session: str, script: Path, *, timeout: float) -> str:
        # `--timeout` is colab's idle-output limit; the hard limit is the local one.
        idle = f"{timeout:.0f}"
        return self._run("exec", "-s", session, "-f", str(script), "--timeout", idle,
                         timeout=timeout)  # fmt: skip

    def download(self, session: str, remote: str, local: Path, *, timeout: float) -> None:
        self._run("download", "-s", session, remote, str(local), timeout=timeout)

    def stop(self, session: str, *, timeout: float) -> None:
        self._run("stop", "-s", session, timeout=timeout)


@contextmanager
def _signals_raise(stopping: Callable[[], bool]) -> Iterator[None]:
    """Turn SIGINT/SIGTERM/SIGBREAK into `RunInterrupted` so the caller's `finally` stops
    the VM; ignored once the stop has begun, so a second Ctrl-C cannot abort it."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: FrameType | None) -> None:
        if not stopping():
            raise RunInterrupted(f"signal {signum}")

    names = ("SIGINT", "SIGTERM", "SIGBREAK")
    sigs = [getattr(signal, n) for n in names if hasattr(signal, n)]
    previous = {s: signal.signal(s, handler) for s in sigs}
    try:
        yield
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def verify_return(out_tar: Path, job: JobSpec, staging: Path) -> ModelCard:
    """Check the returned tar against the return contract; extract it into `staging`."""
    expected = sorted(vm.RETURN_FILES)
    try:
        with tarfile.open(out_tar) as tar:
            members = tar.getmembers()
            names = sorted(m.name for m in members)
            odd = [m.name for m in members if not m.isfile() or m.name not in expected]
            if odd or names != expected:
                raise ReturnRefused(f"returned {names} (unexpected: {odd}); expected {expected}")
            staging.mkdir(parents=True)
            tar.extractall(staging, filter="data")
        files = {name: (staging / name).read_bytes() for name in expected}
        manifest = json.loads(files[vm.MANIFEST])
        bound = vm.binding(job.model_dump(mode="json"))
        if not isinstance(manifest, dict) or manifest.get("job") != bound:
            raise ReturnRefused("the manifest is not bound to this job")
        listed = manifest.get("files")
        if not isinstance(listed, dict) or sorted(listed) != [vm.CARD, vm.WEIGHTS]:
            raise ReturnRefused(f"the manifest lists {listed!r}")
        for name, digest in listed.items():
            if sha256_ref(files[name]) != digest:
                raise ReturnRefused(f"{name}: hash does not match the manifest")
        json.loads(files[vm.WEIGHTS])  # weights stay plain JSON; never unpickled
        try:
            card = ModelCard.model_validate_json(files[vm.CARD])
        except ValueError as exc:
            raise ReturnRefused(f"card.json is not a valid ModelCard: {exc}") from None
    except (tarfile.TarError, ValueError) as exc:
        raise ReturnRefused(f"unreadable return: {exc}") from None
    if (card.model_id, card.model_version) != (job.model_id, job.model_version):
        raise ReturnRefused(f"the card describes another model ({card.ref})")
    if card.artifact_hash != listed[vm.WEIGHTS]:
        raise ReturnRefused("the card's artifact_hash is not the returned weights' hash")
    return card


@dataclass(frozen=True)
class RunResult:
    card: ModelCard
    artifact_dir: Path
    run_dir: Path
    session: str
    balance_before: Decimal
    wall_s: float


class ColabRunner:
    def __init__(
        self,
        port: ColabPort,
        registry: ArtifactRegistry,
        *,
        artifacts: Path,
        work: Path,
        archive: Callable[[str], bytes],
        min_cu: Decimal = MIN_CU,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._registry = registry
        self._artifacts = artifacts
        self._work = work
        self._archive = archive
        self._min_cu = min_cu
        self._clock = clock

    def run(self, job_path: Path) -> RunResult:
        start = self._clock()
        bundle = job_path.parent
        job = scan_bundle(bundle, known_secrets=known_secret_values())
        if (bundle / CODE).read_bytes() != self._archive(job.code_sha):
            raise BundleRefused(f"{CODE} is not the git archive of code_sha {job.code_sha}")
        target = self._artifacts / job.model_id / job.model_version
        registered = self._registry.get("model", job.model_id, job.model_version)
        if target.exists() or registered is not None:
            raise RunRefused(f"model {job.model_id}@{job.model_version} already exists")
        balance = self._port.balance()
        if balance < self._min_cu:
            raise CuFloorRefused(
                f"compute-unit balance {balance} is below the floor {self._min_cu}"
            )
        session = f"orionfx-{job.job_id}-{secrets.token_hex(3)}"
        run_dir = self._work / session
        run_dir.mkdir(parents=True)
        with tarfile.open(run_dir / "bundle.tar", "w") as tar:
            for name in BUNDLE_FILES:
                tar.add(bundle / name, arcname=name, recursive=False)
        self._remote(session, run_dir, deadline=start + job.timeout_s, limit_s=job.timeout_s)

        card = verify_return(run_dir / "out.tar", job, run_dir / "staging")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run_dir / "staging", target)
        try:
            self._registry.register("model", card.manifest(), actor=ACTOR)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return RunResult(card, target, run_dir, session, balance, self._clock() - start)

    def _remote(self, session: str, run_dir: Path, *, deadline: float, limit_s: int) -> None:
        def left() -> float:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RunTimeout(f"{session}: the hard timeout of {limit_s}s passed")
            return remaining

        stopping = False
        started = False
        primary: BaseException | None = None
        with _signals_raise(lambda: stopping):
            try:
                self._port.new(session, timeout=left())
                started = True
                self._port.upload(session, run_dir / "bundle.tar", vm.REMOTE_BUNDLE,
                                  timeout=left())  # fmt: skip
                try:
                    log = self._port.exec_file(session, Path(vm.__file__), timeout=left())
                    (run_dir / "exec.log").write_text(log, encoding="utf-8")
                finally:
                    if deadline > self._clock():  # best effort: evidence, never verified
                        with suppress(ColabError, RunTimeout):
                            self._port.download(session, vm.REMOTE_LOG, run_dir / "run.log",
                                                timeout=left())  # fmt: skip
                self._port.download(session, vm.REMOTE_OUT, run_dir / "out.tar", timeout=left())
            except BaseException as exc:
                primary = exc
                raise
            finally:
                stopping = True
                try:
                    self._port.stop(session, timeout=STOP_TIMEOUT_S)
                except Exception as stop_error:
                    hint = (
                        f"colab stop failed for {session}; the VM may still be billing:"
                        f" check `colab sessions` and run"
                        f" `colab --config {self._work / STATE_FILE} stop -s {session}`"
                    )
                    if started or primary is None:
                        raise VmStopFailed(hint) from stop_error
                    # `new` never completed: keep its error (the real cause), with the hint.
                    primary.add_note(f"{hint} ({stop_error})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.learning.colab_runner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bundle", help="build a scanned bundle from a job spec")
    b.add_argument("spec", type=Path)
    b.add_argument("out", type=Path)
    b.add_argument("--repo", type=Path, default=Path("."))
    r = sub.add_parser("run", help="run a bundled job on Colab and register its artifact")
    r.add_argument("job", type=Path)
    r.add_argument("--repo", type=Path, default=Path("."))
    r.add_argument("--db", type=Path, default=Path("data/orionfx.db"))
    r.add_argument("--artifacts", type=Path, default=Path("models"))
    r.add_argument("--work", type=Path, default=Path("data/colab_runs"))
    r.add_argument("--min-cu", type=Decimal, default=MIN_CU)
    args = parser.parse_args(argv)

    if args.cmd == "bundle":
        job = JobSpec.model_validate_json(args.spec.read_bytes())
        print(build_bundle(job, repo=args.repo, out=args.out))
        return 0

    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    migrate(conn)
    cli = ColabCli(state=args.work / STATE_FILE)
    runner = ColabRunner(cli, ArtifactRegistry(conn), artifacts=args.artifacts,
                         work=args.work, archive=lambda sha: git_archive(args.repo, sha),
                         min_cu=args.min_cu)  # fmt: skip
    try:
        result = runner.run(args.job)
    except CuFloorRefused as exc:
        print(f"REFUSED (stop and report to the owner): {exc}", file=sys.stderr)
        return 3
    except (RunRefused, ReturnRefused, BundleRefused) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    after = None
    with suppress(ColabError):
        after = str(cli.balance())
    summary = {
        "registered": result.card.ref,
        "artifact_hash": result.card.artifact_hash,
        "artifact_dir": str(result.artifact_dir),
        "run_dir": str(result.run_dir),
        "session": result.session,
        "balance_before": str(result.balance_before),
        "balance_after": after,
        "wall_s": round(result.wall_s, 1),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
