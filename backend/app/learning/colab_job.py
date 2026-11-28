"""Training job spec and the bundle that goes to a Colab VM (#134; PRD #120 slice 7).

A `JobSpec` pins everything a training run depends on: the protocol hash, the config,
the dataset export and its content hash, the code commit and the seeds. `build_bundle`
writes exactly three files: the export, the job JSON and a `git archive` of the pinned
commit (limited to `CODE_PATHS`). `scan_bundle` is the gate the runner calls before any
byte leaves the machine. It refuses:
- any file outside the allow-list, and any code member outside `CODE_PATHS`;
- secret-shaped names (`.env`, keys, certificates, databases, credentials);
- key-like strings, a SQLite store, and any local secret value.
Generated or trainer code is untrusted (master plan §15.5 item 6): the VM gets no
broker, LLM or GitHub credentials, because nothing secret is ever in the bundle.
"""

import os
import re
import shutil
import subprocess
import tarfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Self

from pydantic import Field, JsonValue, ValidationError, model_validator

from app.core.contracts import ContractModel, Sha256Ref, sha256_ref
from app.core.secrets import SECRET_KEY

JOB = "job.json"
DATASET = "dataset.jsonl"
CODE = "code.tar"
BUNDLE_FILES = (JOB, DATASET, CODE)
CODE_PATHS = ("backend/app", "pyproject.toml", "uv.lock")
_CODE_DIRS = ("backend/", "backend/app/")

_SECRET_NAME = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|id_(rsa|dsa|ecdsa|ed25519)[^/]*|[^/]*credentials[^/]*"
    r"|[^/]*\.(pem|key|p12|pfx|db|sqlite3?))$",
    re.IGNORECASE,
)
KEY_LIKE = re.compile(
    rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    rb"|AKIA[0-9A-Z]{16}"
    rb"|\bgh[pousr]_[A-Za-z0-9]{36}"
    rb"|\bgithub_pat_[A-Za-z0-9_]{22,}"
    rb"|\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{32,}"
    rb"|\bAIza[0-9A-Za-z_-]{35}"
    rb"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    rb"|\b[0-9]{8,10}:AA[A-Za-z0-9_-]{33}"  # Telegram bot token
)
_SQLITE_MAGIC = b"SQLite format 3\x00"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"  # also a directory name: no '/', no '@'


class BundleRefused(ValueError):
    """The bundle may hold something that must never reach a Colab VM."""


def _secret_keys(value: JsonValue, path: str = "config") -> list[str]:
    if isinstance(value, dict):
        found = [f"{path}.{k}" for k in value if SECRET_KEY.search(k)]
        return found + [p for k, v in value.items() for p in _secret_keys(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for v in value for p in _secret_keys(v, path)]
    return []


class JobSpec(ContractModel):
    job_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    protocol_hash: Sha256Ref
    config: dict[str, JsonValue]
    dataset_path: str = Field(min_length=1)
    """The local export; inside a bundle it is always `DATASET`."""
    dataset_hash: Sha256Ref
    code_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    seeds: list[int] = Field(min_length=1)
    entrypoint: str = Field(
        default="app.learning.colab_stub", pattern=r"^app(\.[a-z_][a-z0-9_]*)+$"
    )
    model_id: str = Field(pattern=_ID)
    model_version: str = Field(pattern=_ID)
    timeout_s: int = Field(default=1800, gt=0, le=6 * 3600)
    """Hard wall-clock limit for the whole remote run, enforced locally."""

    @model_validator(mode="after")
    def _no_secret_config(self) -> Self:
        found = _secret_keys(self.config)
        if found:
            raise ValueError(f"config keys look like secrets: {found}")
        return self


def known_secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    """Values of secret-named environment variables: none may appear in a bundle."""
    source = os.environ if env is None else env
    return [v for k, v in source.items() if SECRET_KEY.search(k) and len(v) >= 8]


def _content_problems(label: str, data: bytes, known: list[bytes]) -> list[str]:
    problems = []
    if KEY_LIKE.search(data):
        problems.append(f"{label}: key-like string")
    if data.startswith(_SQLITE_MAGIC):
        problems.append(f"{label}: SQLite database")
    if any(s in data for s in known):
        problems.append(f"{label}: contains a local secret value")
    return problems


def _code_problems(code: Path, known: list[bytes]) -> list[str]:
    problems = []
    with tarfile.open(code) as tar:
        for m in tar.getmembers():
            name = m.name
            if _SECRET_NAME.search(name.rstrip("/")):
                problems.append(f"{CODE}:{name}: secret-shaped name")
            if m.isdir():
                allowed = f"{name}/" in _CODE_DIRS or name.startswith("backend/app/")
            elif m.isfile():
                allowed = name in CODE_PATHS[1:] or name.startswith("backend/app/")
            else:
                allowed = False  # links and special files never go
            if not allowed:
                problems.append(f"{CODE}:{name}: outside the code allow-list")
            elif m.isfile():
                f = tar.extractfile(m)
                problems += _content_problems(f"{CODE}:{name}", f.read() if f else b"", known)
    return problems


def scan_bundle(bundle: Path, *, known_secrets: Iterable[str] = ()) -> JobSpec:
    """Refuse the bundle unless it holds only allow-listed, secret-free content; return
    its job."""
    entries = sorted(bundle.iterdir())
    names = [p.name for p in entries]
    if sorted(names) != sorted(BUNDLE_FILES) or any(
        p.is_symlink() or not p.is_file() for p in entries
    ):
        raise BundleRefused(f"bundle holds {names}; allowed exactly {sorted(BUNDLE_FILES)}")
    try:
        job = JobSpec.model_validate_json((bundle / JOB).read_bytes())
    except ValidationError as exc:
        raise BundleRefused(f"{JOB} is not a valid job: {exc}") from None
    known = [s.encode() for s in known_secrets if s]
    problems = []
    if job.dataset_path != DATASET:
        problems.append(f"{JOB}: dataset_path must be {DATASET!r}")
    for name in (JOB, DATASET):
        problems += _content_problems(name, (bundle / name).read_bytes(), known)
    if sha256_ref((bundle / DATASET).read_bytes()) != job.dataset_hash:
        problems.append(f"{DATASET}: content does not match the job's dataset_hash")
    problems += _code_problems(bundle / CODE, known)
    if problems:
        raise BundleRefused("; ".join(problems))
    return job


def build_bundle(job: JobSpec, *, repo: Path, out: Path) -> Path:
    """Write the bundle into the empty directory `out`, scan it and return its job path.
    A refused bundle is removed."""
    if out.exists() and any(out.iterdir()):
        raise BundleRefused(f"{out} must be empty")
    data = Path(job.dataset_path).read_bytes()
    if sha256_ref(data) != job.dataset_hash:
        raise BundleRefused("the dataset export does not match the job's dataset_hash")
    git = shutil.which("git")
    if git is None:
        raise BundleRefused("git is not on PATH")
    code = subprocess.run(
        [git, "-C", str(repo), "archive", "--format=tar", job.code_sha, "--", *CODE_PATHS],
        capture_output=True,
        check=True,
    ).stdout
    out.mkdir(parents=True, exist_ok=True)
    try:
        (out / DATASET).write_bytes(data)
        (out / CODE).write_bytes(code)
        bundled = job.model_copy(update={"dataset_path": DATASET})
        (out / JOB).write_text(bundled.model_dump_json(indent=2), encoding="utf-8")
        scan_bundle(out, known_secrets=known_secret_values())
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return out / JOB
