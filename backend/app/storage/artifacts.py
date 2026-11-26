"""Trusted artifact loading and isolated research runs (#20; master plan §15.5, UST-07;
CAT-X04-01).

Loading: only a module under an allowlisted in-repo package may be loaded, only
for a registered (kind, id, version), and only when the sha256 of the exact bytes
being executed equals the registered artifact hash. The verified bytes are the
ones compiled, so the file cannot change between the check and the load. There
is no path, URL or arbitrary-name loading, and nothing is fetched from outside.

Isolation: a research candidate runs in a separate process with a hard time
budget and is killed when it overruns. This bounds runaway work; it is not a
security sandbox, so only reviewed, registered code may be run this way.
"""

import hashlib
import importlib.util
import multiprocessing
import re
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.storage.registry import ArtifactRegistry, Kind

_MODULE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")


class UntrustedArtifact(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class ArtifactLoader:
    def __init__(self, registry: ArtifactRegistry, *, allowed_packages: tuple[str, ...]) -> None:
        self._registry = registry
        self._allowed = allowed_packages

    def _check_name(self, module_name: str) -> None:
        if not _MODULE.fullmatch(module_name) or not any(
            module_name.startswith(f"{pkg}.") for pkg in self._allowed
        ):
            raise UntrustedArtifact(f"{module_name!r} is not under an allowlisted package")

    def load(self, kind: Kind, subject_id: str, version: str, module_name: str) -> types.ModuleType:
        self._check_name(module_name)
        manifest = self._registry.get(kind, subject_id, version)
        if manifest is None:
            raise UntrustedArtifact(f"{kind} {subject_id}@{version} is not registered")
        spec = importlib.util.find_spec(module_name)
        if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
            raise UntrustedArtifact(f"{module_name!r} has no source file")
        origin = Path(spec.origin)
        source = origin.read_bytes()
        digest = f"sha256:{hashlib.sha256(source).hexdigest()}"
        if digest != manifest.artifact_hash:
            raise UntrustedArtifact(
                f"{module_name}: hash {digest} does not match the registered artifact"
            )
        module = types.ModuleType(module_name)
        module.__file__ = str(origin)
        exec(compile(source, str(origin), "exec"), module.__dict__)  # verified bytes only
        return module


class IsolatedRunFailed(Exception):
    pass


def _child(fn: Callable[[Any], Any], arg: Any, out: Any) -> None:
    try:
        out.send(("ok", fn(arg)))
    except Exception as exc:
        out.send(("error", repr(exc)))


def run_isolated[T, R](fn: Callable[[T], R], arg: T, *, timeout_s: float) -> R:
    """Run `fn(arg)` in a child process; kill it past `timeout_s`."""
    ctx = multiprocessing.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child, args=(fn, arg, send), daemon=True)
    proc.start()
    send.close()  # the child holds the only send end: its death reads as EOF here
    try:
        if not recv.poll(timeout_s):
            raise BudgetExceeded(f"research run exceeded {timeout_s}s")
        try:
            status, value = recv.recv()
        except EOFError:
            raise IsolatedRunFailed("research run died without a result") from None
        if status != "ok":
            raise IsolatedRunFailed(f"research run raised {value}")
        result: R = value
        return result
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(timeout=5)
        recv.close()
