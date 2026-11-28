"""The VM side of the Colab runner (#134): the return contract and the driver.

Stdlib only, because this file runs in the Colab kernel's own Python (`colab exec -f`),
before any pinned dependency is installed. Trainers import `write_return` from the
pinned code to produce the return set.

Return contract: the trainer writes exactly `RETURN_FILES` into its output directory.
`manifest.json` echoes the job's binding fields (`BINDING`) and the sha256 of every
other returned file. The runner refuses anything else (#134 local verification).

Driver (`python this_file` in the kernel): unpack the uploaded bundle, unpack the
pinned code, install the locked dependencies with a pinned uv (Python per
`pyproject.toml`), run the job's entrypoint, and tar the output directory as-is, so an
extra or missing file reaches the local check instead of being filtered here. Every
command is an argv list: no shell, and the job's fields are never spliced into one.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

UV_VERSION = "0.11.16"
REMOTE_ROOT = "/content/orionfx"
REMOTE_BUNDLE = "/content/orionfx-bundle.tar"
REMOTE_OUT = "/content/orionfx-out.tar"
REMOTE_LOG = "/content/orionfx-run.log"

MANIFEST = "manifest.json"
CARD = "card.json"
WEIGHTS = "weights.json"
RETURN_FILES = (MANIFEST, CARD, WEIGHTS)
BINDING = (
    "job_id", "protocol_hash", "code_sha", "code_hash", "dataset_hash", "seeds", "config",
    "entrypoint", "model_id", "model_version",
)  # fmt: skip


def sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def binding(job: dict[str, Any]) -> dict[str, Any]:
    """The job fields a returned manifest must echo to be accepted for that job."""
    return {k: job[k] for k in BINDING}


def write_return(job: dict[str, Any], out: Path, files: dict[str, bytes]) -> None:
    """Write `files` (card and weights) and a manifest bound to `job` into `out`."""
    if sorted(files) != sorted(set(RETURN_FILES) - {MANIFEST}):
        raise ValueError(f"a trainer returns exactly {sorted(set(RETURN_FILES) - {MANIFEST})}")
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out / name).write_bytes(data)
    manifest = {
        "job": binding(job),
        "files": {name: sha256(data) for name, data in sorted(files.items())},
        "python": sys.version,
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def pack(out: Path, tar_path: Path) -> None:
    """Tar everything under `out`, unfiltered (links and subdirectories included)."""
    with tarfile.open(tar_path, "w") as tar:
        for path in sorted(out.rglob("*")):
            tar.add(path, arcname=path.relative_to(out).as_posix(), recursive=False)


def main() -> None:
    root = Path(REMOTE_ROOT)
    shutil.rmtree(root, ignore_errors=True)
    Path(REMOTE_OUT).unlink(missing_ok=True)
    job_dir, code, out = root / "job", root / "code", root / "out"
    with tarfile.open(REMOTE_BUNDLE) as tar:
        tar.extractall(job_dir, filter="data")
    job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    with tarfile.open(job_dir / "code.tar") as tar:
        tar.extractall(code, filter="data")
    env = {**os.environ, "PYTHONPATH": str(code / "backend")}
    uv = (sys.executable, "-m", "uv")
    with open(REMOTE_LOG, "w", encoding="utf-8") as log:

        def step(*argv: str, timeout: float | None = None) -> None:
            print("$", *argv, file=log, flush=True)
            subprocess.run(
                argv, cwd=code, env=env, stdout=log, stderr=subprocess.STDOUT,
                timeout=timeout, check=True,
            )  # fmt: skip

        meminfo = Path("/proc/meminfo")
        mem = meminfo.read_text().splitlines()[0] if meminfo.exists() else "unknown"
        print(f"kernel python {sys.version}; cpus {os.cpu_count()}; {mem}", file=log, flush=True)
        step(sys.executable, "-m", "pip", "install", "--quiet", f"uv=={UV_VERSION}")
        step(*uv, "sync", "--frozen", "--no-dev", "--no-install-project")
        step(*uv, "run", "--frozen", "--no-dev", "--no-sync", "python", "-m", job["entrypoint"],
             str(job_dir / "job.json"), str(out), timeout=job["timeout_s"])  # fmt: skip
    pack(out, Path(REMOTE_OUT))
    print("orionfx driver: done")


if __name__ == "__main__":
    main()
