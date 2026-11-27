# ruff: noqa  (research script, kept as run; not product code)
"""M03 LLM benchmark v2: assessment + reasoning stages, repeats, budget guard.

Prompts and validators are M03's own (build_prompt/ANSWER_FORMAT/_parse for
assessment; reasoning._prompt/_Answer for reasoning). One attempt per call
(transport errors retried once, counted separately). The key is read from the
repo-root .env and never printed or stored.

usage: uv run python bench.py CASES.json OUT.jsonl MODEL:EFFORT:REPEATS[,...] [--workers 4] [--stop-at 60]
"""

import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.core.enums import Direction
from app.news import reasoning as R
from app.news.assess import ANSWER_FORMAT, _parse
from app.news.injection import build_prompt, injection_flags
from app.news.llm_call import load_json
from app.news.sources import NewsItem

ROOT = Path(__file__).resolve()
while not (ROOT / "pyproject.toml").exists() and ROOT.parent != ROOT:
    ROOT = ROOT.parent
BASE = "https://opencode.ai/inference/go"
NOW = datetime(2027, 3, 1, 12, 0, tzinfo=UTC)
_lock = threading.Lock()
_stop = threading.Event()


def _key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("OPENCODE_API_KEY="):
            k = line.split("=", 1)[1].strip().strip('"').strip("'")
            if k:
                return k
    raise SystemExit("OPENCODE_API_KEY is empty in .env")


KEY = _key()


def _headers(session: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {KEY}",
        "x-opencode-session": session,
        "User-Agent": "orionfx-m03-bench/0.2",
    }


def usage(client: httpx.Client) -> dict:
    r = client.get(f"{BASE}/v1/usage", headers=_headers("orionfx-bench-usage"))
    return r.json().get("usage", {}) if r.status_code == 200 else {}


def news_items(case: dict) -> list[NewsItem]:
    return [
        NewsItem(
            item_id=i["id"],
            source="wire",
            url=f"https://example.com/{i['id']}",
            title=i["title"],
            body=i.get("body", ""),
            published_at=NOW - timedelta(minutes=5),
            fetched_at=NOW - timedelta(minutes=4),
            valid_until=NOW + timedelta(hours=2),
        )
        for i in case["items"]
    ]


def reasoning_prompt(case: dict) -> tuple[str, set[str], set[str]]:
    a = [
        SimpleNamespace(
            assessment_id=x["id"],
            direction=Direction(x["direction"]),
            impact=x["impact"],
            score=SimpleNamespace(
                value=x["confidence"],
                semantics="uncalibrated LLM confidence in the news-implied XAUUSD direction",
            ),
        )
        for x in case["assessments"]
    ]
    p = [
        SimpleNamespace(
            prediction_id=x["id"],
            direction=Direction(x["direction"]),
            horizon_bars=x["horizon_bars"],
            horizon=x["horizon"],
            probability=SimpleNamespace(value=x["score"], semantics=x["semantics"]),
        )
        for x in case["predictions"]
    ]
    feats = {k: str(v) for k, v in sorted(case.get("features", {}).items())}
    return R._prompt(a, p, feats), {x.assessment_id for x in a}, {x.prediction_id for x in p}


def call(
    client: httpx.Client, model: str, effort: str | None, prompt: str, session: str
) -> tuple[str, dict, float, int]:
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 8000,
    }
    if effort:
        body["reasoning_effort"] = effort
    transport_retries = 0
    for attempt in range(2):
        t0 = time.monotonic()
        try:
            r = client.post(
                f"{BASE}/openai/v1/chat/completions", json=body, headers=_headers(session)
            )
        except httpx.HTTPError as exc:
            if attempt == 0:
                transport_retries += 1
                time.sleep(5)
                continue
            raise RuntimeError(f"transport: {type(exc).__name__}") from None
        dt = time.monotonic() - t0
        if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
            transport_retries += 1
            time.sleep(8)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:160]}")
        data = r.json()
        return (
            data["choices"][0]["message"].get("content") or "",
            data.get("usage") or {},
            dt,
            transport_retries,
        )
    raise RuntimeError("unreachable")


def run_one(
    client: httpx.Client, model: str, effort: str | None, rep: int, case: dict, session: str
) -> dict:
    rec: dict = {
        "model": model,
        "effort": effort,
        "rep": rep,
        "case": case["id"],
        "category": case["category"],
        "difficulty": case["difficulty"],
        "expected": case["expected"],
    }
    if case["category"] == "reasoning":
        prompt, a_ids, p_ids = reasoning_prompt(case)
        stage = "reason"
    else:
        items = news_items(case)
        flagged = [i.item_id for i in items if injection_flags(f"{i.title}\n{i.body}")]
        rec["prefiltered"] = flagged
        items = [i for i in items if i.item_id not in flagged]
        if not items:
            rec.update(stage="assess", skipped="all items pre-filtered by M03")
            return rec
        prompt = build_prompt(items, ANSWER_FORMAT)
        given = {i.item_id for i in items}
        stage = "assess"
    rec["stage"] = stage
    try:
        text, use, dt, tr = call(client, model, effort, prompt, session)
    except Exception as exc:
        rec.update(valid=False, error=str(exc)[:200], call_failed=True)
        return rec
    oc = use.get("opencode") or {}
    rec.update(
        latency_s=round(dt, 2),
        transport_retries=tr,
        allowance=oc.get("allowance"),
        list_cost=oc.get("list_cost"),
        tokens=oc.get("tokens"),
        cached=(use.get("prompt_tokens_details") or {}).get("cached_tokens"),
    )
    try:
        if stage == "assess":
            a = _parse(text, given)
            rec.update(
                valid=True,
                direction=a.direction.value,
                confidence=str(a.confidence),
                impact=str(a.impact),
                cited=a.cited_ids,
                item_directions={k: v.value for k, v in a.item_directions.items()},
                rationale=a.rationale[:300],
            )
        else:
            a = R._Answer.model_validate(load_json(text))
            unknown = (set(a.cited_assessment_ids) - a_ids) | (set(a.cited_prediction_ids) - p_ids)
            if unknown:
                raise ValueError(
                    f"cites {len(unknown)} id(s) it was not given: {sorted(unknown)[:3]}"
                )
            rec.update(
                valid=True,
                direction=a.direction.value,
                confidence=str(a.confidence),
                cited=a.cited_assessment_ids + a.cited_prediction_ids,
                rationale=a.rationale[:300],
            )
    except Exception as exc:
        rec.update(valid=False, error=f"{type(exc).__name__}: {str(exc)[:200]}", text=text[:400])
    return rec


def main() -> None:
    cases = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    out = Path(sys.argv[2])
    specs = [s.split(":") for s in sys.argv[3].split(",")]
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
    stop_at = int(sys.argv[sys.argv.index("--stop-at") + 1]) if "--stop-at" in sys.argv else 35
    stop_week = (
        int(sys.argv[sys.argv.index("--stop-week") + 1]) if "--stop-week" in sys.argv else 15
    )
    min_diff = (
        int(sys.argv[sys.argv.index("--min-difficulty") + 1])
        if "--min-difficulty" in sys.argv
        else 1
    )
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not r.get("call_failed"):
                done.add((r["model"], r["rep"], r["case"]))
    with (
        httpx.Client(timeout=120, limits=httpx.Limits(max_connections=64)) as client,
        out.open("a", encoding="utf-8") as fh,
    ):
        start = usage(client)
        print("usage at start", json.dumps(start), flush=True)
        counter = {"n": 0}

        def guard() -> None:
            u = usage(client)
            roll = u.get("rolling", {}).get("percent", 0)
            week = u.get("weekly", {}).get("percent", 0)
            bad = any(
                u.get(w, {}).get("status") not in (None, "ok")
                for w in ("rolling", "weekly", "monthly")
            )
            print(f"  [guard] rolling {roll}% weekly {week}%", flush=True)
            if roll >= stop_at or week >= stop_week or bad:
                print(
                    f"  [guard] STOP: rolling {roll}% (>= {stop_at}) / weekly {week}% (>= {stop_week}) / limited={bad}",
                    flush=True,
                )
                _stop.set()

        def task(model: str, effort: str | None, rep: int, case: dict, session: str) -> None:
            if _stop.is_set():
                return
            rec = run_one(client, model, effort, rep, case, session)
            with _lock:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                counter["n"] += 1
                n = counter["n"]
                print(
                    f"{model} r{rep} {case['id']} valid={rec.get('valid')} dir={rec.get('direction')} "
                    f"want={case['expected']['direction']} {rec.get('latency_s')}s",
                    flush=True,
                )
            if n % 20 == 0:
                guard()

        pools = []
        for model, effort, reps in specs:
            eff = None if effort == "none" else effort
            pool = ThreadPoolExecutor(max_workers=workers)
            pools.append(pool)
            for rep in range(int(reps)):
                session = f"orionfx-bench-{model}-r{rep}-{uuid.uuid4().hex[:6]}"
                for case in cases:
                    if case["difficulty"] >= min_diff and (model, rep, case["id"]) not in done:
                        pool.submit(task, model, eff, rep, case, session)
        for pool in pools:
            pool.shutdown(wait=True)
        print("usage at end", json.dumps(usage(client)), flush=True)


if __name__ == "__main__":
    main()
