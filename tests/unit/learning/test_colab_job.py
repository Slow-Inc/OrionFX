"""Colab job spec and bundle (#134; PRD #120 slice 7): what may go onto the VM."""

import gzip
import io
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.contracts import sha256_ref
from app.learning import colab_job, colab_runner
from app.learning.colab_job import (
    BUNDLE_FILES,
    CODE,
    DATASET,
    JOB,
    BundleRefused,
    JobSpec,
    build_bundle,
    git_archive,
    known_secret_values,
    scan_bundle,
)

REPO = Path(__file__).resolve().parents[3]
# Built at runtime so no literal secret-shaped text sits in the repo.
FAKE_PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"
FAKE_AWS_KEY = "AKIA" + "Q" * 16
KEY_SHAPES = {
    "private-key": FAKE_PRIVATE_KEY,
    "aws": FAKE_AWS_KEY,
    "github": "ghp_" + "a1" * 18,
    "github-pat": "github_pat_" + "B" * 22,
    "openai": "sk-" + "c" * 32,
    "anthropic": "sk-ant-" + "d" * 32,
    "google": "AIza" + "e" * 35,
    "slack": "xoxb-" + "1" * 10,
    "telegram": "123456789:AA" + "f" * 33,
}


def _head() -> str:
    return subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _exports(tmp_path: Path) -> Path:
    path = tmp_path / "exports"
    path.mkdir(exist_ok=True)
    return path


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


def _dataset(
    tmp_path: Path, text: str = '{"split":"train","x":1}\n', name: str = "export.jsonl"
) -> Path:
    path = _exports(tmp_path) / name
    path.write_text(text, encoding="utf-8")
    return path


def _build(tmp_path: Path, job: JobSpec, **kw: Any) -> Path:
    """Build into `tmp_path/b`; returns the bundle directory."""
    kw.setdefault("known_secrets", {})
    kw.setdefault("exports", _exports(tmp_path))
    return build_bundle(job, repo=REPO, out=tmp_path / "b", **kw).parent


def _rewrite_code(bundle: Path, extra: dict[str, bytes]) -> None:
    """Append members to the bundle's code.tar (simulates a poisoned archive)."""
    with tarfile.open(bundle / CODE, "a") as tar:
        for name, data in extra.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _add_link(bundle: Path, name: str, kind: bytes) -> None:
    with tarfile.open(bundle / CODE, "a") as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = "backend/app/__init__.py"
        tar.addfile(info)


def _code_names(bundle: Path) -> list[str]:
    with tarfile.open(bundle / CODE) as tar:
        return tar.getnames()


def test_bundle_from_head_holds_only_the_allow_list_and_passes_the_scan(tmp_path: Path) -> None:
    job = _job(_dataset(tmp_path))
    bundle = _build(tmp_path, job)

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
    assert rebuilt.code_hash == sha256_ref((bundle / CODE).read_bytes())
    # The runner's code-pin check: a rebuild of the pinned commit is byte-identical.
    assert git_archive(REPO, rebuilt.code_sha) == (bundle / CODE).read_bytes()


def test_git_archive_of_an_unknown_commit_is_refused() -> None:
    with pytest.raises(BundleRefused, match="git archive"):
        git_archive(REPO, "f" * 40)


def test_the_scanner_does_not_trip_on_its_own_source() -> None:
    source = Path(colab_job.__file__).read_bytes()
    assert colab_job.KEY_LIKE.search(source) is None


def test_an_env_file_beside_the_bundle_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    (bundle / ".env").write_text("MT5_PASSWORD=x\n")
    with pytest.raises(BundleRefused, match=r"\.env"):
        scan_bundle(bundle)


def test_an_env_file_inside_the_code_archive_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    _rewrite_code(bundle, {"backend/app/.env": b"A=1\n"})
    with pytest.raises(BundleRefused, match=r"backend/app/\.env"):
        scan_bundle(bundle)


@pytest.mark.parametrize(
    "name",
    ["backend/app/../../x.py", "/backend/app/x.py", "backend/app\\x.py", "backend/app/./x.py"],
)
def test_an_unsafe_member_path_is_refused(tmp_path: Path, name: str) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    _rewrite_code(bundle, {name: b"print(1)\n"})
    with pytest.raises(BundleRefused, match="unsafe member path"):
        scan_bundle(bundle)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE], ids=["symlink", "hardlink"])
def test_a_link_member_in_the_code_archive_is_refused(tmp_path: Path, kind: bytes) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    _add_link(bundle, "backend/app/link.py", kind)
    with pytest.raises(BundleRefused, match=r"backend/app/link\.py: outside the code allow-list"):
        scan_bundle(bundle)


def test_a_bundle_entry_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    (bundle / CODE).unlink()
    (bundle / CODE).mkdir()
    with pytest.raises(BundleRefused, match="allowed exactly"):
        scan_bundle(bundle)


def test_a_symlinked_bundle_entry_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    real = tmp_path / "elsewhere.tar"
    (bundle / CODE).replace(real)
    try:
        (bundle / CODE).symlink_to(real)
    except OSError:
        pytest.skip("this platform cannot create a symlink without privileges")
    with pytest.raises(BundleRefused, match="allowed exactly"):
        scan_bundle(bundle)


def test_code_outside_the_allow_list_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    _rewrite_code(bundle, {"scripts/leak.py": b"print(1)\n"})
    with pytest.raises(BundleRefused, match=r"scripts/leak\.py"):
        scan_bundle(bundle)


def test_a_directory_outside_the_allow_list_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    with tarfile.open(bundle / CODE, "a") as tar:
        info = tarfile.TarInfo("scripts")
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
    with pytest.raises(BundleRefused, match="scripts: outside the code allow-list"):
        scan_bundle(bundle)


def test_a_code_archive_that_differs_from_its_code_hash_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    _rewrite_code(bundle, {"backend/app/extra.py": b"X = 1\n"})
    with pytest.raises(BundleRefused, match="code_hash"):
        scan_bundle(bundle)


def test_a_bundled_job_without_a_code_hash_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    data = json.loads((bundle / JOB).read_text(encoding="utf-8"))
    data["code_hash"] = None
    (bundle / JOB).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BundleRefused, match="code_hash"):
        scan_bundle(bundle)


@pytest.mark.parametrize("secret", list(KEY_SHAPES.values()), ids=list(KEY_SHAPES))
def test_every_key_like_shape_in_the_code_is_refused(tmp_path: Path, secret: str) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    _rewrite_code(bundle, {"backend/app/leak.py": f"K = '{secret}'\n".encode()})
    with pytest.raises(BundleRefused, match="key-like"):
        scan_bundle(bundle)


def test_a_key_like_string_in_the_dataset_is_refused_and_the_bundle_removed(
    tmp_path: Path,
) -> None:
    job = _job(_dataset(tmp_path, f'{{"note":"{FAKE_AWS_KEY}"}}\n'))
    with pytest.raises(BundleRefused, match="key-like"):
        _build(tmp_path, job)
    assert not (tmp_path / "b").exists()  # a refused bundle is never left behind


def test_a_sqlite_store_is_refused(tmp_path: Path) -> None:
    db = _exports(tmp_path) / "store.jsonl"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    with pytest.raises(BundleRefused, match="SQLite"):
        _build(tmp_path, _job(db))


# --- B1: what the dataset may be -------------------------------------------------------


def test_a_dataset_outside_the_export_directory_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text('{"x":1}\n', encoding="utf-8")
    with pytest.raises(BundleRefused, match="export directory"):
        _build(tmp_path, _job(outside))
    assert not (tmp_path / "b").exists()


@pytest.mark.parametrize("name", [".env", "credentials.jsonl", "store.sqlite3", "id_rsa"])
def test_a_secret_shaped_dataset_name_is_refused_even_when_its_content_is_jsonl(
    tmp_path: Path, name: str
) -> None:
    dataset = _dataset(tmp_path, '{"x":1}\n', name=name)
    with pytest.raises(BundleRefused, match="secret-shaped name"):
        _build(tmp_path, _job(dataset))


def test_a_compressed_dataset_is_refused(tmp_path: Path) -> None:
    dataset = _exports(tmp_path) / "export.jsonl"
    dataset.write_bytes(gzip.compress(b"SQLite format 3\x00" + b"\x00" * 64))
    with pytest.raises(BundleRefused, match="UTF-8"):
        _build(tmp_path, _job(dataset))


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ('{"x":1}\nnot json\n', "line 2: not JSON"),
        ('{"x":1}\n[1,2]\n', "line 2: not a JSON object"),
        ("", "empty"),
    ],
    ids=["not-json", "not-an-object", "empty"],
)
def test_a_dataset_that_is_not_jsonl_objects_is_refused(
    tmp_path: Path, text: str, reason: str
) -> None:
    with pytest.raises(BundleRefused, match=reason):
        _build(tmp_path, _job(_dataset(tmp_path, text)))


def test_a_dataset_line_too_deep_to_parse_is_refused_not_crashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # How deep the parser can go depends on the platform's stack, so force the error.
    original = json.loads

    def too_deep(s: str | bytes, *args: Any, **kwargs: Any) -> Any:
        if isinstance(s, str) and s.strip() == '{"deep":1}':
            raise RecursionError("maximum recursion depth exceeded")
        return original(s, *args, **kwargs)

    monkeypatch.setattr(json, "loads", too_deep)
    with pytest.raises(BundleRefused, match="not JSON"):
        _build(tmp_path, _job(_dataset(tmp_path, '{"deep":1}\n')))


# --- B1: known local secret values -----------------------------------------------------


def test_a_local_secret_value_anywhere_in_the_bundle_is_refused_by_name(tmp_path: Path) -> None:
    secret = "hunter2-correct-horse-battery"
    bundle = _build(tmp_path, _job(_dataset(tmp_path, f'{{"x":"{secret}"}}\n')))
    with pytest.raises(BundleRefused, match="MT5_PASSWORD") as err:
        scan_bundle(bundle, known_secrets={"MT5_PASSWORD": secret})
    assert secret not in str(err.value)  # the refusal names the variable, never the value


def test_a_json_escaped_secret_value_is_refused(tmp_path: Path) -> None:
    secret = 'pa"ss\\word'
    line = json.dumps({"x": secret}) + "\n"
    assert secret not in line  # only the escaped form is in the file
    with pytest.raises(BundleRefused, match="MT5_PASSWORD"):
        _build(tmp_path, _job(_dataset(tmp_path, line)), known_secrets={"MT5_PASSWORD": secret})


def test_a_four_character_secret_value_is_refused(tmp_path: Path) -> None:
    with pytest.raises(BundleRefused, match="MT5_PIN"):
        _build(
            tmp_path, _job(_dataset(tmp_path, '{"x":"q7z9"}\n')), known_secrets={"MT5_PIN": "q7z9"}
        )


def test_known_secret_values_come_from_secret_named_variables_and_every_dotenv_value(
    tmp_path: Path,
) -> None:
    env = {"MT5_PASSWORD": "pw-1", "TELEGRAM_BOT_TOKEN": "tok-123456789", "HOME": "/h", "A": "x"}
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        '# comment\nMT5_LOGIN=12345678\nexport MT5_SERVER="Demo-Server"\nSHORT=abc\n'
        "QUOTED='q-value'\n\nNOEQUALS\n",
        encoding="utf-8",
    )
    found = known_secret_values(env, dotenv=dotenv)
    assert found == {
        "MT5_PASSWORD": "pw-1",
        "TELEGRAM_BOT_TOKEN": "tok-123456789",
        "MT5_LOGIN": "12345678",
        "MT5_SERVER": "Demo-Server",
        "QUOTED": "q-value",
    }
    assert known_secret_values({}, dotenv=_dotenv(tmp_path, 'A="x\\"y z"\n')) == {
        "A": 'x"y z',
        "A (raw)": 'x\\"y z',
    }
    assert known_secret_values(env, dotenv=tmp_path / "missing.env") == {
        "MT5_PASSWORD": "pw-1",
        "TELEGRAM_BOT_TOKEN": "tok-123456789",
    }


def _dotenv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "x.env"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("line", "value"),
    [
        ("MT5_PASSWORD=hunter2 # prod", "hunter2"),
        ("MT5_PASSWORD=hunter2\t# prod", "hunter2"),
        ('MT5_PASSWORD="hunter2" # prod', "hunter2"),
        ("MT5_PASSWORD='hunter2' trailing", "hunter2"),
        ("MT5_PASSWORD=hun#ter2", "hun#ter2"),  # a '#' inside a bare value is not a comment
        ('MT5_PASSWORD="a # b"', "a # b"),
    ],
    ids=["inline-comment", "tab-comment", "quoted-comment", "quoted-trailing", "hash", "quoted"],
)
def test_the_dotenv_parser_strips_comments_and_quotes_like_dotenv(
    tmp_path: Path, line: str, value: str
) -> None:
    assert known_secret_values({}, dotenv=_dotenv(tmp_path, line + "\n")) == {"MT5_PASSWORD": value}


def test_the_dotenv_parser_decodes_escapes_like_python_dotenv_and_keeps_the_raw_form(
    tmp_path: Path,
) -> None:
    text = 'A="l1\\nl2\\t\\\\x\\"y"\nB=\'raw\\n\'\n'
    assert known_secret_values({}, dotenv=_dotenv(tmp_path, text)) == {
        "A": 'l1\nl2\t\\x"y',
        "A (raw)": 'l1\\nl2\\t\\\\x\\"y',  # a loader that does not decode sees this
        "B": "raw\\n",  # `\n` is not an escape inside single quotes
    }


@pytest.mark.parametrize(
    ("line", "forms"),
    [
        ('MT5_PASSWORD="Pa55\\word!"', {"MT5_PASSWORD": "Pa55\\word!"}),  # unknown escape kept
        (
            "MT5_PASSWORD='ab\\'cdefg'",
            {"MT5_PASSWORD": "ab'cdefg", "MT5_PASSWORD (raw)": "ab\\'cdefg"},
        ),
    ],
    ids=["unknown-escape-kept", "escaped-single-quote"],
)
def test_a_backslash_in_a_quoted_dotenv_secret_does_not_hide_it(
    tmp_path: Path, line: str, forms: dict[str, str]
) -> None:
    known = known_secret_values({}, dotenv=_dotenv(tmp_path, line + "\n"))
    assert known == forms
    for value in forms.values():
        data = json.dumps({"x": value}) + "\n"
        with pytest.raises(BundleRefused, match="MT5_PASSWORD"):
            _build(tmp_path, _job(_dataset(tmp_path, data)), known_secrets=known)


def test_the_bundle_command_exits_2_on_a_malformed_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(colab_runner, "known_secret_values", lambda **kw: {})
    spec = tmp_path / "spec.json"
    spec.write_text('{"job_id": "Bad Id"}', encoding="utf-8")
    assert colab_runner.main(["bundle", str(spec), str(tmp_path / "b")]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_a_relative_dataset_path_resolves_against_the_export_directory(tmp_path: Path) -> None:
    _dataset(tmp_path)
    job = _job(_exports(tmp_path) / "export.jsonl").model_copy(
        update={"dataset_path": "export.jsonl"}
    )
    assert scan_bundle(_build(tmp_path, job)).dataset_path == DATASET


def test_a_code_archive_that_is_not_a_plain_tar_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    (bundle / CODE).write_bytes(gzip.compress((bundle / CODE).read_bytes()))
    with pytest.raises(BundleRefused, match="not a plain tar"):
        scan_bundle(bundle)


def test_a_bundled_job_naming_another_dataset_path_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    data = json.loads((bundle / JOB).read_text(encoding="utf-8"))
    data["dataset_path"] = "../elsewhere.jsonl"
    (bundle / JOB).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BundleRefused, match="dataset_path must be"):
        scan_bundle(bundle)


def test_a_dotenv_secret_with_an_inline_comment_is_still_found_in_the_bundle(
    tmp_path: Path,
) -> None:
    known = known_secret_values({}, dotenv=_dotenv(tmp_path, "MT5_PASSWORD=hunter2 # prod\n"))
    with pytest.raises(BundleRefused, match="MT5_PASSWORD"):
        _build(tmp_path, _job(_dataset(tmp_path, '{"x":"hunter2"}\n')), known_secrets=known)


def _spec(tmp_path: Path, dataset: Path) -> Path:
    spec = tmp_path / "spec.json"
    spec.write_text(_job(dataset).model_dump_json(), encoding="utf-8")
    return spec


def test_the_bundle_command_writes_a_scanned_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(colab_runner, "known_secret_values", lambda **kw: {})
    spec = _spec(tmp_path, _dataset(tmp_path))
    argv = ["bundle", str(spec), str(tmp_path / "b"), "--repo", str(REPO),
            "--exports", str(_exports(tmp_path))]  # fmt: skip
    assert colab_runner.main(argv) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "b" / JOB)
    assert scan_bundle(tmp_path / "b").code_hash is not None


def test_the_bundle_command_exits_2_on_a_refused_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(colab_runner, "known_secret_values", lambda **kw: {})
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text('{"x":1}\n', encoding="utf-8")
    argv = ["bundle", str(_spec(tmp_path, outside)), str(tmp_path / "b"), "--repo", str(REPO),
            "--exports", str(_exports(tmp_path))]  # fmt: skip
    assert colab_runner.main(argv) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert not (tmp_path / "b").exists()


def test_a_dataset_that_differs_from_its_recorded_hash_is_refused(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path)
    job = _job(dataset, dataset_hash=sha256_ref(b"something else"))
    with pytest.raises(BundleRefused, match="dataset"):
        _build(tmp_path, job)


def test_a_tampered_bundled_dataset_is_refused(tmp_path: Path) -> None:
    bundle = _build(tmp_path, _job(_dataset(tmp_path)))
    (bundle / DATASET).write_text('{"x":2}\n')
    with pytest.raises(BundleRefused, match="dataset"):
        scan_bundle(bundle)


def test_build_refuses_a_non_empty_output_directory(tmp_path: Path) -> None:
    out = tmp_path / "b"
    out.mkdir()
    (out / JOB).write_text("{}")
    with pytest.raises(BundleRefused, match="empty"):
        _build(tmp_path, _job(_dataset(tmp_path)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("config", {"api_key": "x"}),
        ("config", {"nested": {"MT5_PASSWORD": "x"}}),
        ("config", {"x": [{"api_key": 1}]}),
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
