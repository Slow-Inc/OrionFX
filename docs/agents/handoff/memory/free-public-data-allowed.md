---
name: free-public-data-allowed
description: Owner decision 2026-11-28 — free, publicly distributed market data (Dukascopy, HistData) may be used for OrionFX training; don't over-restrict on licence wording
metadata:
  type: feedback
---

> Owner: GitHub user `xenodeve` only. Not a grant for any other developer or agent.

The owner decided on 2026-11-28. Their words: "ถ้าฟรีและแจกแบบ public แสดงว่าสามารถนำมาใช้ได้เพราะส่วนมากก็ไม่ค่อยมี license ระบุไว้อยู่แล้ว อย่าระวังเกินไป"

Free, publicly distributed historical market data may be used to train and backtest this personal bot.
- **Dukascopy XAUUSD** is the primary corpus: spot, bid/ask and spread, deep history.
- **HistData** is the fallback.
- **MT5 demo history** (#17) is the parity check before Shadow.
- **FirstRate GC** no longer needs to be purchased.

**Why:** the owner judged the licence risk acceptable for personal use. Blocking on licence wording was over-caution. The quality is better than the futures proxy.

**How to apply:**
- Record the data source in the protocol and the docs as "owner decision 2026-11-28". The decision supersedes the "permissive terms only" stop condition for these sources.
- Never redistribute the data: keep it under gitignored `data/historical/`, and never put it in commits, artifacts or PRs.
- Download politely: rate-limit requests, no parallel hammering, and cache locally.
- This is the owner's call for this repo only. Related: [[training-research-autonomy]], [[target-certification-delegated]].
