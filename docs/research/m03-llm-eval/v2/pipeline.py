# ruff: noqa  (research script, kept as run; not product code)
"""OrionFX end-to-end fit: a prototype OpenAI-compatible Provider -> real M03
`assess_news` (production retry + 20 s timeout) -> real M03 `reason` -> real M05
`decide` / `manage` under fixed market scenarios. Measures what the bot would DO,
not only what the LLM said.

Scenarios (truth T = the case's expected news direction; -T its opposite):
  E1 good entry : chart=T,  calibrated forecast=T  -> ideal ENTER T (LLM must not block it)
  E2 bad entry  : chart=-T, calibrated forecast=-T -> ideal WAIT  (news contradicts the trade)
  M1 wrong hold : holding -T, chart stale, no forecast -> ideal EXIT (news says we're wrong)
  M2 right hold : holding T,  chart stale, no forecast -> ideal no EXIT
  N  neutral    : truth NEUTRAL, holding BUY, chart stale, no forecast -> ideal no EXIT
usage: uv run python pipeline.py CASES.json OUT.jsonl MODEL:EFFORT[,...] [--workers 4]
"""

import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))  # repo root (tests.support)
from bench import BASE, KEY, NOW, news_items  # noqa: E402
from tests.support.decision import COSTS, CYCLE, _ctx, _forecast, _proposal  # noqa: E402

from app.core.enums import Direction, Side  # noqa: E402
from app.decision.decide import decide  # noqa: E402
from app.decision.fusion import Evidence  # noqa: E402
from app.decision.manage import Held, manage  # noqa: E402
from app.news.assess import assess_news  # noqa: E402
from app.news.provider import ProviderError, ProviderTimeout  # noqa: E402
from app.news.reasoning import reason  # noqa: E402

OPP = {"UP": "DOWN", "DOWN": "UP"}
_lock = threading.Lock()


class OpenCodeGoProvider:
    """Prototype of the #41 adapter: OpenAI-compatible chat completions on OpenCode Go."""

    provider_id = "opencode-go"

    def __init__(self, client: httpx.Client, model: str, effort: str | None, session: str) -> None:
        self.model, self._client, self._effort, self._session = model, client, effort, session
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, timeout_s: float) -> str:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8000,
        }
        if self._effort:
            body["reasoning_effort"] = self._effort
        t0 = time.monotonic()
        try:
            r = self._client.post(
                f"{BASE}/openai/v1/chat/completions",
                json=body,
                timeout=timeout_s,
                headers={"Authorization": f"Bearer {KEY}", "x-opencode-session": self._session},
            )
        except httpx.TimeoutException:
            self.calls.append({"latency_s": round(time.monotonic() - t0, 2), "timeout": True})
            raise ProviderTimeout("timeout") from None
        except httpx.HTTPError as exc:
            self.calls.append(
                {"latency_s": round(time.monotonic() - t0, 2), "error": type(exc).__name__}
            )
            raise ProviderError("transport") from None
        dt = round(time.monotonic() - t0, 2)
        if r.status_code != 200:
            self.calls.append({"latency_s": dt, "status": r.status_code})
            raise ProviderError(f"HTTP {r.status_code}")
        data = r.json()
        oc = (data.get("usage") or {}).get("opencode") or {}
        self.calls.append({"latency_s": dt, "allowance": oc.get("allowance")})
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("bad response shape") from None


def scenarios(truth: str, news: list, reasoning) -> dict:
    ctx0 = _ctx(now=NOW + timedelta(seconds=2))
    ctx = replace(
        ctx0,
        snapshot=ctx0.snapshot.model_copy(
            update={
                "as_of": NOW - timedelta(seconds=30),
                "available_at": NOW - timedelta(seconds=29),
                "valid_until": NOW + timedelta(minutes=5),
            }
        ),
    )
    stale = replace(
        ctx, snapshot=ctx.snapshot.model_copy(update={"valid_until": NOW})
    )  # chart not live
    ev = lambda fc: Evidence(forecast=fc, news=news, reasoning=reasoning)  # noqa: E731
    out: dict = {}
    t = Direction(truth) if truth != "NEUTRAL" else None
    if t is not None:
        o = Direction(OPP[truth])
        for name, chart in (("E1", t), ("E2", o)):
            fc = _forecast(direction=chart).model_copy(
                update={
                    "generated_at": NOW - timedelta(seconds=10),
                    "valid_until": NOW + timedelta(minutes=5),
                }
            )
            prop = _proposal(direction=chart).model_copy(
                update={"valid_until": NOW + timedelta(minutes=5)}
            )
            d = decide(
                ctx,
                [prop],
                ev(fc),
                cycle=CYCLE,
                costs=COSTS,
                symbol="XAUUSD",
                policy_version="bench",
            )
            out[name] = {
                "action": d.created.action.value,
                "dir": d.direction.value if d.direction else None,
                "reasons": d.created.reason_codes,
            }
        for name, held in (("M1", o), ("M2", t)):
            h = Held(
                side=Side.BUY if held is Direction.UP else Side.SELL,
                positions=1,
                cycle_net_usd=Decimal("-1"),
                position_refs=["p1"],
            )
            d = manage(
                stale,
                [],
                ev(None),
                cycle=replace(CYCLE, open_positions=1),
                held=h,
                max_positions=3,
                costs=COSTS,
                symbol="XAUUSD",
                policy_version="bench",
            )
            out[name] = {"action": d.created.action.value, "reasons": d.created.reason_codes}
    else:
        h = Held(side=Side.BUY, positions=1, cycle_net_usd=Decimal("-1"), position_refs=["p1"])
        d = manage(
            stale,
            [],
            ev(None),
            cycle=replace(CYCLE, open_positions=1),
            held=h,
            max_positions=3,
            costs=COSTS,
            symbol="XAUUSD",
            policy_version="bench",
        )
        out["N"] = {"action": d.created.action.value, "reasons": d.created.reason_codes}
    return out


def run_case(client: httpx.Client, model: str, effort: str | None, case: dict) -> dict:
    prov = OpenCodeGoProvider(client, model, effort, f"orionfx-pipe-{model}-{uuid.uuid4().hex[:6]}")
    clock = lambda: NOW  # noqa: E731  (answers become available at NOW; decisions run at NOW+2s)
    a = assess_news(news_items(case), prov, now=NOW, clock=clock)
    rec: dict = {
        "model": model,
        "effort": effort,
        "case": case["id"],
        "category": case["category"],
        "difficulty": case["difficulty"],
        "truth": case["expected"]["direction"],
        "acceptable": case["expected"].get("acceptable"),
        "assess_status": a.health.status.value,
        "assess_detail": a.health.detail,
        "excluded": list(a.excluded),
        "news_dir": a.assessment.direction.value if a.assessment else None,
    }
    news = [a.assessment] if a.assessment else []
    r = reason(news, [], None, prov, now=NOW, clock=clock) if news else None
    rec["reason_status"] = r.health.status.value if r else None
    rec["reason_dir"] = r.reasoning.direction.value if r and r.reasoning else None
    rec["calls"] = prov.calls
    rec["scenarios"] = scenarios(case["expected"]["direction"], news, r.reasoning if r else None)
    return rec


def main() -> None:
    cases = [
        c
        for c in json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        if c["category"] != "reasoning"
    ]
    out = Path(sys.argv[2])
    specs = [s.split(":") for s in sys.argv[3].split(",")]
    workers = int(sys.argv[sys.argv.index("--workers") + 1]) if "--workers" in sys.argv else 4
    with httpx.Client(timeout=60) as client, out.open("a", encoding="utf-8") as fh:
        pools = []
        for model, effort in specs:
            pool = ThreadPoolExecutor(max_workers=workers)
            pools.append(pool)
            for case in cases:

                def task(m=model, e=None if effort == "none" else effort, c=case) -> None:
                    try:
                        rec = run_case(client, m, e, c)
                    except Exception as exc:
                        rec = {
                            "model": m,
                            "case": c["id"],
                            "crash": f"{type(exc).__name__}: {str(exc)[:200]}",
                        }
                    with _lock:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        fh.flush()
                        print(
                            m,
                            c["id"],
                            rec.get("assess_status"),
                            rec.get("news_dir"),
                            "truth",
                            c["expected"]["direction"],
                            {k: v["action"] for k, v in (rec.get("scenarios") or {}).items()},
                            rec.get("crash", ""),
                            flush=True,
                        )

                pool.submit(task)
        for p in pools:
            p.shutdown(wait=True)


if __name__ == "__main__":
    main()
