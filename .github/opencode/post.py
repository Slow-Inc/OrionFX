"""Trusted posting helpers for the OpenCode workflows (stdlib only).

The model never holds a GitHub token. These helpers turn its answer into
GitHub writes that the workflow permits: a COMMENT review, or labels/comments
on a new issue. They validate everything the model chose before writing.

  post.py review ANSWER DIFF REPO PR HEAD_SHA
  post.py triage ANSWER REPO ISSUE BODY_FILE EXISTING_LABELS
"""

import json
import re
import subprocess
import sys
from pathlib import Path

INLINE_RE = re.compile(r"```json opencode-inline\s*\n(.*?)\n```", re.S)
COMPONENTS = {
    "M01-dashboard",
    "M02-market-data",
    "M03-llm-news",
    "M04-prediction",
    "M05-decision",
    "M06-risk-execution",
    "M07-learning-memory",
    "M08-system-core",
    "X01-integration",
    "X02-validation",
    "repo-ops",
}
# wontfix is a maintainer decision; the bot never applies it.
ROLES = {"needs-triage", "needs-info", "ready-for-agent", "ready-for-human"}
MAX_INLINE = 15
VERDICT_RE = re.compile(r"^\s*Verdict:.*$", re.M)
SEVERITY_RE = re.compile(r"^\s*[-*]\s*\[(blocker|major)\]", re.M | re.I)


def verdict_line(summary: str) -> str:
    """The merge gate parses this exact last line: `Verdict: <n> blocker, <n> major`.

    It counts the summary's finding bullets tagged [blocker] / [major]; each finding is
    reported once in the summary (the prompt says so), so inline copies are not counted.
    """
    tags = [m.lower() for m in SEVERITY_RE.findall(summary)]
    return f"Verdict: {tags.count('blocker')} blocker, {tags.count('major')} major"


def gh_api(endpoint: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Right-side line numbers present in the diff, per file."""
    lines: dict[str, set[int]] = {}
    path, new_line, prev = None, 0, ""
    for raw in diff.splitlines():
        is_header = raw.startswith("+++ ") and prev.startswith("--- ")
        prev = raw
        if is_header:
            path = raw[6:] if raw.startswith("+++ b/") else None
            if path is not None:
                lines.setdefault(path, set())
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_line = int(m.group(1)) if m else 0
        elif path is not None and raw[:1] in (" ", "+"):
            lines[path].add(new_line)
            new_line += 1
    return lines


def review(answer_path: str, diff_path: str, repo: str, pr: str, head_sha: str) -> int:
    answer = Path(answer_path).read_text(encoding="utf-8")
    allowed = commentable_lines(Path(diff_path).read_text(encoding="utf-8"))
    comments: list[dict] = []
    off_diff: list[str] = []
    over_cap: list[str] = []
    extra = ""
    match = INLINE_RE.search(answer)
    if match:
        try:
            items = json.loads(match.group(1))
        except ValueError:
            items = []
            print("::warning::The inline-comment block is not valid JSON; kept as text.")
            extra += "\n\n#### Inline block that was not valid JSON\n" + match.group(1)[:3000]
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            path, line, body = item.get("path"), item.get("line"), item.get("body")
            if not isinstance(body, str):
                continue
            entry = f"- `{path}:{line}` {body[:500]}"
            on_diff = (
                isinstance(path, str) and isinstance(line, int) and line in allowed.get(path, set())
            )
            if not on_diff:
                off_diff.append(entry)
            elif len(comments) >= MAX_INLINE:
                over_cap.append(entry)
            else:
                comments.append({"path": path, "line": line, "side": "RIGHT", "body": body[:2000]})
        answer = INLINE_RE.sub("", answer).strip()
    # The verdict counts the summary only: the prompt requires every finding to be in the
    # summary, and the inline entries are copies of some of them (counting both would
    # double-count).
    summary = VERDICT_RE.sub("", answer).strip()
    verdict = verdict_line(summary)
    if off_diff:
        extra += "\n\n#### Not on a diff line\n" + "\n".join(off_diff)
    if over_cap:
        extra += f"\n\n#### Over the {MAX_INLINE}-comment inline cap\n" + "\n".join(over_cap)
    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        "body": f"{summary}{extra}\n\n{verdict}",
        "comments": comments,
    }
    endpoint = f"repos/{repo}/pulls/{pr}/reviews"
    result = gh_api(endpoint, payload)
    if result.returncode != 0 and comments:
        reason = result.stderr.strip()[:300]
        print(f"::warning::Inline comments rejected ({reason}); moving them to the summary.")
        moved = "\n".join(f"- `{c['path']}:{c['line']}` {c['body'][:500]}" for c in comments)
        extra += "\n\n#### Inline comments GitHub rejected\n" + moved
        payload["body"] = f"{summary}{extra}\n\n{verdict}"
        payload["comments"] = []
        result = gh_api(endpoint, payload)
    print(verdict)
    if result.returncode != 0:
        print(f"::error::Posting the review failed: {result.stderr.strip()[:500]}")
        return 1
    print(f"Posted COMMENT review with {len(payload['comments'])} inline comment(s).")
    return 0


def has_thai(text: str) -> bool:
    return re.search(r"[฀-๿]", text) is not None


def has_english(text: str) -> bool:
    # Words outside code spans; identifiers alone do not make an English body.
    prose = re.sub(r"`[^`]*`|```.*?```", " ", text, flags=re.S)
    return len(re.findall(r"\b[A-Za-z]{2,}\b", prose)) >= 3


def first_json_object(text: str) -> dict:
    """The first JSON object in the model's answer, ignoring prose around it."""
    decoder = json.JSONDecoder()
    for start in (m.start() for m in re.finditer(r"\{", text)):
        try:
            value, _ = decoder.raw_decode(text, start)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def triage(answer_path: str, repo: str, issue: str, body_path: str, existing_csv: str) -> int:
    existing = {x for x in existing_csv.split(",") if x}
    choice = first_json_object(Path(answer_path).read_text(encoding="utf-8"))
    labels = []
    component, role = str(choice.get("component")), str(choice.get("role"))
    if not existing & COMPONENTS and component in COMPONENTS:
        labels.append(component)
    if not existing & (ROLES | {"wontfix"}) and role in ROLES:
        labels.append(role)
    if labels:
        result = gh_api(f"repos/{repo}/issues/{issue}/labels", {"labels": labels})
        if result.returncode != 0:
            print(f"::error::Adding labels failed: {result.stderr.strip()[:500]}")
            return 1
    print(f"Labels added: {labels or 'none'} (model chose component={component!r}, role={role!r})")
    body = Path(body_path).read_text(encoding="utf-8")
    checks = (("English", has_english(body)), ("Thai", has_thai(body)))
    missing = [name for name, ok in checks if not ok]
    if missing:
        missing_th = "และ".join("ภาษาอังกฤษ" if m == "English" else "ภาษาไทย" for m in missing)
        note = (
            f"This issue body seems to lack the {' and '.join(missing)} part. "
            "Issue bodies here are bilingual: "
            "English first, then a Thai mirror of the same depth. Please add it.\n\n"
            f"เนื้อหา issue นี้ดูเหมือนยังขาดส่วน{missing_th} "
            "issue ใน repo นี้ต้องเขียนสองภาษา: ภาษาอังกฤษก่อน ตามด้วยภาษาไทยที่ละเอียดเท่ากัน ช่วยเพิ่มด้วย\n\n"
            "_Automated triage by OpenCode (advisory). / คัดแยกอัตโนมัติโดย OpenCode (เป็นคำแนะนำเท่านั้น)_"
        )
        result = gh_api(f"repos/{repo}/issues/{issue}/comments", {"body": note})
        if result.returncode != 0:
            print(f"::error::Commenting failed: {result.stderr.strip()[:500]}")
            return 1
        print(f"Asked for the missing {missing}.")
    return 0


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    if command == "review":
        sys.exit(review(*args))
    if command == "triage":
        sys.exit(triage(*args))
    sys.exit(f"unknown command {command}")
