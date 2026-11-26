"""Regenerate docs/contracts/*.schema.json from backend/app/core (run after a contract change)."""

from pathlib import Path

from app.core.schemas import generate_schemas

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    target = ROOT / "docs" / "contracts"
    target.mkdir(parents=True, exist_ok=True)
    generated = generate_schemas()
    for stale in target.glob("*.schema.json"):
        if stale.name not in generated:
            stale.unlink()
    for name, text in generated.items():
        (target / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {len(generated)} schemas to {target}")


if __name__ == "__main__":
    main()
