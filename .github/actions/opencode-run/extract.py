"""Extract OpenCode's final answer from `opencode run --format json` events.

Writes the text of the last assistant message to OUTPUT, logs the tools used
(evidence that skills were loaded) and the token cost, and exits non-zero on a
model error or an empty answer.
"""

import json
import sys


def main(events_path: str, output_path: str) -> int:
    messages: dict[str, list[str]] = {}
    order: list[str] = []
    errors: list[str] = []
    cost = 0.0
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
                args = json.dumps(state.get("input", {}))[:160]
                print(f"tool: {part.get('tool')} {args} -> {state.get('status')}")
            elif kind == "step_finish":
                cost += float(part.get("cost") or 0)
                tok = part.get("tokens") or {}
                cache_read = (tok.get("cache") or {}).get("read") or 0
                tokens_in += int(tok.get("input") or 0) + int(cache_read)
                tokens_out += int(tok.get("output") or 0)
            elif kind == "error":
                errors.append(json.dumps(event.get("error"))[:500])
    print(f"usage: input~{tokens_in} tokens, output~{tokens_out} tokens, cost ${cost:.4f}")
    for err in errors:
        print(f"::error::OpenCode error: {err}")
    answer = "".join(messages[order[-1]]).strip() if order else ""
    if errors or not answer:
        if not answer:
            print("::error::OpenCode produced no final answer.")
        return 1
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(answer + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
