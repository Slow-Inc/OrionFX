"""Extract OpenCode's final answer from `opencode run --format json` events.

Writes the text of the latest assistant message that has text to OUTPUT, logs
the tools used (evidence that skills were loaded) and the token cost, and exits
non-zero on a model error, an empty answer, or a policy breach.

Policy canary: the run is allowed only read/glob/grep/list/skill. If any other
tool ran without being refused (for example bash or edit), the tool policy was not honoured,
so the answer is discarded and nothing is posted.

The optional STATS file is watchdog.py's record of the attempt (outcome, duration,
event count, longest silence). It names the stall or hard-cap budget in the cause, and
one line per attempt goes to the job summary so the budgets can be tuned from evidence.
"""

import json
import os
import sys
from decimal import Decimal

ALLOWED_TOOLS = {"read", "glob", "grep", "list", "skill"}
HARD_CAP, STALLED = 124, 125  # watchdog.py exit statuses
KILLED = 137  # SIGKILL from outside the watchdog (the job, the runner, the OOM killer)
RETRY = 75  # exit code: the model ended cleanly with no answer text; the caller may retry once


def _cause(text: str) -> int:
    """Record why the run failed (see .github/opencode/post.py `classify`); return 1."""
    path = os.environ.get("OPENCODE_CAUSE_FILE")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1


def _minutes(seconds: object) -> str:
    """`4 min` for whole minutes, else `2m30s`; `?` when the stats are missing."""
    if not isinstance(seconds, int | float):
        return "?"
    whole = int(seconds)
    return f"{whole // 60} min" if whole % 60 == 0 else f"{whole // 60}m{whole % 60:02d}s"


def _read_stats(stats_path: str | None) -> dict[str, object]:
    if not stats_path:
        return {}
    try:
        with open(stats_path, encoding="utf-8") as fh:
            stats = json.load(fh)
    except (OSError, ValueError):
        return {}
    return stats if isinstance(stats, dict) else {}


def _summarise(stats: dict[str, object], usage: str) -> None:
    """One line per attempt in the job summary: the evidence to tune the budgets from."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not stats:
        return
    line = (
        f"- OpenCode attempt: {stats.get('outcome')} (status {stats.get('status')}) after "
        f"{_minutes(stats.get('elapsed_seconds'))}, {stats.get('events')} events, longest "
        f"silence {_minutes(stats.get('longest_silence_seconds'))}; budgets idle "
        f"{_minutes(stats.get('idle_seconds'))}, hard {_minutes(stats.get('hard_seconds'))}; "
        f"{usage}\n"
    )
    # Best effort: an unwritable summary must never stop the cause (or a breach) from
    # being recorded below.
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"::warning::Could not write the job summary: {exc}")


def main(
    events_path: str, output_path: str, exit_status: int = 0, stats_path: str | None = None
) -> int:
    """Return 0 (answer written), RETRY (no answer after a clean exit), or 1 (cause recorded)."""
    messages: dict[str, list[str]] = {}
    order: list[str] = []
    errors: list[str] = []
    breaches: list[str] = []
    cost = Decimal(0)
    tokens_in = tokens_out = 0
    with open(events_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            part = event.get("part") or {}
            kind = event.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                mid = part.get("messageID", "")
                if mid not in messages:
                    messages[mid] = []
                    order.append(mid)
                messages[mid].append(part["text"])
            elif kind == "tool_use":
                state = part.get("state") or {}
                tool, status = part.get("tool"), state.get("status")
                args = json.dumps(state.get("input", {}))[:160]
                print(f"tool: {tool} {args} -> {status}")
                # Only a refused call ("error") is safe; any other status of a
                # disallowed tool (completed, or anything unexpected) is a breach.
                if tool not in ALLOWED_TOOLS and status != "error":
                    breaches.append(str(tool))
            elif kind == "step_finish":
                cost += Decimal(str(part.get("cost") or 0))
                tok = part.get("tokens") or {}
                cache_read = (tok.get("cache") or {}).get("read") or 0
                tokens_in += int(tok.get("input") or 0) + int(cache_read)
                tokens_out += int(tok.get("output") or 0)
            elif kind == "error":
                errors.append(json.dumps(event.get("error"))[:500])
    usage = f"usage: input~{tokens_in} tokens, output~{tokens_out} tokens, cost ${cost:.4f}"
    print(usage)
    stats = _read_stats(stats_path)
    _summarise(stats, usage)
    for err in errors:
        print(f"::error::OpenCode error: {err}")
    # Cause precedence: policy breach (security, red) > stall > hard cap > killed (137) >
    # model error > no answer (the only retryable case) > non-zero exit status.
    if breaches:
        print(f"::error::Tool policy breach: {sorted(set(breaches))} completed; nothing is posted.")
        return _cause(f"security: tool policy breach, {sorted(set(breaches))} completed")
    if exit_status == STALLED:
        idle = _minutes(stats.get("idle_seconds"))
        return _cause(f"unavailable: OpenCode stalled (no progress for {idle})")
    if exit_status == HARD_CAP:
        # The configured cap for all attempts, not what was left of it for a retry.
        configured = os.environ.get("HARD_MINUTES", "")
        hard = f"{configured} min" if configured.isdigit() else _minutes(stats.get("hard_seconds"))
        return _cause(f"unavailable: OpenCode hit the hard cap ({hard})")
    if exit_status == KILLED:
        return _cause(f"unavailable: OpenCode was killed (status {exit_status})")
    # The latest message that has text: a run can end on a tool-only step.
    texts = ["".join(messages[mid]).strip() for mid in order]
    answer = next((t for t in reversed(texts) if t), "")
    if answer and not texts[-1]:
        print("::warning::The last assistant message had no text; using the latest one that did.")
    if errors:
        return _cause(f"unavailable: model error {errors[0][:200]}")
    if not answer:
        print("::error::OpenCode produced no final answer.")
        _cause("unavailable: OpenCode produced no final answer")
        return RETRY if exit_status == 0 else 1
    if exit_status != 0:
        return _cause(f"unavailable: OpenCode exited with status {exit_status}")
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(answer + "\n")
    return 0


if __name__ == "__main__":
    status = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    sys.exit(main(sys.argv[1], sys.argv[2], status, sys.argv[4] if len(sys.argv) > 4 else None))
