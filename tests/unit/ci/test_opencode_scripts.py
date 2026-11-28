"""Behaviour of the OpenCode CI helpers (.github/opencode/post.py, extract.py).

These scripts turn the model's answer into GitHub writes, and post.py's verdict line
is what the merge gate parses, so they are tested through their public functions.
"""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


post = _load("opencode_post", ROOT / ".github/opencode/post.py")
extract = _load("opencode_extract", ROOT / ".github/actions/opencode-run/extract.py")

DIFF = """diff --git a/x.py b/x.py
index 1..2 100644
--- a/x.py
+++ b/x.py
@@ -1,2 +1,3 @@
 a
+b
 c
@@ -10,1 +11,2 @@
 k
+l
"""


# --- verdict line (parsed by the merge gate) -------------------------------------------


def test_verdict_counts_blocker_and_major_bullets_only() -> None:
    summary = "\n".join(
        [
            "### code-review",
            "- [blocker] a.py:1 - boom",
            "- [Major] b.py:2 - case-insensitive",
            "  - [major] nested bullet still counts",
            "- [minor] c.py:3 - not counted",
            "- [nit] d.py:4 - not counted",
            "text mentioning [major] mid-line is not a finding",
        ]
    )
    assert post.verdict_line(summary) == "Verdict: 1 blocker, 2 major"


def test_verdict_with_no_findings_is_zero_zero() -> None:
    assert post.verdict_line("- no findings") == "Verdict: 0 blocker, 0 major"


# --- diff line anchoring -------------------------------------------------------------------


def test_commentable_lines_are_new_file_lines_in_hunks() -> None:
    assert post.commentable_lines(DIFF) == {"x.py": {1, 2, 3, 11, 12}}


def test_added_line_that_looks_like_a_file_header_is_content() -> None:
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n a\n+++ not a header\n"
    assert post.commentable_lines(diff) == {"x.py": {1, 2}}


def test_deleted_file_has_no_commentable_lines() -> None:
    diff = "--- a/gone.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-x\n"
    assert post.commentable_lines(diff) == {}


# --- review posting ------------------------------------------------------------------------


class _Recorder:
    def __init__(self, fail_first_with_comments: bool = False, fail_all: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.endpoints: list[str] = []
        self.fail_first = fail_first_with_comments
        self.fail_all = fail_all

    def __call__(self, endpoint: str, payload: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        self.calls.append(json.loads(json.dumps(payload)))
        self.endpoints.append(endpoint)
        failing = self.fail_all or (
            self.fail_first and payload.get("comments") and len(self.calls) == 1
        )
        if failing:
            return subprocess.CompletedProcess([], 1, "", "422 line not in diff")
        return subprocess.CompletedProcess([], 0, "{}", "")


def _answer(tmp: Path, inline: object, summary: str = "- [major] x.py:2 - bug") -> str:
    text = f"OpenCode review of abc\n\n{summary}\nVerdict: 9 blocker, 9 major\n"
    text += f"\n```json opencode-inline\n{json.dumps(inline)}\n```\n"
    path = tmp / "answer.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _diff(tmp: Path) -> str:
    path = tmp / "pr.diff"
    path.write_text(DIFF, encoding="utf-8")
    return str(path)


def test_review_posts_comment_event_with_anchored_inline_and_computed_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(post, "gh_api", rec)
    inline = [
        {"path": "x.py", "line": 2, "body": "[major] bug"},
        {"path": "x.py", "line": 99, "body": "[minor] off-diff"},
    ]
    assert post.review(_answer(tmp_path, inline), _diff(tmp_path), "o/r", "7", "abc") == 0
    (payload,) = rec.calls
    assert payload["event"] == "COMMENT" and payload["commit_id"] == "abc"
    assert payload["comments"] == [
        {"path": "x.py", "line": 2, "side": "RIGHT", "body": "[major] bug"}
    ]
    body = payload["body"]
    assert "x.py:99" in body and "Not on a diff line" in body and "cap" not in body
    assert body.rstrip().splitlines()[-1] == "Verdict: 0 blocker, 1 major"
    assert "Verdict: 9 blocker" not in body and "opencode-inline" not in body


def test_inline_comments_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    monkeypatch.setattr(post, "gh_api", rec)
    inline = [{"path": "x.py", "line": 2, "body": f"n{i}"} for i in range(post.MAX_INLINE + 1)]
    post.review(_answer(tmp_path, inline), _diff(tmp_path), "o/r", "7", "abc")
    assert len(rec.calls[0]["comments"]) == post.MAX_INLINE
    body = rec.calls[0]["body"]
    assert f"n{post.MAX_INLINE}" in body and "inline cap" in body
    assert "Not on a diff line" not in body


def test_rejected_inline_comments_move_to_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(fail_first_with_comments=True)
    monkeypatch.setattr(post, "gh_api", rec)
    inline = [{"path": "x.py", "line": 2, "body": "[major] bug"}]
    assert post.review(_answer(tmp_path, inline), _diff(tmp_path), "o/r", "7", "abc") == 0
    retry = rec.calls[1]
    assert retry["comments"] == [] and "GitHub rejected" in retry["body"]
    assert retry["body"].rstrip().splitlines()[-1].startswith("Verdict:")


def test_invalid_inline_json_still_posts_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder()
    monkeypatch.setattr(post, "gh_api", rec)
    path = tmp_path / "answer.md"
    path.write_text("summary\n```json opencode-inline\n{not json\n```\n", encoding="utf-8")
    assert post.review(str(path), _diff(tmp_path), "o/r", "7", "abc") == 0
    assert rec.calls[0]["comments"] == []
    assert "{not json" in rec.calls[0]["body"]


def test_review_fails_when_the_retry_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _Recorder(fail_all=True)
    monkeypatch.setattr(post, "gh_api", rec)
    inline = [{"path": "x.py", "line": 2, "body": "[major] bug"}]
    assert post.review(_answer(tmp_path, inline), _diff(tmp_path), "o/r", "7", "abc") == 1
    assert len(rec.calls) == 2


# --- triage --------------------------------------------------------------------------------

EN = "The settings page loses the risk profile after a reload and shows the default."
TH = "หน้าตั้งค่าลืม risk profile หลังโหลดหน้าใหม่"


@pytest.mark.parametrize(
    ("text", "english", "thai"),
    [
        (EN, True, False),
        (TH, False, True),
        (f"{EN}\n\n{TH}", True, True),
        ("CI job fails", True, False),
        ("`backend/app/core/config.py` `risk_profile`", False, False),
    ],
    ids=["english", "thai", "both", "short-english", "code-only"],
)
def test_language_detection(text: str, english: bool, thai: bool) -> None:
    assert post.has_english(text) is english
    assert post.has_thai(text) is thai


def _triage(
    tmp: Path, monkeypatch: pytest.MonkeyPatch, answer: str, body: str, existing: str = ""
) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(post, "gh_api", rec)
    (tmp / "a.md").write_text(answer, encoding="utf-8")
    (tmp / "b.md").write_text(body, encoding="utf-8")
    assert post.triage(str(tmp / "a.md"), "o/r", "5", str(tmp / "b.md"), existing) == 0
    return rec


def test_triage_adds_allowed_component_and_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer = 'Here: {"component": "M06-risk-execution", "role": "needs-info"} done {x}'
    rec = _triage(tmp_path, monkeypatch, answer, f"{EN}\n{TH}")
    assert rec.calls == [{"labels": ["M06-risk-execution", "needs-info"]}]


@pytest.mark.parametrize(
    ("answer", "existing"),
    [
        ('{"component": "made-up", "role": "wontfix"}', ""),
        ('{"component": ["M06-risk-execution"], "role": 1}', ""),
        ('{"component": "M06-risk-execution", "role": "needs-info"}', "M02-market-data,wontfix"),
        ("no json at all", ""),
    ],
    ids=["not-allowed-and-wontfix", "wrong-types", "groups-already-labelled", "no-json"],
)
def test_triage_never_adds_disallowed_or_duplicate_group_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, existing: str
) -> None:
    rec = _triage(tmp_path, monkeypatch, answer, f"{EN}\n{TH}", existing)
    assert rec.calls == []


def test_triage_asks_for_the_missing_thai_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _triage(tmp_path, monkeypatch, "{}", EN)
    assert len(rec.calls) == 1 and rec.endpoints == ["repos/o/r/issues/5/comments"]
    assert "Thai" in rec.calls[0]["body"] and "English part" not in rec.calls[0]["body"]


# --- extract.py ----------------------------------------------------------------------------


def _events(tmp: Path, events: list[dict[str, Any]]) -> str:
    path = tmp / "events.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\nnot json\n", encoding="utf-8")
    return str(path)


def _text(mid: str, text: str) -> dict[str, Any]:
    return {"type": "text", "part": {"messageID": mid, "text": text}}


def _tool(name: str, status: str = "completed") -> dict[str, Any]:
    return {"type": "tool_use", "part": {"tool": name, "state": {"status": status, "input": {}}}}


def test_extract_writes_the_latest_message_with_text(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    events = [_text("m1", "draft"), _tool("skill"), _text("m2", "final "), _text("m2", "answer")]
    assert extract.main(_events(tmp_path, events), str(out)) == 0
    assert out.read_text(encoding="utf-8") == "final answer\n"


def test_extract_falls_back_when_the_last_step_has_no_text(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    events = [_text("m1", "the review"), _text("m2", "   "), _tool("read")]
    assert extract.main(_events(tmp_path, events), str(out)) == 0
    assert out.read_text(encoding="utf-8") == "the review\n"


@pytest.mark.parametrize(
    "events",
    [
        [],
        [_tool("read")],
        [_text("m1", "answer"), {"type": "error", "error": {"name": "APIError"}}],
    ],
    ids=["empty", "tools-only", "error-event"],
)
def test_extract_fails_without_a_clean_answer(tmp_path: Path, events: list[dict[str, Any]]) -> None:
    out = tmp_path / "out.md"
    assert extract.main(_events(tmp_path, events), str(out)) == 1
    assert not out.exists()


def test_a_completed_disallowed_tool_is_a_policy_breach(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    events = [_tool("bash"), _text("m1", "answer")]
    assert extract.main(_events(tmp_path, events), str(out)) == 1
    assert not out.exists()


def test_a_denied_disallowed_tool_is_not_a_breach(tmp_path: Path) -> None:
    out = tmp_path / "out.md"
    events = [_tool("bash", status="error"), _text("m1", "answer")]
    assert extract.main(_events(tmp_path, events), str(out)) == 0
