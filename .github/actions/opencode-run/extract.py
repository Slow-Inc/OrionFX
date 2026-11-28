"""Extract OpenCode's final answer from `opencode run --format json` events.

Writes the text of the latest assistant message that has text to OUTPUT, logs
the tools used (evidence that skills were loaded) and the token cost, and exits
non-zero on a model error, an empty answer, or a policy breach.

Policy canary: the run is allowed only read/glob/grep/list/skill. If any other
tool ran without being refused (for example bash or edit), the tool policy was not honoured,
so the answer is discarded and nothing is posted.
"""

import json
import os
import sys
from decimal import Decimal

ALLOWED_TOOLS = {"read", "glob", "grep", "list", "skill"}
TIMEOUT_STATUSES = {124, 137}  # `timeout` and its --kill-after SIGKILL
RETRY = 75  # exit code: the model ended cleanly with no answer text; the caller may retry once


def _cause(text: str) -> int:
    """Record why the run failed (see .github/opencode/post.py `classify`); return 1."""
    path = os.environ.get("OPENCODE_CAUSE_FILE")
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1


def main(events_path: str, output_path: str, exit_status: int = 0) -> int:
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
    print(f"usage: input~{tokens_in} tokens, output~{tokens_out} tokens, cost ${cost:.4f}")
    for err in errors:
        print(f"::error::OpenCode error: {err}")
    # Cause precedence: policy breach (security, red) > timeout > model error >
    # no answer (the only retryable case) > non-zero exit status.
    if breaches:
        print(f"::error::Tool policy breach: {sorted(set(breaches))} completed; nothing is posted.")
        return _cause(f"security: tool policy breach, {sorted(set(breaches))} completed")
    if exit_status in TIMEOUT_STATUSES:
        return _cause(f"unavailable: OpenCode timed out (status {exit_status})")
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
    sys.exit(main(sys.argv[1], sys.argv[2], status))
