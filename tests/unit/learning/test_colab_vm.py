"""The VM driver of the Colab runner (#134): unpack, install, run the entrypoint, pack."""

import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from app.core.contracts import sha256_ref
from app.learning import colab_vm as vm
from app.learning.colab_job import CODE, DATASET, JOB, JobSpec
from app.learning.colab_stub import train


def _bundle_tar(path: Path) -> None:
    data = b'{"split":"train","x":1}\n'
    code = io.BytesIO()
    with tarfile.open(fileobj=code, mode="w") as tar:
        tar.addfile(tarfile.TarInfo("backend/app/__init__.py"), io.BytesIO(b""))
    job = JobSpec(
        job_id="smoke-1", protocol_hash=sha256_ref(b"p"), config={}, dataset_path=DATASET,
        dataset_hash=sha256_ref(data), code_sha="0" * 40, code_hash=sha256_ref(code.getvalue()),
        seeds=[7], model_id="stub-colab", model_version="0.0.1",
    )  # fmt: skip
    files = {JOB: job.model_dump_json().encode(), DATASET: data, CODE: code.getvalue()}
    with tarfile.open(path, "w") as tar:
        for name, blob in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))


def test_the_driver_installs_runs_the_entrypoint_and_packs_the_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "REMOTE_ROOT": tmp_path / "root", "REMOTE_BUNDLE": tmp_path / "bundle.tar",
        "REMOTE_OUT": tmp_path / "out.tar", "REMOTE_LOG": tmp_path / "run.log",
    }.items():  # fmt: skip
        monkeypatch.setattr(vm, name, str(value))
    _bundle_tar(tmp_path / "bundle.tar")
    steps: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        steps.append(list(argv))
        assert "shell" not in kw and kw["check"] is True
        if "app.learning.colab_stub" in argv:
            train(Path(argv[-2]), Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(vm.subprocess, "run", fake_run)
    vm.main()

    assert [s[1:4] for s in steps[:1]] == [["-m", "pip", "install"]]
    assert f"uv=={vm.UV_VERSION}" in steps[0]
    assert steps[1][3:] == ["sync", "--frozen", "--no-dev", "--no-install-project"]
    assert "app.learning.colab_stub" in steps[2]
    with tarfile.open(tmp_path / "out.tar") as tar:
        assert sorted(tar.getnames()) == sorted(vm.RETURN_FILES)
        f = tar.extractfile(vm.MANIFEST)
        assert f is not None
        manifest = json.loads(f.read())
    assert manifest["job"]["model_id"] == "stub-colab"
    assert "kernel python" in (tmp_path / "run.log").read_text(encoding="utf-8")
