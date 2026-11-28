"""In-memory fake of the `colab` CLI port (#134). The default exec emulates the VM
driver: it unpacks the uploaded bundle, runs the real stub trainer and tars its output."""

import tarfile
import tempfile
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from app.learning import colab_vm as vm
from app.learning.colab_runner import ColabError
from app.learning.colab_stub import train


def run_stub(fake: "FakeColab") -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "bundle.tar").write_bytes(fake.remote[vm.REMOTE_BUNDLE])
        with tarfile.open(root / "bundle.tar") as tar:
            tar.extractall(root / "job", filter="data")
        train(root / "job" / "job.json", root / "out")
        if fake.tamper is not None:
            fake.tamper(root / "out")
        vm.pack(root / "out", root / "out.tar")
        fake.remote[vm.REMOTE_OUT] = (root / "out.tar").read_bytes()
        fake.remote[vm.REMOTE_LOG] = b"fake driver log\n"


class FakeColab:
    def __init__(
        self,
        *,
        balance: Decimal = Decimal("136.64"),
        fail: dict[str, Exception] | None = None,
        fail_once: dict[str, Exception] | None = None,
        on_exec: Callable[["FakeColab"], None] = run_stub,
        on_stop: Callable[["FakeColab"], None] | None = None,
        tamper: Callable[[Path], None] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.remote: dict[str, bytes] = {}
        self.sessions: set[str] = set()
        self._balance = balance
        self._fail = fail or {}
        self._fail_once = dict(fail_once or {})
        self._on_exec = on_exec
        self._on_stop = on_stop
        self.tamper = tamper

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self._fail:
            raise self._fail[name]
        if name in self._fail_once:
            raise self._fail_once.pop(name)

    def balance(self) -> Decimal:
        self._call("usage")
        return self._balance

    def new(self, session: str, *, timeout: float) -> None:
        self._call("new")
        self.sessions.add(session)

    def upload(self, session: str, local: Path, remote: str, *, timeout: float) -> None:
        self._call("upload")
        self.remote[remote] = local.read_bytes()

    def exec_file(self, session: str, script: Path, *, timeout: float) -> str:
        self._call("exec")
        self._on_exec(self)
        return "orionfx driver: done\n"

    def download(self, session: str, remote: str, local: Path, *, timeout: float) -> None:
        self._call(f"download:{Path(remote).name}")
        if remote not in self.remote:
            raise ColabError(f"no such remote file {remote}")
        local.write_bytes(self.remote[remote])

    def stop(self, session: str, *, timeout: float) -> None:
        self._call("stop")
        if self._on_stop is not None:
            self._on_stop(self)
        self.sessions.discard(session)
