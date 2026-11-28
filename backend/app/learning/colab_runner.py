"""Colab runner for M04 training jobs (#134; PRD #120 slice 7).

    uv run python -m app.learning.colab_runner bundle <spec.json> <bundle_dir> [--exports ...]
    uv run python -m app.learning.colab_runner run <bundle_dir>/job.json --db <db> [--repo .]

`run` order, and what each guard protects:
1. `scan_files` again, with the local secrets (secret-named environment variables and
   every `<repo>/.env` value); only the scanned bytes are uploaded. Refuse a `code.tar`
   that is not byte-identical to a fresh `git archive` of `code_sha`: this proves the
   *uploaded* code is the pinned commit, not that the VM ran it. Refuse a model version
   that is already registered or already on disk (no VM spend for a run we cannot keep).
2. Compute-unit guard: `colab usage` must parse and show at least `--min-cu` (default
   and minimum 20). A refusal here is the stop condition to report to the owner.
3. On a fresh CPU high-mem VM: upload the bundle as one tar, run `colab_vm` (pinned uv,
   locked deps, the job's entrypoint), download the run log and the output tar. One hard
   deadline (`JobSpec.timeout_s`) bounds every step.
4. `colab stop` always: in a `finally`, which SIGINT/SIGTERM/SIGBREAK also reach (the
   handler raises `RunInterrupted`). The stop runs in its own process group, is retried
   once, and a signal that arrives during it is held, then raised as `RunInterrupted`
   once the stop is done. A failed stop raises `VmStopFailed` once `new` has completed;
   if `new` itself failed, its error is kept and carries the stop failure as a note.
5. Local verification, after the VM is gone: a plain tar under the size cap holding
   exactly the regular return files; every sha256 matching the manifest; the manifest
   bound to this job; the card a valid `ModelCard` for this job naming the weights' hash
   and claiming nothing the VM cannot prove (no LIVE mode, no calibration, no evidence,
   no validation metrics); the weights plain JSON (never pickle).
6. Copy into `<artifacts>/<model_id>/<version>/` with a local `run.json` record. Nothing
   is registered: the registry hash must be what the trusted adapter loads (#130), so
   registration is #130's step (`RunResult` and `run.json` are its inputs). Any failure
   removes the copy.

A hard kill (Windows TerminateProcess, SIGKILL) cannot be caught: recover with
`colab sessions` and `colab --config <work>/colab-state.json stop -s <session>` (the
session is recorded in that state file, not in the default one).
"""

import argparse
import io
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
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import FrameType
from typing import Any, Protocol

from app.core.contracts import sha256_ref
from app.core.enums import Mode
from app.core.secrets import SECRET_KEY
from app.learning import colab_vm as vm
from app.learning.colab_job import (
    BUNDLE_FILES,
    CODE,
    BundleRefused,
    JobSpec,
    build_bundle,
    git_archive,
    known_secret_values,
    scan_files,
)
from app.prediction.card import ModelCard
from app.storage.db import connect, migrate
from app.storage.registry import ArtifactRegistry

MIN_CU = Decimal(20)
STOP_TIMEOUT_S = 180.0
STATE_FILE = "colab-state.json"
RUN_RECORD = "run.json"
MAX_RETURN_BYTES = 256 * 1024 * 1024
"""Cap on the downloaded `out.tar`, checked after the download and before it is opened."""
MAX_MEMBER_BYTES = 128 * 1024 * 1024
"""Cap on each returned file, read from its tar header before anything is extracted."""
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
    """The returned files failed local verification; nothing was kept."""


def parse_balance(text: str) -> Decimal:
    match = _BALANCE.search(text)
    if match is None:
        raise ColabError("cannot read the compute-unit balance from `colab usage`")
    return Decimal(match.group(1))


def check_min_cu(value: Decimal) -> Decimal:
    if not value.is_finite() or value < MIN_CU:
        raise ValueError(f"min_cu must be a finite number of at least {MIN_CU}, not {value}")
    return value


class ColabPort(Protocol):
    def balance(self) -> Decimal: ...
    def new(self, session: str, *, timeout: float) -> None: ...
    def upload(self, session: str, local: Path, remote: str, *, timeout: float) -> None: ...
    def exec_file(self, session: str, script: Path, *, timeout: float) -> str: ...
    def download(self, session: str, remote: str, local: Path, *, timeout: float) -> None: ...
    def stop(self, session: str, *, timeout: float) -> None: ...


class ColabCli:
    """The real `colab` CLI: argv lists only, never a shell; isolated session state; no
    secret-named environment variable is forwarded."""

    def __init__(self, *, state: Path, exe: str | None = None) -> None:
        found = exe or shutil.which("colab")
        if found is None:
            raise ColabError("the colab CLI is not on PATH")
        self._exe = found
        self._state = state

    def _run(self, *args: str, timeout: float, own_group: bool = False) -> str:
        argv = [self._exe, "--config", str(self._state), *args]
        env = {k: v for k, v in os.environ.items() if not SECRET_KEY.search(k)}
        flags, new_session = 0, False
        if own_group:  # a console Ctrl-C does not reach a separate process group
            if sys.platform == "win32":
                flags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                new_session = True
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                env={**env, "PYTHONIOENCODING": "utf-8"}, stdin=subprocess.DEVNULL,
                timeout=timeout, creationflags=flags, start_new_session=new_session,
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
        self._run("stop", "-s", session, timeout=timeout, own_group=True)


@contextmanager
def _signals_raise(stopping: Callable[[], bool], held: list[int]) -> Iterator[None]:
    """Turn SIGINT/SIGTERM/SIGBREAK into `RunInterrupted` so the caller's `finally` stops
    the VM. Once the stop has begun, a signal is held in `held` instead, so a second
    Ctrl-C cannot abort the stop; the caller raises it afterwards."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: FrameType | None) -> None:
        if stopping():
            held.append(signum)
        else:
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
    """Check the returned tar against the return contract; extract it into `staging`.
    The returned card is forced to SHADOW only."""
    expected = sorted(vm.RETURN_FILES)
    size = out_tar.stat().st_size
    if size > MAX_RETURN_BYTES:
        raise ReturnRefused(f"the return is {size} bytes, over the size cap {MAX_RETURN_BYTES}")
    try:
        with tarfile.open(out_tar, "r:") as tar:  # plain tar only: never decompress
            members = tar.getmembers()
            names = sorted(m.name for m in members)
            odd = [m.name for m in members
                   if m.type != tarfile.REGTYPE or m.name not in expected
                   or m.size > MAX_MEMBER_BYTES]  # fmt: skip
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
        except (ValueError, RecursionError) as exc:
            raise ReturnRefused(f"card.json is not a valid ModelCard: {exc}") from None
    except (tarfile.TarError, ValueError, RecursionError) as exc:
        raise ReturnRefused(f"unreadable return: {exc}") from None
    if (card.model_id, card.model_version) != (job.model_id, job.model_version):
        raise ReturnRefused(f"the card describes another model ({card.ref})")
    if card.artifact_hash != listed[vm.WEIGHTS]:
        raise ReturnRefused("the card's artifact_hash is not the returned weights' hash")
    # The VM is untrusted: it may not claim what only local, trusted code can establish.
    if Mode.LIVE in card.supported_modes:
        raise ReturnRefused("the card claims LIVE; a returned model is SHADOW only")
    if card.calibrated:
        raise ReturnRefused("the card claims calibrated; calibration is established locally")
    if card.evidence_refs:
        raise ReturnRefused("the card carries evidence_refs; evidence is recorded locally")
    if card.validation:
        raise ReturnRefused("the card carries validation metrics; they are recomputed locally")
    return card.model_copy(update={"supported_modes": [Mode.SHADOW]})


@dataclass(frozen=True)
class RunResult:
    """A verified, not yet registered artifact: the input #130's registration takes."""

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
        known_secrets: Mapping[str, str],
        min_cu: Decimal = MIN_CU,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._port = port
        self._registry = registry
        self._artifacts = artifacts
        self._work = work
        self._archive = archive
        self._known = dict(known_secrets)
        self._min_cu = check_min_cu(min_cu)
        self._clock = clock

    def run(self, job_path: Path) -> RunResult:
        start = self._clock()
        job, files = scan_files(job_path.parent, known_secrets=self._known)
        if files[CODE] != self._archive(job.code_sha):
            raise BundleRefused(f"{CODE} is not the git archive of code_sha {job.code_sha}")
        target = self._artifacts / job.model_id / job.model_version
        registered = self._registry.get("model", job.model_id, job.model_version)
        if target.exists() or registered is not None:
            raise RunRefused(f"model {job.model_id}@{job.model_version} already exists")
        try:
            balance = self._port.balance()
        except ColabError as exc:
            raise CuFloorRefused(f"cannot confirm the compute-unit floor: {exc}") from None
        if balance < self._min_cu:
            raise CuFloorRefused(
                f"compute-unit balance {balance} is below the floor {self._min_cu}"
            )
        session = f"orionfx-{job.job_id}-{secrets.token_hex(3)}"
        run_dir = self._work / session
        run_dir.mkdir(parents=True)
        with tarfile.open(run_dir / "bundle.tar", "w") as tar:  # the scanned bytes only
            for name in BUNDLE_FILES:
                info = tarfile.TarInfo(name)
                info.size = len(files[name])
                tar.addfile(info, io.BytesIO(files[name]))
        self._remote(session, run_dir, deadline=start + job.timeout_s, limit_s=job.timeout_s)

        card = verify_return(run_dir / "out.tar", job, run_dir / "staging")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(run_dir / "staging", target)
            (target / RUN_RECORD).write_text(
                json.dumps(self._record(job, card, target, session, balance), indent=2),
                encoding="utf-8",
            )
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return RunResult(card, target, run_dir, session, balance, self._clock() - start)

    @staticmethod
    def _record(
        job: JobSpec, card: ModelCard, target: Path, session: str, balance: Decimal
    ) -> dict[str, Any]:
        """Written by trusted local code, after verification, over the copied files."""
        return {
            "job": job.model_dump(mode="json"),
            "files": {n: sha256_ref((target / n).read_bytes()) for n in sorted(vm.RETURN_FILES)},
            "supported_modes": [m.value for m in card.supported_modes],
            "session": session,
            "balance_before": str(balance),
            "verified_at": datetime.now(UTC).isoformat(),
            "registered": False,
            "registration": "pending: #130 registers through the trusted adapter path",
        }

    def _stop(self, session: str) -> Exception | None:
        """Stop the VM, retrying once; return the last error, if any."""
        error: Exception | None = None
        for _ in range(2):
            try:
                self._port.stop(session, timeout=STOP_TIMEOUT_S)
                return None
            except Exception as exc:
                error = exc
        return error

    def _remote(self, session: str, run_dir: Path, *, deadline: float, limit_s: int) -> None:
        def left() -> float:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise RunTimeout(f"{session}: the hard timeout of {limit_s}s passed")
            return remaining

        stopping = False
        started = False
        primary: BaseException | None = None
        held: list[int] = []
        with _signals_raise(lambda: stopping, held):
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
                stop_error = self._stop(session)
                hint = (
                    f"colab stop failed for {session}; the VM may still be billing:"
                    f" check `colab sessions` and run"
                    f" `colab --config {self._work / STATE_FILE} stop -s {session}`"
                )
                if stop_error is None:
                    if held and primary is None:
                        raise RunInterrupted(f"signal {held[0]} during colab stop; VM stopped")
                elif not started and primary is not None:
                    # `new` never completed: keep its error (the real cause), with the hint.
                    primary.add_note(f"{hint} ({stop_error})")
                else:
                    raise VmStopFailed(hint) from stop_error


def _min_cu_arg(text: str) -> Decimal:
    try:
        return check_min_cu(Decimal(text))
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.learning.colab_runner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bundle", help="build a scanned bundle from a job spec")
    b.add_argument("spec", type=Path)
    b.add_argument("out", type=Path)
    b.add_argument("--repo", type=Path, default=Path("."))
    b.add_argument("--exports", type=Path, default=Path("data/exports"))
    r = sub.add_parser("run", help="run a bundled job on Colab and keep its verified artifact")
    r.add_argument("job", type=Path)
    r.add_argument("--db", type=Path, required=True)
    r.add_argument("--repo", type=Path, default=Path("."))
    r.add_argument("--artifacts", type=Path, default=Path("models"))
    r.add_argument("--work", type=Path, default=Path("data/colab_runs"))
    r.add_argument("--min-cu", type=_min_cu_arg, default=MIN_CU)
    args = parser.parse_args(argv)
    known = known_secret_values(dotenv=args.repo / ".env")

    if args.cmd == "bundle":
        job = JobSpec.model_validate_json(args.spec.read_bytes())
        try:
            print(build_bundle(job, repo=args.repo, out=args.out, exports=args.exports,
                               known_secrets=known))  # fmt: skip
        except BundleRefused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        return 0

    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    try:
        migrate(conn)
        cli = ColabCli(state=args.work / STATE_FILE)
        runner = ColabRunner(cli, ArtifactRegistry(conn), artifacts=args.artifacts,
                             work=args.work, archive=lambda sha: git_archive(args.repo, sha),
                             known_secrets=known, min_cu=args.min_cu)  # fmt: skip
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
        "verified": result.card.ref,
        "registered": False,
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
