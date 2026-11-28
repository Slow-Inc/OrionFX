"""Protocol v1 §6 pre-run viability on imported bars (#132; data from #128).

    uv run python scripts/viability.py --db hd.db --version hd-xau-v1 \\
        [--era-start 2022-01-01] [--era-end 2023-11-28] [--horizon H1] \\
        [--k 1.0,1.25,1.5,2.0] [--out viability]

Reads only the given SQLite file (bars of one dataset version), never past the seal
(`require_unsealed`: the era must end by 2023-11-28). Writes `<out>.json` and `<out>.md`,
prints the markdown table, and states the pre-declared pick or "non-viable".
"""

import argparse
import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.core.enums import Timeframe
from app.learning.bars import Horizon
from app.storage.db import connect
from app.validation.viability import run_viability, to_markdown


def _utc(text: str) -> datetime:
    d = date.fromisoformat(text)
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


def main() -> None:
    p = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--era-start", type=_utc, default=_utc("2022-01-01"))
    p.add_argument("--era-end", type=_utc, default=_utc("2023-11-28"))
    p.add_argument("--horizon", type=Timeframe, default=Timeframe.H1)
    p.add_argument("--k", default="1.0,1.25,1.5,2.0")
    p.add_argument("--out", type=Path, default=Path("viability"))
    args = p.parse_args()
    if not args.db.exists():
        raise SystemExit(f"{args.db} does not exist")
    conn = connect(args.db)
    t0 = time.perf_counter()
    result = run_viability(conn, dataset_version=args.version, era_start=args.era_start,
                           era_end=args.era_end, horizon=Horizon(args.horizon, 1),
                           ks=[Decimal(k) for k in args.k.split(",")])  # fmt: skip
    data = result.to_json()
    data["seconds"] = round(time.perf_counter() - t0, 1)
    json_path, md_path = args.out.with_suffix(".json"), args.out.with_suffix(".md")
    json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    md = to_markdown(result)
    md_path.write_text(md, encoding="utf-8")
    print(md)
    print(f"{data['seconds']} s; wrote {json_path} and {md_path}")


if __name__ == "__main__":
    main()
