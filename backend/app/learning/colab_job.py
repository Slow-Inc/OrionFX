"""Training job spec and the bundle that goes to a Colab VM (#134; PRD #120 slice 7).

A `JobSpec` pins everything a training run depends on: the protocol hash, the config,
the dataset export and its content hash, the code commit and its archive hash, and the
seeds. `build_bundle` writes exactly three files: the export, the job JSON and a
`git archive` of the pinned commit (limited to `CODE_PATHS`). The export must sit under
the configured export directory and must not have a secret-shaped name.

`scan_files` is the gate the runner calls before any byte leaves the machine; the runner
uploads exactly the bytes it scanned. It refuses:
- any file outside the allow-list, and any code member outside `CODE_PATHS`, any link or
  special member, and any unsafe member path;
- secret-shaped names (`.env`, keys, certificates, databases, credentials);
- a dataset that is not UTF-8 JSONL with one JSON object per line;
- key-like strings, a SQLite store, and the value of any known local secret: the
  secret-named environment variables and every value in the repo's `.env` (raw or
  JSON-escaped, 4 characters or more). A refusal names the variable, never the value.
- a `code.tar` whose sha256 is not the job's `code_hash`.
`git_archive` regenerates the code archive, so the runner can also check that `code.tar`
is the archive of `code_sha`.

What the scan proves is limited to that list: it cannot find a secret whose shape and
value it does not know. Generated or trainer code is untrusted (master plan §15.5 item
6): the VM is given no broker, LLM or GitHub credentials, and the runner forwards no
secret-named environment variable to the `colab` CLI.
"""

import io
import json
import os
import posixpath
import re
import shutil
import subprocess
import tarfile
from collections.abc import Mapping
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
# `git archive` also emits the parent directories of `backend/app` as members.
_CODE_PARENT_DIRS = ("backend", "backend/app")
MIN_SECRET_LEN = 4

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
    code_hash: Sha256Ref | None = None
    """sha256 of `code.tar`. `build_bundle` sets it; a bundled job must carry it."""
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


def _dotenv_value(raw: str) -> str:
    """One value as dotenv reads it: a quoted value ends at its closing quote (`\\` escapes
    inside double quotes); a bare value ends at a ` #` comment."""
    value = raw.strip()
    if value[:1] not in ("'", '"'):
        return re.split(r"\s#", value, maxsplit=1)[0].rstrip()
    quote, out, i = value[0], [], 1
    while i < len(value) and value[i] != quote:
        if quote == '"' and value[i] == "\\" and i + 1 < len(value):
            i += 1
        out.append(value[i])
        i += 1
    return "".join(out)


def _dotenv_values(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines of a `.env` file (optional `export `, quotes, inline comments)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = _dotenv_value(value)
    return values


def known_secret_values(
    env: Mapping[str, str] | None = None, *, dotenv: Path | None = None
) -> dict[str, str]:
    """Name -> value of every local secret no bundle may contain: secret-named environment
    variables, plus every value in `dotenv` (account data such as a login or server name
    is not secret-named). Values shorter than `MIN_SECRET_LEN` are skipped."""
    source = os.environ if env is None else env
    found = {k: v for k, v in source.items() if SECRET_KEY.search(k)}
    if dotenv is not None and dotenv.is_file():
        found.update(_dotenv_values(dotenv))
    return {k: v for k, v in found.items() if len(v) >= MIN_SECRET_LEN}


def _needles(known: Mapping[str, str]) -> list[tuple[str, bytes]]:
    """Each secret in its raw and JSON-escaped forms."""
    out = []
    for name, value in known.items():
        forms = {value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}
        out += [(name, form.encode("utf-8")) for form in sorted(forms)]
    return out


def _content_problems(label: str, data: bytes, needles: list[tuple[str, bytes]]) -> list[str]:
    problems = []
    if KEY_LIKE.search(data):
        problems.append(f"{label}: key-like string")
    if data.startswith(_SQLITE_MAGIC):
        problems.append(f"{label}: SQLite database")
    names = sorted({name for name, needle in needles if needle in data})
    if names:
        problems.append(f"{label}: contains the value of {', '.join(names)}")
    return problems


def _dataset_problems(data: bytes) -> list[str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{DATASET}: not UTF-8 text"]
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    if not lines:
        return [f"{DATASET}: empty"]
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            return [f"{DATASET} line {number}: not JSON"]
        if not isinstance(row, dict):
            return [f"{DATASET} line {number}: not a JSON object"]
    return []


def _unsafe(name: str) -> bool:
    return (
        "\\" in name
        or name.startswith("/")
        or ".." in name.split("/")
        or posixpath.normpath(name) != name
    )


def _code_problems(code: bytes, needles: list[tuple[str, bytes]]) -> list[str]:
    problems = []
    try:
        with tarfile.open(fileobj=io.BytesIO(code), mode="r:") as tar:
            for m in tar.getmembers():
                name = m.name
                if _unsafe(name):
                    problems.append(f"{CODE}:{name}: unsafe member path")
                    continue
                if _SECRET_NAME.search(name):
                    problems.append(f"{CODE}:{name}: secret-shaped name")
                if m.isdir():
                    allowed = name in _CODE_PARENT_DIRS or name.startswith("backend/app/")
                elif m.isfile():
                    allowed = name in CODE_PATHS[1:] or name.startswith("backend/app/")
                else:
                    allowed = False  # links and special files never go
                if not allowed:
                    problems.append(f"{CODE}:{name}: outside the code allow-list")
                elif m.isfile():
                    f = tar.extractfile(m)
                    data = f.read() if f else b""
                    problems += _content_problems(f"{CODE}:{name}", data, needles)
    except tarfile.TarError as exc:
        problems.append(f"{CODE}: not a plain tar ({exc})")
    return problems


def scan_files(
    bundle: Path, *, known_secrets: Mapping[str, str]
) -> tuple[JobSpec, dict[str, bytes]]:
    """Refuse the bundle unless it holds only allow-listed, secret-free content; return
    its job and the exact bytes that were scanned (the only bytes that may be sent)."""
    entries = sorted(bundle.iterdir())
    names = [p.name for p in entries]
    if sorted(names) != sorted(BUNDLE_FILES) or any(
        p.is_symlink() or not p.is_file() for p in entries
    ):
        raise BundleRefused(f"bundle holds {names}; allowed exactly {sorted(BUNDLE_FILES)}")
    files = {name: (bundle / name).read_bytes() for name in BUNDLE_FILES}
    try:
        job = JobSpec.model_validate_json(files[JOB])
    except ValidationError as exc:
        raise BundleRefused(f"{JOB} is not a valid job: {exc}") from None
    needles = _needles(known_secrets)
    problems = []
    if job.dataset_path != DATASET:
        problems.append(f"{JOB}: dataset_path must be {DATASET!r}")
    for name in (JOB, DATASET):
        problems += _content_problems(name, files[name], needles)
    problems += _dataset_problems(files[DATASET])
    if sha256_ref(files[DATASET]) != job.dataset_hash:
        problems.append(f"{DATASET}: content does not match the job's dataset_hash")
    if job.code_hash is None:
        problems.append(f"{JOB}: code_hash is missing")
    elif sha256_ref(files[CODE]) != job.code_hash:
        problems.append(f"{CODE}: content does not match the job's code_hash")
    problems += _code_problems(files[CODE], needles)
    if problems:
        raise BundleRefused("; ".join(problems))
    return job, files


def scan_bundle(bundle: Path, *, known_secrets: Mapping[str, str] | None = None) -> JobSpec:
    """`scan_files` without the bytes."""
    return scan_files(bundle, known_secrets=known_secrets or {})[0]


def git_archive(repo: Path, code_sha: str) -> bytes:
    """The `git archive` of `code_sha`, limited to `CODE_PATHS`. Deterministic for a given
    commit and git version, so a rebuild can be compared byte for byte."""
    git = shutil.which("git")
    if git is None:
        raise BundleRefused("git is not on PATH")
    argv = [git, "-C", str(repo), "archive", "--format=tar", code_sha, "--", *CODE_PATHS]
    done = subprocess.run(argv, capture_output=True)
    if done.returncode != 0:
        tail = done.stderr.decode(errors="replace").strip()[-500:]
        raise BundleRefused(f"git archive of {code_sha} failed: {tail}")
    return done.stdout


def _export_source(job: JobSpec, exports: Path) -> Path:
    root = exports.resolve()
    source = Path(job.dataset_path).resolve()
    if not source.is_relative_to(root):
        raise BundleRefused(f"dataset_path must be inside the export directory {exports}")
    rel = source.relative_to(root).as_posix()
    if any(_SECRET_NAME.search(part) for part in rel.split("/")):
        raise BundleRefused(f"dataset_path {rel!r} has a secret-shaped name")
    return source


def build_bundle(
    job: JobSpec, *, repo: Path, out: Path, exports: Path, known_secrets: Mapping[str, str]
) -> Path:
    """Write the bundle into the empty directory `out`, scan it and return its job path.
    A refused bundle is removed."""
    if out.exists() and any(out.iterdir()):
        raise BundleRefused(f"{out} must be empty")
    data = _export_source(job, exports).read_bytes()
    if sha256_ref(data) != job.dataset_hash:
        raise BundleRefused("the dataset export does not match the job's dataset_hash")
    code = git_archive(repo, job.code_sha)
    out.mkdir(parents=True, exist_ok=True)
    try:
        (out / DATASET).write_bytes(data)
        (out / CODE).write_bytes(code)
        bundled = job.model_copy(update={"dataset_path": DATASET, "code_hash": sha256_ref(code)})
        (out / JOB).write_text(bundled.model_dump_json(indent=2), encoding="utf-8")
        scan_files(out, known_secrets=known_secrets)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    return out / JOB
