"""Trusted posting helpers for the OpenCode workflows (stdlib only).

The model never holds a GitHub token. These helpers turn its answer into
GitHub writes that the workflow permits: a COMMENT review, or labels/comments
on a new issue. They validate everything the model chose before writing.

  post.py review ANSWER DIFF REPO PR HEAD_SHA SKILLS_JSON
  post.py issue-context ISSUE_JSON   (linked-issue context; body only from collaborators)
  post.py triage ANSWER REPO ISSUE BODY_FILE EXISTING_LABELS
  post.py notice CAUSE_FILE REPO PR HEAD_SHA
  post.py skills FILES_TXT   (prints {"run": [...], "skipped": [...]})
  post.py cause-for-post RC  (prints the cause line for a posting exit status)
"""

import json
import os
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
AUTH_FAILURE = 2  # review() exit code for HTTP 401/403 (config), vs 1 (transient)
# Only post.py writes a verdict: any model "Verdict:" (bulleted, bold, quoted...) is
# renamed rather than deleted, so a finding line that mentions it is kept and counted.
VERDICT_RE = re.compile(r"\bVerdict(\W*:)", re.I)
FIRST_LINE_RE = re.compile(r"\A\s*\**OpenCode review of[^\n]*\n?", re.I)
SEVERITY_RE = re.compile(r"\[(blocker|major)\]", re.I)
FAIL_ROW_RE = re.compile(r"^\s*\|\s*(\d+)\s*\|.*\|\s*fail\s*\|", re.I)
NEVER_LIST_GATE = "10"  # t4-gates item whose fail is a blocker (see its SKILL.md)
MALFORMED = 5  # review() exit code: the answer lacks a required section; nothing posted


def verdict_line(summary: str, inline_bodies: list[str] | tuple[str, ...] = ()) -> str:
    """The merge gate parses this exact last line: `Verdict: <n> blocker, <n> major`.

    It must never undercount, so it overcounts: every [blocker]/[major] tag anywhere in a
    line of the summary or of any inline comment body counts (inline copies of summary
    findings count twice), and each failed t4-gates table row counts as a major, or a
    blocker for the never-list gate.
    """
    blockers = majors = 0
    for text in (summary, *inline_bodies):
        for line in text.split("\n"):
            tags = [t.lower() for t in SEVERITY_RE.findall(line)]
            blockers += tags.count("blocker")
            majors += tags.count("major")
            row = FAIL_ROW_RE.match(line)
            if row and not tags:
                if row.group(1) == NEVER_LIST_GATE:
                    blockers += 1
                else:
                    majors += 1
    return f"Verdict: {blockers} blocker, {majors} major"


def missing_sections(summary: str, skills_path: str) -> list[str]:
    """The `### <skill>` sections and the `Skipped:` line a well-formed review must have."""
    selected = json.loads(Path(skills_path).read_text(encoding="utf-8"))
    headings = {
        m.group(1).lower() for m in re.finditer(r"^#{2,4}\s*([a-z0-9-]+)", summary, re.M | re.I)
    }
    missing = [f"### {s}" for s in selected["run"] if s.lower() not in headings]
    if not re.search(r"^\W*Skipped\b", summary, re.M | re.I):
        missing.append("Skipped:")
    return missing


def linked_issue_text(issue: dict) -> str:
    """Context for a linked issue: the body only when a collaborator wrote it (M2)."""
    title = str(issue.get("title") or "")
    association = str(issue.get("authorAssociation") or "")
    if association in {"OWNER", "MEMBER", "COLLABORATOR"}:
        return f"# {title}\n\n{issue.get('body') or ''}\n"
    return f"# {title}\n\n(Body omitted: the author is not a collaborator ({association}).)\n"


def gh_api(endpoint: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", "api", "--method", "POST", endpoint, "--input", "-"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
    )


HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Right-side line numbers present in the diff, per file.

    Hunk bodies are consumed by their declared line counts, so content lines that look
    like headers (`--- x` removed, `+++ y` added) are never mistaken for file headers.
    """
    lines: dict[str, set[int]] = {}
    path: str | None = None
    new_line = old_left = new_left = 0
    # split("\n"), not splitlines(): content may hold \u2028, \x0b and other separators.
    for raw in diff.split("\n"):
        raw = raw.removesuffix("\r")
        if old_left > 0 or new_left > 0:  # inside a hunk body
            tag = raw[:1]
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag in (" ", "+") and path is not None:
                lines[path].add(new_line)
            if tag in (" ", "+"):
                new_line += 1
                new_left -= 1
            if tag in (" ", "-"):
                old_left -= 1
            continue
        if raw.startswith("+++ "):
            # git appends a TAB to header paths that contain spaces.
            path = raw[6:].removesuffix("\t") if raw.startswith("+++ b/") else None
            if path is not None:
                lines.setdefault(path, set())
        elif m := HUNK_RE.match(raw):
            old_left = int(m.group(1) or 1)
            new_line = int(m.group(2))
            new_left = int(m.group(3) or 1)
    return lines


def _one_line(text: str) -> str:
    """Flatten model text placed in our own sections, so it cannot fake a `Verdict:` line."""
    return VERDICT_RE.sub(r"Model-verdict\1", re.sub(r"\s*[\r\n]+\s*", " ", text))


def review(
    answer_path: str,
    diff_path: str,
    repo: str,
    pr: str,
    head_sha: str,
    skills_path: str | None = None,
) -> int:
    answer = Path(answer_path).read_text(encoding="utf-8")
    allowed = commentable_lines(Path(diff_path).read_text(encoding="utf-8"))
    comments: list[dict] = []
    off_diff: list[str] = []
    over_cap: list[str] = []
    inline_bodies: list[str] = []
    extra = ""
    # The last block wins: an earlier one is usually an example such as "[]".
    blocks = list(INLINE_RE.finditer(answer))
    match = blocks[-1] if blocks else None
    # Earlier blocks are not posted, but their severities still count (never undercount).
    inline_bodies.extend(block.group(1) for block in blocks[:-1])
    if match:
        try:
            items = json.loads(match.group(1))
        except ValueError:
            items = []
            print("::warning::The inline-comment block is not valid JSON; kept as text.")
            raw_block = match.group(1)[:3000]
            inline_bodies.append(raw_block)
            extra += "\n\n#### Inline block that was not valid JSON\n" + _one_line(raw_block)
        if not isinstance(items, list):
            # Valid JSON but not an array: keep it visible and let its severities count.
            raw_block = match.group(1)[:3000]
            inline_bodies.append(raw_block)
            extra += "\n\n#### Inline block that was not a JSON array\n" + _one_line(raw_block)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            path, line, body = item.get("path"), item.get("line"), item.get("body")
            if not isinstance(body, str):
                continue
            inline_bodies.append(body)
            entry = f"- `{_one_line(str(path))}:{_one_line(str(line))}` {_one_line(body[:500])}"
            on_diff = (
                isinstance(path, str)
                and type(line) is int  # bool is an int subclass
                and line in allowed.get(path, set())
            )
            if not on_diff:
                off_diff.append(entry)
            elif len(comments) >= MAX_INLINE:
                over_cap.append(entry)
            else:
                comments.append({"path": path, "line": line, "side": "RIGHT", "body": body[:2000]})
        answer = INLINE_RE.sub("", answer).strip()
    summary = VERDICT_RE.sub(r"Model-verdict\1", answer).strip()
    if skills_path is not None:
        missing = missing_sections(summary, skills_path)
        if missing:
            print(f"::error::Malformed review, missing {missing}; nothing is posted.")
            return MALFORMED
    # Overcounts on purpose (summary + inline bodies + failed gate rows); see verdict_line.
    verdict = verdict_line(summary, inline_bodies)
    # The gate finds the review by this exact first line, so post.py owns it.
    summary = FIRST_LINE_RE.sub("", summary).strip()
    summary = f"OpenCode review of {head_sha} (advisory)\n\n{summary}"
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
        moved = "\n".join(
            f"- `{c['path']}:{c['line']}` {_one_line(c['body'][:500])}" for c in comments
        )
        extra += "\n\n#### Inline comments GitHub rejected\n" + moved
        payload["body"] = f"{summary}{extra}\n\n{verdict}"
        payload["comments"] = []
        result = gh_api(endpoint, payload)
    print(verdict)
    if result.returncode != 0:
        print(f"::error::Posting the review failed: {result.stderr.strip()[:500]}")
        # 401/403 means the App token lacks a permission: a config error, not an outage.
        return AUTH_FAILURE if re.search(r"HTTP 40[13]", result.stderr) else 1
    print(f"Posted COMMENT review with {len(payload['comments'])} inline comment(s).")
    return 0


def has_thai(text: str) -> bool:
    return re.search(r"[฀-๿]", text) is not None


def has_english(text: str) -> bool:
    # Words outside code spans; identifiers alone do not make an English body.
    # Only lines with no Thai count, so a Thai body that uses English technical terms
    # is not mistaken for an English section.
    prose = re.sub(r"```.*?```|`[^`]*`", " ", text, flags=re.S)
    latin_lines = (line for line in prose.splitlines() if not has_thai(line))
    return any(len(re.findall(r"\b[A-Za-z]{2,}\b", line)) >= 3 for line in latin_lines)


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


SECURITY_PATHS = re.compile(
    r"^(\.env|backend/app/core/(secrets|config)\.py|backend/app/api/auth\.py"
    r"|\.github/|\.claude/|\.githooks/|\.opencode/)"
)
CODE_PATHS = re.compile(r"(\.py|\.ts|\.tsx)$|^(backend|frontend/src|scripts|migrations)/")


def select_skills(files: list[str]) -> tuple[list[str], list[str]]:
    """Map the PR's changed files to (skills to run, "skill — reason" skipped)."""
    run = ["code-review", "scrutinize", "simplify"]
    skipped = []
    conditional = (
        ("security-review", SECURITY_PATHS, "no security paths touched"),
        ("test-quality", CODE_PATHS, "no code changes"),
        ("ui-audit", re.compile(r"^frontend/"), "no frontend changes"),
    )
    for skill, pattern, reason in conditional:
        if any(pattern.search(f) for f in files):
            run.append(skill)
        else:
            skipped.append(f"{skill} — {reason}")
    return [*run, "t4-gates"], skipped


def skills(files_path: str) -> int:
    files = [f.strip() for f in Path(files_path).read_text(encoding="utf-8").splitlines()]
    run, skipped = select_skills([f for f in files if f])
    print(json.dumps({"run": run, "skipped": skipped}, ensure_ascii=False))
    return 0


def cause_for_post(rc: int) -> str:
    """Map the posting step's exit status (with-app-token.sh + review) to a cause line."""
    if rc == 0:
        return ""
    if rc == 3:
        return "security: the OpenCode App token was not scoped to exactly this repository"
    if rc == AUTH_FAILURE:
        return "config: GitHub refused the review (401/403); check the OpenCode App's permissions"
    if rc == 4:
        return "unavailable: the OpenCode App token exchange failed"
    if rc == MALFORMED:
        return "unavailable: malformed review"
    return "unavailable: posting the review to GitHub failed"


# Causes that mean a broken setup or a broken boundary: fail the check red. Everything
# else is OpenCode being unavailable: degrade to a notice and keep the check green.
RED_CATEGORIES = {"config", "security"}
UNKNOWN_CAUSE = "unavailable: an OpenCode step failed without a recorded cause (see the job log)"


def classify(cause: str) -> tuple[str, str]:
    """Split a recorded cause `<category>: <detail>` into (category, detail).

    An empty or malformed cause is "unavailable": only an explicit config or security
    cause may turn the check red.
    """
    text = cause.strip().splitlines()[0].strip() if cause.strip() else UNKNOWN_CAUSE
    category, sep, detail = text.partition(":")
    category = category.strip().lower()
    if not sep or category not in RED_CATEGORIES | {"unavailable"}:
        return "unavailable", text
    return category, detail.strip()


def notice(cause_path: str, repo: str, pr: str, head_sha: str) -> int:
    path = Path(cause_path)
    category, detail = classify(path.read_text(encoding="utf-8") if path.exists() else "")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if category in RED_CATEGORIES:
        line = f"OpenCode {category} error: {detail}"
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(f"### {line}\n")
        print(f"::error::{line}")
        return 1
    line = f"OpenCode unavailable: {detail}"
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(f"### {line}\n\nNo OpenCode review was posted for `{head_sha}`.\n")
    body = (
        f"**{line}**\n\nNo OpenCode review was posted for `{head_sha}`. The developer runs the "
        "fallback review. This is an issue comment on purpose, not a PR review.\n\n"
        f"**OpenCode ใช้งานไม่ได้:** {detail}\n\nไม่มีรีวิวจาก OpenCode สำหรับ `{head_sha}` "
        "developer จะรันรีวิวสำรองแทน และนี่เป็นคอมเมนต์ธรรมดาโดยตั้งใจ ไม่ใช่ PR review"
    )
    result = gh_api(f"repos/{repo}/issues/{pr}/comments", {"body": body})
    print(f"::warning::{line}")
    if result.returncode != 0:
        reason = result.stderr.strip()[:300]
        if re.search(r"HTTP 40[13]", result.stderr):
            # A refused notice is a broken setup (the job token lacks pull-requests: write):
            # fail red, or the PR would get neither a review nor a notice behind a green check.
            print(f"::error::OpenCode config error: GitHub refused the notice: {reason}")
            if summary_path:
                with open(summary_path, "a", encoding="utf-8") as fh:
                    fh.write("### OpenCode config error: GitHub refused the unavailable notice\n")
            return 1
        print(f"::warning::Could not post the notice: {reason}")
    return 0


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    if command == "review":
        sys.exit(review(*args))
    if command == "triage":
        sys.exit(triage(*args))
    if command == "notice":
        sys.exit(notice(*args))
    if command == "skills":
        sys.exit(skills(*args))
    if command == "issue-context":
        issue_json = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        print(linked_issue_text(issue_json), end="")
        sys.exit(0)
    if command == "cause-for-post":
        print(cause_for_post(int(args[0])))
        sys.exit(0)
    sys.exit(f"unknown command {command}")
