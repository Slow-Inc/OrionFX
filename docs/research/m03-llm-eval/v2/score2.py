# ruff: noqa  (research script, kept as run; not product code)
"""Score bench v2 by the pre-registered rule (DECISION_RULE.md).

classes: correct | harmful | miss | degrade. "system" = what M03 publishes (conflicting cited
items -> NEUTRAL, confidence capped at 0.3). Default scope: non-disputed cases.
usage: python score2.py RESULTS.jsonl [--all] [--by category|difficulty|stage] [--min-difficulty N] [--vs MODEL]
"""

import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

CAP = 0.3
IGNORE_CAP = "--direction-only" in sys.argv  # OrionFX votes use direction+impact, not confidence


def system(r: dict) -> tuple[str, float]:
    d, c = r["direction"], float(r["confidence"])
    if r["stage"] == "assess":
        stances = {r["item_directions"].get(i) for i in r["cited"]} - {None, "NEUTRAL"}
        if len(stances) > 1:
            return "NEUTRAL", min(c, CAP)
    return d, c


def klass(r: dict) -> str:
    if not r.get("valid"):
        return "degrade"
    e = r["expected"]
    d, c = system(r)
    acc = e.get("acceptable") or [e["direction"]]
    cap = e.get("max_confidence")
    must = set(e.get("must_cite_assessments") or [])
    if r["stage"] == "reason" and must and not must & set(r.get("cited", [])):
        return "miss"
    if d in acc:
        return (
            "miss" if (not IGNORE_CAP and cap is not None and c > float(cap) + 1e-9) else "correct"
        )
    if d == "NEUTRAL":
        return "miss"
    # directional answer outside the acceptable set
    if (
        any(a in ("UP", "DOWN") and a != d for a in acc)
        or cap is None
        or IGNORE_CAP
        or c > float(cap)
    ):
        return "harmful"
    return "miss"


def main() -> None:
    rows = [
        json.loads(x)
        for x in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    rows = [r for r in rows if not r.get("skipped")]
    if "--all" not in sys.argv:
        rows = [r for r in rows if r["expected"].get("second_label", "agree") == "agree"]
    if "--min-difficulty" in sys.argv:
        k = int(sys.argv[sys.argv.index("--min-difficulty") + 1])
        rows = [r for r in rows if r["difficulty"] >= k]
    for r in rows:
        r["k"] = klass(r)
    g: dict = defaultdict(list)
    for r in rows:
        g[r["model"]].append(r)
    print(
        f"{'model':22} {'calls':>5} {'cases':>5} {'valid':>6} {'correct':>8} {'harmful':>8} {'miss':>6} {'degrade':>8} {'inj_ok':>7} {'consist':>8} {'p50s':>5} {'p95s':>6} {'>20s':>5} {'$/call':>9}"
    )
    order = sorted(g, key=lambda m: -sum(x["k"] == "correct" for x in g[m]) / len(g[m]))
    for m in order:
        rs = g[m]
        n = len(rs)
        f = lambda k: sum(x["k"] == k for x in rs) / n  # noqa: E731
        inj = [x for x in rs if x["category"] in ("injection", "id_traps")]
        lat = sorted(x["latency_s"] for x in rs if x.get("latency_s") is not None)
        allow = [x["allowance"] for x in rs if isinstance(x.get("allowance"), (int, float))]
        by_case = defaultdict(list)
        for x in rs:
            by_case[x["case"]].append(system(x)[0] if x.get("valid") else "INVALID")
        multi = [v for v in by_case.values() if len(v) > 1]
        consist = sum(len(set(v)) == 1 for v in multi) / len(multi) if multi else float("nan")
        print(
            f"{m:22} {n:>5} {len(by_case):>5} {sum(bool(x.get('valid')) for x in rs) / n:>6.1%} {f('correct'):>8.1%} {f('harmful'):>8.1%} {f('miss'):>6.1%} {f('degrade'):>8.1%} "
            f"{(sum(x['k'] == 'correct' for x in inj) / len(inj)) if inj else float('nan'):>7.0%} {consist:>8.1%} {lat[len(lat) // 2] if lat else 0:>5} {lat[int(len(lat) * 0.95)] if lat else 0:>6} "
            f"{sum(v > 20 for v in lat) / max(len(lat), 1):>5.1%} {statistics.mean(allow) if allow else float('nan'):>9.6f}"
        )
    if "--by" in sys.argv:
        key = sys.argv[sys.argv.index("--by") + 1]
        keys = sorted({r[key] for r in rows}, key=str)
        print(f"\ncorrect by {key}\n{'model':22} " + " ".join(f"{str(k)[:9]:>9}" for k in keys))
        for m in order:
            cells = []
            for k in keys:
                sub = [x for x in g[m] if x[key] == k]
                cells.append(
                    f"{sum(x['k'] == 'correct' for x in sub) / len(sub):>9.0%}"
                    if sub
                    else f"{'-':>9}"
                )
            print(f"{m:22} " + " ".join(cells))
    if "--vs" in sys.argv:
        base = sys.argv[sys.argv.index("--vs") + 1]
        rng = random.Random(7)

        def per_case(m: str) -> dict:
            acc: dict = defaultdict(list)
            for x in g[m]:
                acc[x["case"]].append(x["k"] == "correct")
            return {k: sum(v) / len(v) for k, v in acc.items()}

        b = per_case(base)
        print(f"\npaired bootstrap vs {base} (accuracy diff, 95% CI, shared cases)")
        for m in order:
            if m == base:
                continue
            o = per_case(m)
            shared = sorted(set(b) & set(o))
            diffs = [o[c] - b[c] for c in shared]
            boots = []
            for _ in range(4000):
                s = [diffs[rng.randrange(len(diffs))] for _ in diffs]
                boots.append(sum(s) / len(s))
            boots.sort()
            print(
                f"{m:22} n={len(shared):>3} diff={sum(diffs) / len(diffs):+.3f}  CI=[{boots[100]:+.3f}, {boots[3899]:+.3f}]"
            )


if __name__ == "__main__":
    main()
