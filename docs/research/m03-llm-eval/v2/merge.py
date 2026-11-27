# ruff: noqa  (research script, kept as run; not product code)
"""Adjudicate author labels with a blind second labeler.
Agree -> keep; acceptable = union; cap = min of caps.
Disagree -> disputed: acceptable = union of both sets, cap = min(caps, 0.6)."""

import json, sys

cases = json.load(open(sys.argv[1], encoding="utf-8"))
l2 = json.load(open(sys.argv[2], encoding="utf-8"))
agree = dis = 0
for c in cases:
    e, s = c["expected"], l2.get(c["id"])
    if s is None:
        raise SystemExit(f"missing second label: {c['id']}")
    caps = [x for x in (e.get("max_confidence"), s.get("max_confidence")) if x is not None]
    acc = sorted(
        set(e.get("acceptable") or [e["direction"]]) | set(s.get("acceptable") or [s["direction"]])
    )
    if s["direction"] == e["direction"]:
        agree += 1
        e["second_label"] = "agree"
    else:
        dis += 1
        e["second_label"] = f"disputed: second={s['direction']} ({s.get('note', '')[:120]})"
        caps.append(0.6)
    e["acceptable"] = acc
    e["max_confidence"] = min(caps) if caps else None
json.dump(cases, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"{sys.argv[1]}: agree {agree}, disputed {dis}, of {len(cases)}")
