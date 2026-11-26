# OrionFX

Personal XAUUSD AI auto-trading bot on MT5. **Demo/Shadow only** — Live stays disabled until the qualification gates in `docs/plans/master_planV1.md` §11 pass and the owner approves explicitly.

## Layout

- `backend/app/` — Python package (`app`). `core/` holds the shared contracts (PRD-X01); each module owns its subpackage (master plan §7.1).
- `tests/` — `unit/`, `contract/`, `integration/`, … (pytest).
- `docs/` — master plan, PRDs (`docs/prd/`), generated JSON Schemas (`docs/contracts/`), ADRs, records.

## Setup (Windows, local)

```sh
uv sync                       # Python 3.13 + pinned deps from uv.lock
uv run pytest                 # tests
uv run ruff check . && uv run ruff format --check . && uv run mypy   # lint + types
git config core.hooksPath .githooks                                  # T4 git guards
```

After changing a contract in `backend/app/core/`, regenerate the schemas:

```sh
uv run python scripts/gen_schemas.py
```

## Safety

No real money, credentials or real account data in this repo, its tests or its logs. The LLM never sends orders; only the Risk & Execution Engine does, through the MQL5 Execution EA.
