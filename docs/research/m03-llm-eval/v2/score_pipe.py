# ruff: noqa  (research script, kept as run; not product code)
"""Score the end-to-end OrionFX pipeline run (pipeline.py output)."""

import json
import sys
from collections import defaultdict
from pathlib import Path

rows = [
    json.loads(x) for x in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines() if x.strip()
]
g = defaultdict(list)
for r in rows:
    g[r["model"]].append(r)


def rate(xs: list, pred) -> str:  # type: ignore[no-untyped-def]
    xs = list(xs)
    return (
        f"{sum(1 for x in xs if pred(x))}/{len(xs)} ({sum(1 for x in xs if pred(x)) / len(xs):.1%})"
        if xs
        else "-"
    )


for m, rs in g.items():
    ok = [r for r in rs if "crash" not in r]
    calls = [c for r in ok for c in r.get("calls", [])]
    print(
        f"\n== {m}: {len(rs)} cases, crashes {len(rs) - len(ok)}, LLM calls {len(calls)}, timeouts {sum(1 for c in calls if c.get('timeout'))}, "
        f"HTTP errors {sum(1 for c in calls if 'status' in c)}, allowance ${sum(c.get('allowance') or 0 for c in calls):.4f}"
    )
    print("  M03 assess DEGRADED/DOWN :", rate(ok, lambda r: r["assess_status"] != "OK"))
    print(
        "  M03 reason DEGRADED       :",
        rate([r for r in ok if r["reason_status"]], lambda r: r["reason_status"] != "OK"),
    )
    direct = [r for r in ok if r["truth"] in ("UP", "DOWN")]
    print(
        "  news direction correct    :",
        rate(direct, lambda r: r["news_dir"] in (r.get("acceptable") or [r["truth"]])),
    )
    sc = lambda r, k: (r.get("scenarios") or {}).get(k, {}).get("action")  # noqa: E731
    print("  E1 good entry taken       :", rate(direct, lambda r: sc(r, "E1") == "ENTER"))
    print(
        "  E2 bad entry taken (!)    :",
        rate(direct, lambda r: sc(r, "E2") == "ENTER"),
        " <- model-independent under current M05 voting",
    )
    print("  M1 wrong-side hold exited :", rate(direct, lambda r: sc(r, "M1") == "EXIT"))
    print("  M2 right-side hold exited :", rate(direct, lambda r: sc(r, "M2") == "EXIT"), " (bad)")
    neutral = [r for r in ok if r["truth"] == "NEUTRAL"]
    print("  N  spurious exit (neutral):", rate(neutral, lambda r: sc(r, "N") == "EXIT"), " (bad)")
    lat = sorted(c["latency_s"] for c in calls)
    if lat:
        print(
            f"  latency p50 {lat[len(lat) // 2]}s p95 {lat[int(len(lat) * 0.95)]}s max {lat[-1]}s"
        )
