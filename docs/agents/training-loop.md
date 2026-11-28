# M04 training loop: how it works

Status: **LIVING** (2026-11-28). This is the operating manual for the autonomous training loop in PRD #120, under ADR 0001 and the research grant in `CLAUDE.md` → "Training-loop research autonomy". Each part is marked **[built]** (merged, with tests), **[planned #n]** (an open issue) or **[decision]** (designed here, not yet built). Update the marks when a slice merges. Never mark something built without the PR that built it.

## 1. The loop at a glance

```
 ┌───────────── data ─────────────┐   ┌──────────── research cycle (development data) ───────────┐
 │ historical M1 import [#128]     │   │ hypothesis → config → train per fold → evaluate per fold │
 │ live bars (CandleEngine) [built]│──►│   ↑                                         │            │
 │ MarketRecorder → M07 dataset    │   │   └──── findings (docs/research) ◄──────────┘            │
 │   snapshots/outcomes [built]    │   └──────────────────────────┬───────────────────────────────┘
 └─────────────────────────────────┘                              │ budget spent or a config passes
                                                                  ▼
 ┌──────────── claim (pre-registered protocol version) ───────────────────────────────────────────┐
 │ freeze protocol → power check → system backtest → open holdout ONCE → readiness (per subject)   │
 └──────────────────────────────────────────────┬────────────────────────────────────────────────────┘
                                                │ all tiers pass
                                                ▼
 ┌──────────── deployment (ADR 0001) ───────────────────────────────────────────────────────────────┐
 │ candidate → shadow predictions beside champion → auto-promote to Shadow/Demo → forward monitor  │
 │   ↳ degradation or drift → auto-rollback, and a new protocol version on forward data only       │
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

## 2. Data layer

| Step | Mechanism | Status |
|---|---|---|
| Live bars | `CandleEngine` builds M5/M15/H1 from bid ticks and flags real gaps. With a `SessionSchedule`, scheduled breaks and weekends are not gaps. | [built] #29, #124 |
| Session schedule | `SessionSchedule` uses the New York zone and is DST-aware. `XAUUSD_NY_1700` is [PROPOSED]. `audit(schedule, bars)` checks it against the data. Each data source has its own schedule. | [built] #124. Per-source wiring: [planned #128] |
| Historical bars | Import M1 bars and resample to M5/M15/H1, with spread (if the vendor has it) and a **data-source** tag separate from LIVE/BACKFILL. Validation matches live bars, and chunks are resumable. Sources: `CsvM1Source` (generic format) and `FirstRateGcSource`. | [planned #128] |
| Training source (bulk) | **HistData XAUUSD M1** (spot bid, from 2009), data source `histdata-xauusd`, schedule `XAUUSD_NY_1700`, by owner decision 2026-11-28. The file clock is EST with no DST (UTC−5 all year), converted to UTC, so the summer break (16:00 in the file) lands in the broker schedule's 21:00 UTC slot. No spread: the backtest's stressed spread applies; Dukascopy's median spread per (year, month, UTC hour) is a cost input only (`spread_profile`), never a feature. `HistDataFetcher` (page token, then the form POST; ≥ 3 s between requests; zips capped and read in memory, one expected CSV member) and `scripts/histdata_import.py`. Sealed like `dukascopy-xauusd`. | [planned #128]; fetched by the coordinator on Colab |
| Spread source | **Dukascopy XAUUSD M1** (spot; bid OHLC, spread = ask close − bid close), data source `dukascopy-xauusd`, schedule `XAUUSD_NY_1700`, by **owner decision 2026-11-28** (free, public data may be used; HistData is the fallback). `DukascopyFetcher` fetches one day file at a time, with a pause, backoff and a gitignored cache (`data/historical/dukascopy/`); `scripts/dukascopy_import.py` checks the price scale on a reference day, then fetches and imports. Dukascopy had been rejected on its terms; that reading is kept as history. FirstRate GC (`firstrate-gc`) stays as a reference source. | [planned #128]; the fetch has not run yet |
| Sealed windows | Protocol v1 (#132) length rule, fixed 2026-11-28 (certified by the agent under owner delegation): holdout `[2024-05-28, 2026-11-28)` (30 months), unread slice `[2023-11-28, 2024-05-28)` UTC; development data is 2008-01 → 2023-11. `require_unsealed` makes `export_dataset` and the `MarketRecorder` backfill refuse a real-source window ending after 2023-11-28 unless #132's `open_holdout` token is passed (and even then never the unread slice); the cost log never reads past it. TODO #131: the system backtest entry calls the same guard. | [planned #128] |
| Cost log | `era_costs`: per calendar year, median close spread and median spread/ATR_M15, reported beside `STRESSED_COST`. Information only; never changes the V2 bound. | [planned #128] |
| No-trade minutes | FirstRate omits minutes with no trade. A run of ≤ 15 open-session minutes between two trades is filled flat at the previous close, volume 0; a bar made only of such minutes is flagged `NO_TRADE`, not `GAP`, and is labelled normally. A run never spans a scheduled closure. A longer run is a real gap, and so is a run that holds a row validation dropped. The import report lists every filled run's length. | [planned #128]; limit [PROPOSED] |
| Rolls | Only the **ratio-adjusted** continuous series is read (multiplicative back-adjustment: scale-free features are unchanged). Roll-crossing rows are kept. Roll dates are recorded per dataset version in `candle_rolls` from FirstRate's contract listing. | [planned #128] |
| Features | `build_feature_snapshot`, schema `m02.features.v2`, point-in-time, session-aware. | [built] #31, #124 |
| Per-bar dataset | `MarketRecorder` records one snapshot per closed bar (LIVE or BACKFILL) into `learning_snapshots`, in one transaction per pass. | [built] #108 |
| Outcomes | `BarLog.label_due` measures the move on the finest timeframe with bars closing at both ends. A row that cannot be labelled is marked once, point-in-time (`learning_unlabellable`). | [built] #108, #125 |
| Recorded predictions | Candidate and champion predictions per bar go to `learning_predictions`. They are learning data only and never trade. | [built] #109 |
| Export | `export_dataset` writes sorted, deterministic JSONL plus a hashed manifest. News is joined at `as_of` and leaking rows are purged. | [built] #110. Splitter folds, embargo and `available_at <= split.end`: [planned #128] |

**Rules for the data layer:**
- Data is point-in-time everywhere: a row uses only what was available at its `as_of`.
- Raw vendor data is never committed.
- FirstRate GC rules (planned until the data arrives; pre-declared in `docs/research/2026-11-28-xauusd-data-sources.md` §5):
  - Prices are futures **trades**, not bid, and have no spread: labels use the locked spread buffer (#129).
  - Adjusted prices are not tradable levels. Never compare them with the broker's spot prices; use scale-free features only.
  - Ratio-adjusted history is rescaled at every new roll, so a later download is a **new dataset version**, never an append.
  - Volume is contracts traded, not tick volume: normalise or exclude it per data source (#129).
  - No holiday calendar: CME holidays and early closes are reported as real gaps.
  - Before the first training run, check the import report (no bars in closed time, the unexpected gaps, the filled-run lengths) and the raw minute gaps against the 15-minute limit.
  - Price-unit quantities (the raw outcome `move`, a fixed spread or slippage buffer) are scaled by the cumulative roll factor in older history: express them in ATR or percent terms (#129).
- News features are only `news.assessment` rows recorded live. The LLM never re-interprets past news, because of look-ahead through its training cutoff.

## 3. Labels and features for training

| Item | Definition | Status |
|---|---|---|
| Triple-barrier label | `app/learning/labels.py`, `m07.labels.tb.v2`. One label per bar, the same for both sides. Barriers at the entry bid close ±d on the bid path, d = `TARGET_ATR`·ATR_M15 (the recorded snapshot's ATR at `as_of`). UP: a bar's high reaches the upper barrier first; DOWN: a bar's low reaches the lower one first; FLAT: neither within N expected slots of the session schedule, N = the forecast horizon's span (a scheduled closure is not a gap). No spread, slippage or commission inside the label, so it depends on bid prices only and a source without spreads labels as live bid candles do. A bar that opens beyond a barrier (a gap) crossed it first and is labelled by that barrier. Any other bar touching both barriers is AMBIGUOUS: no class label, left out of the export. The export manifest's `label_stats` records `dual_touch_count`, `dual_touch_rate` and `flat_share` per era (each split, and "all"). Read on the finest stored timeframe that measures it (M5 first) so dual touches stay rare. A missing bar before it resolves gives no label. `K` = `TARGET_ATR` is [PROPOSED]; the constants EV shares live in `app/core/bet.py`. tb.v1 (side-dependent, spread inside) was replaced before anything trained on it. | [done #129] |
| Stationary transforms | `app/prediction/transforms.py`, `m04.transforms.v1`. One `transform` (via `transform_from_store`) called by the dataset export and by the recorder for a model whose card reads that schema. Per timeframe: `(close−sma)/atr`, Bollinger position, `macd/atr`, MACD histogram/atr, `atr/close`, the log return, `spread/atr`, RSI/ADX/±DI scaled to 0..1; `sin`/`cos` of the UTC hour. Session and liquidity features from the gold bars, point in time and session-aware: distance to the previous session's high and low in ATR_M15, the previous session's London/NY volatility ratio (local 08:00–17:00 each), spread over its rolling median. `tick_volume` is excluded. Invariant to a rescaled price; all but `atr/close` and the log return also to a shifted one. | [done #129] |
| Offline/online parity | The recorder and the export share `transform_from_store` and read a version on one `SessionSchedule`: `SourceRegistry.resolve_schedule` gives a version bound to a data source its source's schedule (a different one given is refused). `MarketRecorder`, `export_dataset`, `prediction_labels` and `label_at` take `session` with no default, and the manifest records each schedule as `session:<schedule_id>` (XAUUSD: `XAUUSD_NY_1700`). Each exported barrier label carries `ends_at`, and a row joins a fold window only if every barrier label ends by the window's end, not just `as_of + horizon`. `prediction_labels` scores recorded predictions on the label version the export carries (`barrier_labels`). The raw `move` stays as raw data. | [done #129 for the recorder and the export; the trading-path adapter is #157 item 2] |

**Certified by agent under owner delegation, 2026-11-28 (#129).**
- **Dual touches stay excluded.** AMBIGUOUS rows are left out. Labels resolve on the finest stored timeframe (`FINEST_FIRST`, M5 first), so dual touches should be rare, and the manifest measures the rate.
- **Pre-declared switch.** If the measured `dual_touch_rate` exceeds 1% in any era, the next label version adds AMBIGUOUS as a fourth class. That class is priced worst case for both sides: `EV = size·(d·(p_s − p_o − p_amb) − fills) − commission`. The switch is declared now and not built in tb.v2.
- **FLAT drift = 0 is a certified assumption.** EV prices a FLAT at zero drift, and a FLAT still pays the fills. The label keeps the real drift as data. Estimating E[move | FLAT] is backlog item 9.

**EV prices the label, and both are proxies (#129).** M05's EV for side s uses the forecast's calibrated class probabilities of `LABEL_VERSION` (p_s = up for a long, down for a short): `EV = size·(d·(p_s − p_opposite) − (spread + 2·slippage)) − commission`, d = `TARGET_ATR`·ATR_M15, the spread the decision-time one (the stressed cost in backtests). That is `size·d·(p_s − p_opposite − cost_over_atr)`: every cost once, on every outcome, a FLAT's drift taken as 0. One triple prices both sides. EV at each certain outcome equals what that outcome pays the side after the same costs (`tests/unit/decision/test_ev_label_alignment.py`). `cost_over_atr` is the per-era break-even and `STRESSED_COST` the pre-declared stressed cost. **The system has no per-position SL/TP (RSK-05):** real exits are the Emergency SL, the USD cycle Profit Target and basket trailing. The ±1·ATR label and EV are a proxy for that trade; only the system backtest (#131), which replays the real exits, can judge whether a model is usable.

## 4. Training

| Item | Mechanism | Status |
|---|---|---|
| Trainer | LightGBM per fold (XGBoost as an alternative) with fixed seeds. It is an offline module; the runtime predictor stays Decimal at its boundary. | [planned #130] |
| Calibration | Isotonic or Platt, on a slice separated in time from the fold's training data. | [planned #130] |
| Metric baseline | Multiclass Brier against class priors from **that training fold only**. | [planned #130] |
| Artifact | Native model file (never pickle). The manifest records the commit SHA, dataset version, label and transform versions, seeds and library versions. | [planned #130] |
| Card | `ModelCard` gains a training cutoff, dataset ref, label version and transform version. `predict` refuses any snapshot with `as_of <= cutoff`, so a model is never scored in-sample. | [planned #130] |
| Trusted adapter | A reviewed module in an allowlisted package loads the weights only after checking them against the sha256 pinned in the registered card. One adapter serves many versions. | **[decision]** in the tracer PR #127: decide whether `build` receives the card and the verified weight bytes. Generating a module per version would break "allowlisted packages hold reviewed code only". |
| Evaluation load | `load_model` is point-in-time since #126, so a model registered today is refused for every historical `now`. An explicit **evaluation load** is hash-verified, needs no eligibility, and is marked so that no live path accepts it. | **[decision]** #127 |
| Where it runs | Colab CPU high-mem. Probed at 8 vCPU, 50 GB RAM, 226 GB disk and 0.26 CU/h. The code arrives as a `git archive` of a pinned commit. The bundle is scanned for the secret shapes and known local secret values listed in §4a. The artifact, the manifest and the run log come back. Only the artifact and the manifest are hash-checked, verified and kept, and nothing is registered yet. Every VM is stopped after use. Runbook: §4a. | [built] #134: runner, bundle scan, verified import of the files. The trainer is a stub until #130. **Not built:** registration, which comes through the #130 adapter path; resumable stages; local recompute of validation metrics (#130, #132). |

## 4a. Runbook: a training job on Colab [built #134]

**Run it.** The job spec is a `JobSpec` JSON (`app/learning/colab_job.py`). It holds:
- the protocol hash and the config;
- the local export path and its `dataset_hash`;
- the 40-hex code SHA;
- the seeds and the entrypoint;
- the model id and version;
- `timeout_s`.

`bundle` adds `code_hash`, the sha256 of the bundled `code.tar`.

```
uv run python -m app.learning.colab_runner bundle spec.json data/bundles/<job> \
    [--repo .] [--exports data/exports]                                # scanned bundle
uv run python -m app.learning.colab_runner run data/bundles/<job>/job.json --db <scratch.db> \
    [--repo .] [--artifacts models] [--work data/colab_runs] [--min-cu 20]
```

- `bundle` writes three files and nothing else:
  - the export (`dataset.jsonl`). It must resolve inside `--exports` (a relative `dataset_path` is relative to `--exports`) and must not have a secret-shaped name. It must be UTF-8 JSONL, with one JSON object on every line;
  - `job.json`;
  - `code.tar`, a `git archive` of the code SHA limited to `backend/app`, `pyproject.toml` and `uv.lock`. The SHA must be committed.
- **Known local secrets.** Both commands load the secret-named environment variables, plus **every value** in `<repo>/.env`. Account data such as a login or a server name is not secret-named, so all `.env` values count. `.env` is parsed the way python-dotenv parses it: inline comments are stripped, and its escapes are decoded inside quotes. When the decoded value differs from the raw text inside the quotes, both are searched; the raw one is named `KEY (raw)`. Any value of 4 or more characters, raw or JSON-escaped, found anywhere in the bundle is a refusal. The refusal names the variable, never the value.
  - Expect false refusals when a `.env` holds a short or common value (for example `true`, a port or a host) that also occurs in the code or the data. Lengthen or rename the value; do not weaken the scan.
- `run` steps:
  1. Re-scans the bundle with the known local secrets, then uploads exactly the scanned bytes.
  2. Checks `code_hash`, and refuses a `code.tar` that is not byte-identical to a fresh `git archive` of the job's code SHA, taken from `--repo`. This proves that the **uploaded** archive is the pinned commit. It does not prove that the VM ran it: the returned manifest is written by the untrusted VM, so the artifact's provenance is **asserted by the VM, not attested**. A different git version could change the archive bytes; that case is a refusal, never a pass.
  3. Refuses a model version that is already registered, or whose `models/<id>/<version>/` directory already exists.
  4. Refuses when `colab usage` cannot be read, or shows less than `--min-cu`. `--min-cu` cannot be set below 20 or to a non-finite value. **Exit 3 is the CU-floor stop condition: report it to the owner, do not lower the floor on your own.** Any other refusal (a bundle refused at steps 1–3, or a return refused at step 7) exits 2 with `REFUSED: ...`.
  5. Starts a CPU high-mem VM. The driver (`colab_vm.py`) installs the locked deps with a pinned uv and runs the entrypoint.
     - The driver is uploaded from the **local checkout** that runs the runner. It is trusted, reviewed local code, not the pinned `code_sha`. Only `code.tar`, which holds the trainer and its deps, is pinned. Whatever the VM returns, including the manifest, is still verified as untrusted.
  6. Downloads the output tar and the run log, then always runs `colab stop`:
     - A `finally` runs it, and SIGINT, SIGTERM and SIGBREAK lead into that `finally`.
     - The stop runs in its own process group, so a second Ctrl-C does not kill it. It is retried once.
     - A signal that arrives during the stop is held until the stop is done. It is then raised as `RunInterrupted`, so nothing is kept.
     - The stop is attempted even when `colab new` failed, because a late failure can still have assigned a VM. If that stop also fails, the `new` error is raised (the real cause), with the recovery command attached as a note.
     - The `colab` CLI never receives a secret-named environment variable. A `colab usage` call through this filtered environment authenticated on 2026-11-28.
  7. Verifies the return locally, then keeps it (see below).
- **Hard timeout:** `timeout_s` bounds the whole remote run.
- **Verification.** The checks, in order:
  - **The tar.** `out.tar` must be at most 256 MiB, checked after the download. It must be a plain, uncompressed tar holding exactly `manifest.json`, `card.json` and `weights.json`, all regular files.
  - **The hashes.** Every sha256 must match the manifest.
  - **The binding.** The manifest must echo these job fields: job id, protocol hash, code SHA, `code_hash`, dataset hash, seeds, config, entrypoint, and model id and version.
    - A bundle without `code_hash` is refused by the scan, before any VM starts. Re-bundle older jobs.
    - A job whose `code_sha` pins a commit from before this binding was extended is refused only after the VM has run. The VM-side binding comes from the pinned commit's `colab_vm.py`, so the mismatch shows up only in the returned manifest. Pin a commit that has this change.
  - **The card.** It must be a valid `ModelCard` for this job and must name the weights' hash. It may not claim anything the VM cannot prove: `supported_modes` must be exactly SHADOW (DEMO and LIVE are refused, and so is an omitted field, because the default is SHADOW+DEMO); `calibrated` must be false; there must be no `evidence_refs` and no `validation` metrics. Validation is recomputed locally, by trusted code (#159, with #130 and #132).
  - **The weights.** They must be JSON (a native model file is not accepted yet), and are never unpickled.
- **What happens on success:**
  - The verified files are copied to `models/<id>/<version>/`.
  - A local `run.json` record is written beside them. It holds the job, the file hashes, the session, the forced `supported_modes`, `registered: false`, and the time.
  - **Nothing is registered.** The registry's `artifact_hash` must be what the trusted adapter loads (#130), so registration belongs to #130's `lightgbm_native` card and weights path. `RunResult` and `run.json` are its inputs (tracked in #159). The copied `card.json` is the VM's bytes, hash-pinned by the manifest, and passed the checks above (SHADOW only, no claims).
  - Any failure during the copy removes the directory.
  - `--db` is required. It is read only to refuse a version that is already registered; point smoke runs at a scratch DB.
- **Recovery after a hard kill** (which cannot be caught): run `colab sessions`, then `colab --config <work>/colab-state.json stop -s <session>`. The runner records its sessions in that state file, not in the default one. A `VmStopFailed` error means the VM may still be billing.
- **Smoke evidence (2026-11-28, run by the agent, stub trainer, scratch DB).**
  - **Normal run.** Commit `88d8e4d` ran with `timeout_s` 900.
    - The VM showed 8 CPUs, `MemTotal` 53467180 kB and Python 3.13.18. Locked deps were installed with uv 0.11.16.
    - The return was verified and registered. That is the behaviour before this change.
    - Wall time was 38.9 s inside the runner and 44 s end to end. An earlier run took 53 s.
    - The balance read 136.64 CU before and after. `colab usage` shows two decimals, so the spend was below 0.01 CU.
    - Afterwards, `colab sessions` reported "No active sessions found on server", with `Active assignments: 0`.
  - **CU floor.** `--min-cu 1000` was refused with exit 3, and no VM started.
  - **Hard timeout.** A real run at `24eea01`, with `timeout_s` 25, killed `colab exec` at the deadline. It exited non-zero after 32 s in total, registered nothing and left no session.
  - **What this proves.** The plumbing only; it is not a model result. Every change after `0ce391c` is unit-tested only and has not run a VM on Colab: the review fixes, the hardened scan, verification without registration, and the stop handling.

**What the scan refuses** (`scan_files`, tested on a bundle built from HEAD). It proves only what this list covers. It cannot find a secret whose shape and value it does not know.
- `.env` files, and a dataset export with a secret-shaped name;
- keys and certificates (`*.pem`, `*.key`, `id_*`), and anything named like a credential;
- SQLite stores (by name and by magic bytes), and a dataset that is not UTF-8 JSONL objects, which covers compressed files;
- key-like strings (private keys, AWS, GitHub, OpenAI/Anthropic-style, Google, Slack and Telegram tokens);
- the value of any secret-named local environment variable and of every `<repo>/.env` entry, raw or JSON-escaped;
- config keys that look like secrets;
- tests, frontend, docs, data, `.claude`, and any path outside the code allow-list;
- links and special files, and member paths that are absolute, contain `..` or `\`, or are not normalised.

The VM is given no broker, LLM or GitHub credentials. `--env` is never used, so no local environment is forwarded to the VM.

## 5. Evaluation tiers (always reported separately)

1. **Model metrics per fold:**
   - multiclass Brier skill;
   - ECE;
   - direction accuracy with a block-bootstrap 95% CI over trading days;
   - precision at high confidence, with coverage;
   - per-regime buckets, each with `n`; thin buckets get no verdict. [planned #130/#132]
2. **System backtest.** `DecisionLoop`, M06 and `FakeBroker` run over stored bars on a fresh DB per run. The clock follows bar availability. The model's forecast reaches M05 through a **model-backed evidence provider**; the snapshot is passed in, not rebuilt (**[decision]** #127). Reported:
   - expectancy after costs, PF, max DD and trade count;
   - open positions at fold end and refused trades, reported separately.

   Two costs are measured, not assumed: M06's deal scan and the 30 s staleness rule after a DENY (#121). [planned #131]
3. **Shadow/Demo on the real market.** These are the same metrics on forward data. The only renewable evidence. [blocked #17]

The synthetic scenarios (`validation/scenarios.py`) are behaviour tests. They never count as edge.

## 6. The claim protocol (anti-overfitting)

A **protocol version** is a hashed record saved before the first run of a claim [built #132 machinery: `validation/protocol.py`, a full sha256 of canonical JSON; the certified values themselves live in the protocol document, not in code]. It holds:
- the targets [PROPOSED; certified by the owner];
- the configuration space and the iteration budget (20);
- the regime rule;
- the fold plan;
- the debug window;
- the **unread slice**;
- the holdout window.

Rules, adopted from mip-research (PRD #120):
1. **A window that has been read is development data.** Walk-forward scores used to choose a config are development scores. The holdout opens once, by a recorded step, and is development data afterwards.
2. **Count independent units.** CIs use block bootstrap over trading days, and every count states how many days and episodes stand behind it.
3. **Check power before spending a window.** Do not open the holdout, or start Shadow/Demo, if the pass rule cannot pass at that size.
4. **Correct for selection.** Compare against a baseline rerun on the same folds and seeds, and report best-of-N together with N.
5. **Compare in pairs against a noise floor.** An A/A run measures seed variance; a difference below it is "unconfirmed".
6. **Recompute independently.** A second implementation recomputes the metrics from the logs, and a train/serve skew check compares feature vectors.
7. **Retention.** A new version must not do worse than the previous one on a frozen benchmark period.
8. **Keep errata.** Evidence can be marked withdrawn, corrected, unconfirmed or superseded.
9. **Say who verified.** Every report states what was verified and by whom.
10. **Stop rule.** If the budget runs out before the targets are met, stop and report. Never lower the targets.

**What the #132 machinery builds** (built #132 machinery, not yet wired into a trainer or the system backtest):

| Rule | Mechanism | Status |
|---|---|---|
| Protocol record | `protocol.Protocol`, `protocol_hash`, `load_protocol(text, expected_hash)` refuses a document whose content does not hash to the expected value. | [built #132] |
| 1, 10 | `runlog.RunLog` (migration `0016_claim_protocol`). A run counts only under a registered protocol hash. N is the number of distinct config hashes with an `ok` or `failed` run. A new counted config past the budget is refused. `aa` and `bugfix-rerun` (with a bug note and fix SHA) are logged but never counted, and only for a config already counted. A config has one current result: a `bugfix-rerun` replaces it only when that result failed or is marked INVALID or CORRECTED, and never after the holdout is consumed, so reruns cannot be shopped for the best one. `open_holdout` records the single opening, for a counted config whose current result (its latest `ok` or `bugfix-rerun` run) carries no erratum; after it, no new config counts. Windows stay honest across versions: `register` refuses a new version whose holdout or unread slice overlaps a window another version has read (its debug window, or its holdout once opened), whose debug window overlaps a window another version seals (its unread slice, or its holdout while unopened), or whose unread slice and holdout cross another version's holdout and unread slice; `open_holdout` refuses a holdout another version already opened. An `aa` run must use a seed set its config has not run. | [built #132] |
| 2 | `claimstats.mean_ci`, `brier_skill_ci`: stationary or circular block bootstrap over days, one- or two-sided. | [built #132] |
| 3 | `claimstats.simulated_power`, `mean_lb_power`, `min_track_record_length`; `ece_sweep` with `ece_noise_floor_consistency` for the ECE gate (`ece_noise_floor`, `expected_binned_ece` for the binned estimator). | [built #132] |
| 4 | `claimstats.best_of_n` (the caller passes N as `RunLog.counted_configs`; the function checks only that N is not below the configs shown, and the wiring that passes it is planned), `deflated_sharpe`, `pbo_cscv`, `sign_test`, `holm`. | [built #132] |
| 5 | `claimstats.aa_noise_floor`, `compare_to_floor`. The caller states each metric's direction (`higher_is_better`), for `best_of_n` too, because the gates mix Brier skill with ECE and log loss. | [built #132] |
| 6 | `recompute.recheck`: a second implementation recomputes `brier_multiclass`, `top_label_accuracy`, `log_loss` and `brier_skill_multiclass` from per-prediction outputs and flags each mismatch. A metric logged under another name (the card's binary `brier`, `direction_accuracy`) is reported as unchecked, never compared. The train/serve skew check is not built. | recompute [built #132]; skew [planned] |
| 7 | The retention benchmark against the previous version is not built. Runs are never deleted (database triggers). | [planned] |
| 8 | `RunLog.add_erratum`: INVALID, WITHDRAWN, CORRECTED, UNCONFIRMED or SUPERSEDED, with a reason and a time. The run stays, and its budget is never refunded. | [built #132] |

The ECE estimators are stated in `claimstats`:
- `ece_sweep` is class-wise ECE_sweep (Roelofs et al. 2022), **L1**: one-vs-rest per class on equal-mass bins, using the largest bin count that keeps the observed frequency monotone, averaged over the classes.
- `ece_noise_floor_consistency` gives its noise floor by consistency resampling: labels are redrawn from the model's own probabilities, and the result is a set of quantiles (for example q50).
- `ece_binned` is the plug-in L1 at a fixed bin count.
- `ece_debiased` is Kumar, Liang & Ma 2019, the debiased **L2** calibration error. It is a secondary diagnostic only, because L2 is not on the L1 scale.

Which estimator a gate uses, and on which sample, is a protocol value.

**Readiness** is judged per subject: `EvidenceStore` qualifies only subjects common to every run [built #126]. `DEFAULT_POLICY` is uncertified, so the verdict is NOT_READY until the owner certifies thresholds.

## 7. Deployment and the closed loop (ADR 0001)

| Step | Mechanism | Status |
|---|---|---|
| Candidate | A model that passes every tier under a certified policy is proposed through `ModelLifecycle.propose_candidate`. | [built] #61 (lifecycle); wiring: [planned #133] |
| Shadow scoring | The candidate's recorded predictions run beside the champion's on live bars. A champion-vs-candidate report compares them on forward data. | [planned #133] |
| Promotion | A **policy-bound system actor** promotes to Shadow/Demo only under a certified policy version with the full evidence set. LIVE stays owner-only. | [planned #133] |
| Rollback | Automatic when forward metrics breach the policy floors: back to the previous champion, or to no model (M05 waits). It never rolls forward to an unqualified model. | [planned #133] |
| Drift | `psi` per feature above RETRAIN_PSI [PROPOSED] recommends a retrain. That opens a **new protocol version** evaluated on forward data only. | psi [built] #61; trigger [planned #133] |
| Independent safety | M06 checks registry eligibility itself, so whoever promoted a model or strategy, a missing, expired or revoked approval is refused. | [planned #121] (BLK-04) |

## 8. Research cycle (what the agent does on its own)

1. **Pick a question from the data.** For example: which features drift, which regime buckets are weak, whether a label horizon is wrong, or whether calibration is off.
2. **Write a short hypothesis** in `docs/research/` before running anything.
3. **Explore on development data only**, inside the declared budget.
4. **If it helps, ship it as a normal unit of work:** issue → TDD → T4 gates → PR. Generated code is untrusted until reviewed. It runs with no credentials.
5. **Turn a result into a claim** only through a new protocol version. The owner certifies any change to targets or thresholds; the agent may propose them, with evidence.
6. **Record the outcome either way.** Failures are evidence too, and wrong numbers go to errata.

## 8a. Research backlog (ordered; each item is an arm, never a shortcut)

Source: `docs/research/2026-11-28-m04-quant-upgrade-proposals.md`.

**Governing rule.** Every item below is an **arm** ablated against the V1 baseline on the same folds and seeds, under one pre-registered protocol version.
- **Shared budget.** All arms share the protocol's single iteration budget. Horizon, time decay, feature set and label type each count as configs.
- **One winner.** Only one winner per protocol version reaches the single-use holdout.
- **Baseline first.** Nothing here runs before the baseline exists (#127 → #130 → #132).

| Pri | Item | Where | Status |
|---|---|---|---|
| 0 | **EV and the label price the same event.** EV assumes a symmetric ±`TARGET_ATR`·ATR_M15 bet (`fusion.expected_value`). Triple-barrier barriers, the treatment of FLAT, the horizon and any meta-label P(win) must match it, or EV must change with them. | #129 | [done: tb.v2, literal EV, policy-0.5] |
| 1 | **Session and liquidity features** from the gold bars: distance to the previous session's high and low, the London/NY volatility ratio, spread anomalies. | #129 | [done] |
| 2 | **Uniqueness weights and time decay.** Pre-register whether claim metrics are weighted. Time decay is a hyperparameter inside the budget. | #130 | [planned] |
| 3 | **Horizon M15–H1 as configs.** Watch label overlap. | #130/#132 | [planned] |
| 4 | **Volatility-model WAIT gate.** Block-only, following the `NEWS_VETO` pattern. New evidence contract. Baseline is persistence plus the M02 regime rule. | #139 | [planned] |
| 5 | **Meta-labeling arm.** Predictor-port contract change. Needs an evaluation path for strategy eligibility over history (the same seam as #127). The dataset pins the strategy rule hash. | #140 | [planned] |
| 6 | **Cross-market data-only symbols** (XAGUSD and a USD proxy). Schema bump, never tradable, one session schedule per symbol. Admitted only with a live source. Measure the skew between the training feed and the serving feed. | #141 | [planned] |
| 7 | Yields, VIX and the pre-event countdown. Need point-in-time sources. | after #41 / OD-14 | [blocked] |
| 8 | Chart+News fusion and regime-specific ensembles, from Shadow data. | after #17, #41 | [blocked] |
| 9 | **Estimate E[move \| FLAT].** EV prices a FLAT at zero drift (certified assumption, #129). Measure the FLAT drift per era from the exported `barrier_labels` and, if it is material, propose a label/EV version that prices it. | after #130 | [planned] |
| — | **Model input to M06 trailing, TP or SL.** An owner decision, never a backlog item ("risk is independent of AI"). | owner | [not planned] |
| — | Deep learning or transformers on raw OHLCV. Deferred. | — | [not planned] |

## 9. Who decides what

| Decision | Owner | Agent |
|---|---|---|
| Targets, readiness thresholds, holdout window, policy certification | ✅ | proposes |
| Features, labels, model family, hyperparameters, pipeline code | reviews by PR | ✅ within the protocol |
| Promotion and rollback in Shadow/Demo | certifies the policy once | ✅ automatic (ADR 0001) |
| Real-money Live | ✅ only | never |
| External resources: MT5, Demo account, news licence | ✅ | — |

## 10. Open design decisions (resolve in #127)

- **Evaluation load path** for historical scoring: no eligibility needed, and never accepted by a live path.
- **Adapter and weights:** how `build` gets the card and the verified weight bytes, with one reviewed adapter for many versions.
- **Evidence provider input:** it receives the snapshot instead of rebuilding it.
- **Tracer fixture:** a daily break, a weekend, M5 recording and M15/H1 horizons. It asserts usable features, label coverage and at least one ENTER in the test fold. A negative control with `session=None` must fail. Readiness must be NOT_READY under the default policy.
