"""Stub training entrypoint for the Colab runner (#134). Trains nothing.

`python -m app.learning.colab_stub <job.json> <out_dir>` checks the dataset hash, writes
dummy weights (native JSON, never pickle) and a `ModelCard`-shaped card that says it is
a stub, then the bound manifest. The real trainer (#130) replaces it and keeps the same
return contract (`colab_vm.write_return`). Stdlib only.
"""

import json
import sys
from pathlib import Path
from typing import Any

from app.learning.colab_vm import CARD, WEIGHTS, sha256, write_return


def _json(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")


def train(job_path: Path, out: Path) -> None:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    data = (job_path.parent / job["dataset_path"]).read_bytes()
    if sha256(data) != job["dataset_hash"]:
        raise SystemExit("dataset does not match the job's dataset_hash")
    weights = _json(
        {"kind": "stub", "rows": data.count(b"\n"), "seeds": job["seeds"], "config": job["config"]}
    )
    card = _json(
        {
            "model_id": job["model_id"],
            "model_version": job["model_version"],
            "artifact_hash": sha256(weights),
            "lineage": "CHART_ONLY",
            "feature_schema": "stub",
            "required_features": ["stub"],
            "feature_ranges": {},
            "horizon": "M15",
            "horizon_bars": 1,
            "semantics": "stub output of the Colab runner (#134); not a model, never trades",
            "calibrated": False,
            "validation": {},
            "evidence_refs": [],
            "supported_modes": ["SHADOW"],
        }
    )
    write_return(job, out, {CARD: card, WEIGHTS: weights})


if __name__ == "__main__":
    train(Path(sys.argv[1]), Path(sys.argv[2]))
