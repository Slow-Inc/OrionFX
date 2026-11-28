"""Colab job spec and bundle (#134; PRD #120 slice 7): what may go onto the VM."""

import io
import subprocess
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.contracts import sha256_ref
from app.learning import colab_job
from app.learning.colab_job import (
    BUNDLE_FILES,
    CODE,
    DATASET,
    JOB,
    BundleRefused,
    JobSpec,
    build_bundle,
    scan_bundle,
)

REPO = Path(__file__).resolve().parents[3]
# Built at runtime so no literal secret-shaped text sits in the repo.
FAKE_PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"
FAKE_AWS_KEY = "AKIA" + "Q" * 16


def _head() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _job(dataset: Path, **over: object) -> JobSpec:
    fields: dict[str, object] = {
        "job_id": "smoke-1",
        "protocol_hash": sha256_ref(b"protocol"),
        "config": {"learning_rate": 0.1, "num_leaves": 31},
        "dataset_path": str(dataset),
        "dataset_hash": sha256_ref(dataset.read_bytes()),
        "code_sha": _head(),
        "seeds": [7, 11],
        "model_id": "stub-colab",
        "model_version": "0.0.1",
    }
    fields.update(over)
    return JobSpec.model_validate(fields)


def _dataset(tmp_path: Path, text: str = '{"split":"train","x":1}\n') -> Path:
    path = tmp_path / "export.jsonl"
    path.write_text(text, encoding="utf-8")
    return path


def _rewrite_code(bundle: Path, extra: dict[str, bytes]) -> None:
    """Append members to the bundle's code.tar (simulates a poisoned archive)."""
    with tarfile.open(bundle / CODE, "a") as tar:
        for name, data in extra.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _code_names(bundle: Path) -> list[str]:
    with tarfile.open(bundle / CODE) as tar:
        return tar.getnames()


def test_bundle_from_head_holds_only_the_allow_list_and_passes_the_scan(tmp_path: Path) -> None:
    job = _job(_dataset(tmp_path))
    job_path = build_bundle(job, repo=REPO, out=tmp_path / "bundle")

    bundle = job_path.parent
    assert sorted(p.name for p in bundle.iterdir()) == sorted(BUNDLE_FILES)
    names = _code_names(bundle)
    assert "backend/app/core/contracts.py" in names
    assert "uv.lock" in names
    assert all(
        n in ("pyproject.toml", "uv.lock", "backend", "backend/app") or n.startswith("backend/app/")
        for n in names
    ), names
    assert not any(n.startswith(("tests/", "frontend/", "docs/", "data/", ".")) for n in names)
    rebuilt = scan_bundle(bundle)
    assert rebuilt.dataset_path == DATASET  # the bundled job names the bundled export
    assert rebuilt.dataset_hash == job.dataset_hash


def test_the_scanner_does_not_trip_on_its_own_source() -> None:
    source = Path(colab_job.__file__).read_bytes()
    assert colab_job.KEY_LIKE.search(source) is None


def test_an_env_file_beside_the_bundle_is_refused(tmp_path: Path) -> None:
    bundle = build_bundle(_job(_dataset(tmp_path)), repo=REPO, out=tmp_path / "b").parent
    (bundle / ".env").write_text("MT5_PASSWORD=x\n")
    with pytest.raises(BundleRefused, match=r"\.env"):
        scan_bundle(bundle)


def test_an_env_file_inside_the_code_archive_is_refused(tmp_path: Path) -> None:
    bundle = build_bundle(_job(_dataset(tmp_path)), repo=REPO, out=tmp_path / "b").parent
    _rewrite_code(bundle, {"backend/app/.env": b"A=1\n"})
    with pytest.raises(BundleRefused, match=r"backend/app/\.env"):
        scan_bundle(bundle)


def test_code_outside_the_allow_list_is_refused(tmp_path: Path) -> None:
    bundle = build_bundle(_job(_dataset(tmp_path)), repo=REPO, out=tmp_path / "b").parent
    _rewrite_code(bundle, {"scripts/leak.py": b"print(1)\n"})
    with pytest.raises(BundleRefused, match=r"scripts/leak\.py"):
        scan_bundle(bundle)


@pytest.mark.parametrize("secret", [FAKE_PRIVATE_KEY, FAKE_AWS_KEY, "ghp_" + "a1" * 18])
def test_a_key_like_string_in_the_code_is_refused(tmp_path: Path, secret: str) -> None:
    bundle = build_bundle(_job(_dataset(tmp_path)), repo=REPO, out=tmp_path / "b").parent
    _rewrite_code(bundle, {"backend/app/leak.py": f"K = '{secret}'\n".encode()})
    with pytest.raises(BundleRefused, match="key-like"):
        scan_bundle(bundle)


def test_a_key_like_string_in_the_dataset_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    job = _job(_dataset(tmp_path, f'{{"note":"{FAKE_AWS_KEY}"}}\n'))
    with pytest.raises(BundleRefused, match="key-like"):
        build_bundle(job, repo=REPO, out=tmp_path / "b")


def test_a_sqlite_store_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "store.bin"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    with pytest.raises(BundleRefused, match="SQLite"):
        build_bundle(_job(db), repo=REPO, out=tmp_path / "b")


def test_a_local_secret_value_anywhere_in_the_bundle_is_refused(tmp_path: Path) -> None:
    secret = "hunter2-correct-horse-battery"
    bundle = build_bundle(
        _job(_dataset(tmp_path, f'{{"x":"{secret}"}}\n')), repo=REPO, out=tmp_path / "b"
    ).parent
    with pytest.raises(BundleRefused, match="local secret value") as err:
        scan_bundle(bundle, known_secrets=[secret])
    assert secret not in str(err.value)  # the refusal never echoes the secret


def test_known_secret_values_come_from_secret_named_variables_only() -> None:
    env = {"MT5_PASSWORD": "pw-123456789", "TELEGRAM_BOT_TOKEN": "tok-123456789", "HOME": "/h"}
    assert sorted(colab_job.known_secret_values(env)) == ["pw-123456789", "tok-123456789"]


def test_a_dataset_that_differs_from_its_recorded_hash_is_refused(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    job = _job(dataset, dataset_hash=sha256_ref(b"something else"))
    with pytest.raises(BundleRefused, match="dataset"):
        build_bundle(job, repo=REPO, out=tmp_path / "b")


def test_a_tampered_bundled_dataset_is_refused(tmp_path: Path) -> None:
    bundle = build_bundle(_job(_dataset(tmp_path)), repo=REPO, out=tmp_path / "b").parent
    (bundle / DATASET).write_text('{"x":2}\n')
    with pytest.raises(BundleRefused, match="dataset"):
        scan_bundle(bundle)


def test_build_refuses_a_non_empty_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "b"
    out.mkdir()
    (out / JOB).write_text("{}")
    with pytest.raises(BundleRefused, match="empty"):
        build_bundle(_job(_dataset(tmp_path)), repo=REPO, out=out)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("config", {"api_key": "x"}),
        ("config", {"nested": {"MT5_PASSWORD": "x"}}),
        ("code_sha", "HEAD"),
        ("code_sha", "--output=/tmp/x"),
        ("model_id", "../escape"),
        ("model_version", "a/b"),
        ("job_id", "Has Space"),
        ("entrypoint", "os.system"),
        ("seeds", []),
    ],
)
def test_job_spec_refuses_unsafe_fields(tmp_path: Path, field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _job(_dataset(tmp_path), **{field: value})
