"""Trusted artifact loading and isolated research runs (#20; master plan §15.5, UST-07;
CAT-X04-01).

Loading: only a module under an allowlisted in-repo package, only for a registered
(kind, id, version), and only when the sha256 of the exact bytes being executed
equals the registered hash. The file is located from the package directory
without importing anything (no parent `__init__` runs first), and the verified
bytes are the ones compiled, so nothing changes between the check and the load.
There is no path, URL or arbitrary-name loading. `load` also requires a current
eligibility record approving the requested mode; research use is the separate,
explicit `load_for_research`. Imports made by the artifact itself run normally:
only the entry file is hashed, so allowlisted packages hold reviewed code only.

Isolation: a research candidate runs in a separate process with a hard time
budget and is killed when it overruns. This bounds runaway work; it is not a
security sandbox. The callable must be importable by name in the child (a
function returned by `load` is not, and pickling it fails rather than running
an unverified copy).
"""

import importlib.util
import multiprocessing
import re
import types
from collections.abc import Callable
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, cast

from app.core.contracts import SubjectKind, sha256_ref
from app.core.enums import Mode
from app.storage.registry import ArtifactRegistry, subject_ref

_MODULE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


class UntrustedArtifact(Exception):
    pass


class IsolatedRunError(Exception):
    pass


class BudgetExceeded(IsolatedRunError):
    pass


class IsolatedRunFailed(IsolatedRunError):
    pass


class ArtifactLoader:
    def __init__(self, registry: ArtifactRegistry, *, allowed_packages: tuple[str, ...]) -> None:
        self._registry = registry
        self._allowed = allowed_packages

    def _source_file(self, module_name: str) -> Path:
        package = next((p for p in self._allowed if module_name.startswith(f"{p}.")), None)
        if not _MODULE.fullmatch(module_name) or package is None:
            raise UntrustedArtifact(f"{module_name!r} is not under an allowlisted package")
        spec = importlib.util.find_spec(package)  # top-level: located, not executed
        roots = None if spec is None else spec.submodule_search_locations
        if not roots:
            raise UntrustedArtifact(f"allowlisted package {package!r} is not installed")
        rest = module_name[len(package) + 1 :].split(".")
        root = Path(next(iter(roots))).resolve()
        path = root.joinpath(*rest[:-1], f"{rest[-1]}.py").resolve()
        if root not in path.parents or not path.is_file():
            raise UntrustedArtifact(f"{module_name!r} has no source file in {package!r}")
        return path

    def _load(
        self, kind: SubjectKind, subject_id: str, version: str, module_name: str
    ) -> types.ModuleType:
        path = self._source_file(module_name)
        manifest = self._registry.get(kind, subject_id, version)
        if manifest is None:
            raise UntrustedArtifact(f"{kind} {subject_id}@{version} is not registered")
        source = path.read_bytes()
        digest = sha256_ref(source)
        if digest != manifest.artifact_hash:
            raise UntrustedArtifact(
                f"{module_name}: hash {digest} does not match the registered artifact"
            )
        module = types.ModuleType(module_name)
        module.__file__ = str(path)
        module.__package__ = module_name.rpartition(".")[0]
        exec(compile(source, str(path), "exec"), module.__dict__)  # verified bytes only
        return module

    def load(
        self, kind: SubjectKind, subject_id: str, version: str, module_name: str, *, mode: Mode
    ) -> types.ModuleType:
        """For trading use: needs a current eligibility record that approves `mode`."""
        record = self._registry.eligibility(kind, subject_ref(subject_id, version))
        # A DISABLED or revoked record approves no modes (EligibilityRecord enforces it).
        if record is None or mode not in record.approved_modes:
            raise UntrustedArtifact(f"{kind} {subject_id}@{version} is not eligible for {mode}")
        return self._load(kind, subject_id, version, module_name)

    def load_for_research(
        self, kind: SubjectKind, subject_id: str, version: str, module_name: str
    ) -> types.ModuleType:
        """Registered and hash-verified, but not approved for any trading mode."""
        return self._load(kind, subject_id, version, module_name)


def _child(fn: Callable[[Any], Any], arg: Any, out: Connection) -> None:
    try:
        out.send(("ok", fn(arg)))
    except Exception as exc:
        out.send(("error", repr(exc)))


def run_isolated[T, R](fn: Callable[[T], R], arg: T, *, timeout_s: float) -> R:
    """Run `fn(arg)` in a child process; kill it when the result is not back within
    `timeout_s` of the child starting."""
    ctx = multiprocessing.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(fn, arg, send), daemon=True)
    try:
        proc.start()
        send.close()  # the child holds the only send end: its death reads as EOF here
        if not recv.poll(timeout_s):
            raise BudgetExceeded(f"research run exceeded {timeout_s}s")
        try:
            status, value = recv.recv()
        except EOFError:
            raise IsolatedRunFailed("research run died without a result") from None
        if status != "ok":
            raise IsolatedRunFailed(f"research run raised {value}")
        return cast(R, value)
    finally:
        if proc.is_alive():
            proc.kill()
        if proc.pid is not None:
            proc.join(timeout=5)
        send.close()
        recv.close()
