# XAUUSD intraday history: data sources and their terms

Status: **SNAPSHOT** (2026-11-28). Research for #128 / PRD #120. It is not legal advice. **The owner decides** which source to buy or use.

Nothing was bought, no account was created, no terms were accepted, and no data or sample file was downloaded. One exception, made during the first research pass: EODHD's public `api_token=demo` key was used for two probe requests (XAUUSD.FOREX and EURUSD.FOREX). The raw term texts read for this note were kept outside the repo (agent scratchpad), not committed.

**Need:** XAUUSD M1 (or M5) bars from 2016 to the present, ideally with bid or spread, for personal, non-commercial ML training. They are stored in a local SQLite store, processed on Google Colab (#134), and never redistributed.

## Owner decision 2026-11-28 (supersedes the decision below)

The owner: "ถ้าฟรีและแจกแบบ public แสดงว่าสามารถนำมาใช้ได้... อย่าระวังเกินไป" (if it is free and publicly distributed, it may be used; do not be over-cautious).

- **Update, same day (owner decision 2026-11-28; downloads authorised by the owner: "อนุญาตให้ download ทุกอย่างที่จำเป็นในการ train"):** Dukascopy throttles hard (15–27 s per request and 503s under mild parallelism on Colab, so more than 50 h for the full range). **HistData is the bulk M1 corpus** (spot bid M1 from 2009; `app/market_data/histdata.py`, data source `histdata-xauusd`, New York local time **with** DST converted to UTC, no spread). **Dukascopy stays the measured-spread source** for the recent era (`dukascopy-xauusd`). The combined-source rule (`cost_log.spread_profile`): Dukascopy's median spread per (year, month, UTC hour) is a cost input only for HistData bars, never a feature. The HistData permission draft is no longer needed.
- First pick that morning: Dukascopy XAUUSD as the primary corpus (spot, M1 bid and ask candles from the public datafeed; `app/market_data/dukascopy.py`). No purchase is needed.
- FirstRate GC is no longer needed; its importer stays as a reference source. The ledger's purchase row is withdrawn.
- The terms reading below and in `2026-11-28-dukascopy-terms.md` is kept as history: it records what the terms say, and the owner decided with it in view.

## Decision (before the owner decision; history)

**FirstRate Data gold futures (GC) 1-minute bars are the planned training corpus, pending the owner's purchase** ($213.95 one-time, read from the page HTML; check it at checkout). It has the clearest licence found: perpetual, import into databases, internal models, and hosting by a service provider "regardless of location" (Colab) are all granted in writing.

Caveats that the importer and the training loop must carry (see "FirstRate GC: verified format facts" below and `docs/agents/training-loop.md` §2):
- It is a **futures proxy** for spot XAUUSD: exchange trade prices, not bid quotes; no spread; CME Globex hours; contract rolls; a basis against the broker's spot feed.
- Features must be scale-free (returns, ATR-normalised values). The ratio-adjusted continuous series is the one imported.
- Expect a distribution shift between this training feed and the broker's spot bid feed used at inference.

Owner action (also in `docs/OPEN-WORK-LEDGER.md`): buy FirstRate GC 1m and place the files in `data/historical/` (gitignored).

## Verdict rule (applied strictly)

- **Permissive**: the written terms affirmatively grant private storage and analysis, do not ban ML or model use, and do not require deletion when the account ends.
- **Restricted**: the written terms forbid something the owner needs, such as ML training, keeping a persistent local store after the account ends, or the source itself.
- **Unclear**: there are no terms, the terms could not be retrieved, they are silent on a right the owner needs, or the data's origin is unknown.

Colab note: training on Colab puts the data on a third-party host. Only FirstRate grants this expressly. Every other source is silent or ambiguous on it, so Colab hosting is treated as unclear for them.

## 1. Ranking

| # | Source | Terms verdict | Cost | Coverage | Quality | Steps to obtain |
|---|---|---|---|---|---|---|
| 1 | **FirstRate Data: gold futures (GC), a proxy for spot** | **Permissive.** "Perpetual license – once downloaded, the data is yours to use forever" (this line is in the licence's Key Points summary, which says it is "for convenience only"; the operative agreement has no term or expiry clause either way); "(iii) import any Data into its various databases"; "(v) create derivative works, including … incorporating the Data into its internal models"; cloud hosting allowed ("(ii) … host Data on computers … at a service provider facility, regardless of location") | **$213.95** one-time (price read from the page HTML). Updates cost $99.95/yr after one free month. Refunds: "Refunds will not be provided" | Continuous series from 31-Jan-2008 to the present (26-Nov-2026 on the page). Individual contracts from GCZ08. Bars at 1m, 5m, 30m, 1h and 1d | Exchange-sourced **trades**, not bid quotes, and **no spread**. Unadjusted, absolute-adjusted and ratio-adjusted continuous series. Timestamps US Eastern (verified later from the FAQ; see below). **Futures, not spot**: basis/contango offset, roll dates and CME session hours, so it differs from the broker's XAUUSD feed | Owner buys the GC dataset on firstratedata.com |
| 2 | **EODHD: XAUUSD.FOREX intraday** | **Permissive** for personal use: "Non-Professional Users are permitted to store, manipulate, and analyze the data for private, non-commercial purposes." Prohibited: "Selling, reselling, retransmitting, redistributing, displaying, or granting access…" No delete-on-cancel clause found. ML is not named (it falls under "analyze"). Colab is not addressed | **$29.99/mo** for "EOD + Intraday All-World Extended". The free plan has no intraday data. "All subscription payments … are final and non-refundable" | Docs say "1-minute data available since 2009" (forex in general), with a 120-day window per 1m request. **XAUUSD 1m depth is unverified**: the demo key returned `Forbidden` for XAUUSD | UTC timestamps. Prices are **aggregated and indicative** ("Forex are not provided by exchanges"; VWAP across 100+ sources). **No bid/ask or spread.** A demo probe of EURUSD 1m in Jan 2017 returned sparse bars (00:07, 00:10, volume 1–2): a density warning | Owner first asks support to confirm the XAUUSD.FOREX 1m start date and bar density, then subscribes for 1–2 months and pulls through the API |
| 3 | **HistData.com** | **Unclear.** No terms-of-use or licence page exists (the site has only cookie and privacy pages). The FAQ says only "Use the data at your own will and risk" (summary), a warranty disclaimer, not a grant. About page: "These data is then shared for free to help you out testing your own/bought strategies and expert advisors." (summary) FTP page: "we're not selling the data" (summary). The origin of the data is not stated | Free through the website forms. FTP/SFTP costs $27 for 15 days (one IP, 3 sessions) | XAUUSD M1 from **2009-03** to the present (monthly files for the current year) | **Bid only** for M1; ask only in the tick Generic ASCII files. "EST … WITHOUT Day Light Savings adjustments" (fixed UTC-5). **Contradicted by the data:** a Colab cross-check against Dukascopy (2026-11-28) matched HistData closes at +4 h on 2021-07-14 (median |diff| 0.0 over 1,140 minutes; +5 h gave 1.24) and at +5 h on 2021-01-13 (+4 h gave 1.49): the clock is America/New_York with DST. Data source unknown | Best data fit, but no terms. Before relying on it, the owner sends a **written-permission request** (draft: `2026-11-28-histdata-permission-request-draft.md`, not sent) |
| 4 | OANDA v20 API (practice account) | **Unclear, leaning restricted.** "Internal Use" covers "research and analysis … and for other data processing use". But "(a) make no more copies of the Licensed Materials than are necessary for your installation…" and §13(a) "you will not retransmit the same from your premises for any purpose" conflict with a persistent store and with Colab. The governing entity depends on residence (OANDA Corporation US and OANDA Asia Pacific checked; the Global Markets licence was not found) | Practice account free | OANDA states its historical pricing goes back to 2005. Candles at M1 and above, with bid/ask/mid. XAU_USD availability and M1 depth per entity unverified | Bid/ask/mid candles and UTC timestamps are general knowledge of v20 (the docs page returned 404; not verified) | Owner opens a practice account (if offered in their country), gets an API token, then reads that entity's API licence |
| 5 | Stooq (bulk `db/h`) | **Unclear.** Bulk page: "This data is intended solely for personal use. Any commercial use is prohibited." Terms §5.3: "Redistribution of data found on the website is not allowed without the consent of Stooq." Silent on storage and ML | Free | Hourly and 5-minute ASCII bulk ("World" folder). XAUUSD 5-minute **depth unverified**; believed to be a rolling recent window only | Unknown basis and timezone | Manual bulk download behind a JS browser check |
| 6 | MetaQuotes-Demo via the `MetaTrader5` Python package | **Unclear.** The MT5 EULA has no data terms. MetaQuotes "does not provide any … data feed services". Third-party content "is the property of the respective content owners". Licence purpose: "organizing a trader's workstation and trading". The PyPI package is MIT (code only, not data) | Free demo | XAUUSD exists on the server. **M1 depth unconfirmed** (one old forum thread says about 2011) | Bars carry a `spread` field (general knowledge, not fetched). Timestamps are **server time (EET with DST)** per forum threads | **Blocked by #17** |
| 7 | Twelve Data | **Restricted.** "(g) Store or cache Data beyond permitted timeframes specified in the Documentation"; §16.2 "Upon termination or expiration: (a) All Data must be deleted within 30 days"; "(l) Use Free Tier data for commercial purposes" is prohibited | Free tier; paid tiers | 1-minute data only "from February 10, 2020" (support article; may be a general limit) | Unknown | Not recommended |
| 8 | Databento: GC on CME Globex (GLBX.MDP3) | **Restricted.** Use limited "to internal Customer purposes". §9.3: "Upon termination … Customer may no longer download, access, or use any Third-Party Data". CME third-party terms apply (not read) | **$125 new-customer credit** (§3.5), then pay as you go | CME 1m OHLCV (depth not verified) | Exchange trades, futures proxy (same caveats as #1) | The ~$0 route to the same GC proxy, but only while the account stays open. Weaker than FirstRate |
| 9 | Massive (formerly Polygon.io): `C:XAUUSD` | **Restricted.** §5(g): "you may not: … Use Information to train, fine-tune or distill an artificial-intelligence or machine-learning model unless expressly permitted…"; §8: delete "within thirty (30) days, including downloaded files, stored copies and caches" | Basic free (2 years); Starter $49/mo (10+ years) | Gold from 27 Sep 2009, with multi-month gaps (e.g. 2010-07-12 to 2010-09-24, 2020-10-13 to 2020-11-13) | Aggregated from bid/ask quotes | **Excluded: bans ML training** |
| 10 | Alpha Vantage | Personal, non-commercial use only; silent on storage. **Not viable** | FX_INTRADAY is Premium | `GOLD_SILVER_HISTORY` is daily/weekly/monthly only; XAU is not in `physical_currency_list` (157 codes checked) | n/a | n/a |
| 11 | Tiingo | **Unverified** (terms page 404) | Free and paid | Forex intraday "from January 2020" (summary); XAUUSD not confirmed | Top-of-book mid | n/a |
| 12 | Kaggle / Hugging Face XAUUSD sets | **Unclear, with provenance red flags** (§3) | Free | Various | Various | Do not use as a primary source |
| 13 | FXCM `MarketData` GitHub repo | "This is for personal use and abides by our EULA" (summary). No LICENSE file | Free | **No XAUUSD candles** (21 FX pairs, 2017–2020) | UTC | n/a |
| 14 | TrueFX (Integral) | **Unverified.** No terms text or instrument list shown | Free with registration | Tick only; metals not shown | n/a | n/a |
| 15 | Norgate Data | n/a | Paid | **End-of-day only** for futures | n/a | Excluded: no intraday data |
| — | Dukascopy | Restricted (`2026-11-28-dukascopy-terms.md`: bans "construct a database of any kind"; automated retrieval needs consent) | — | — | — | Rejected at the time; **primary corpus by owner decision 2026-11-28** |

Not reviewed in depth: QuantConnect (its OANDA FX dataset has no XAU; downloads cost QCC or $800 bulk), Barchart, TickData LLC and TraderMade.

## 2. Verbatim quotes and URLs

Quotes marked **(summary)** were taken from a WebFetch summary of the page, not checked against the raw page text. Checked against raw page text (2026-11-28): Stooq, Databento, OANDA (OC, OAP), EODHD, the FirstRate licence, Massive §1/§5(g)/§8, Twelve Data, Alpha Vantage, the MT5 EULA and the Kaggle API metadata. Taken from summaries only: HistData (FAQ, About, FTP pages), the Massive individuals ToS, the FirstRate AI-licensing page, Tiingo, TrueFX and the FXCM repo.

### FirstRate Data (GC). <https://firstratedata.com/about/license>, <https://firstratedata.com/i/futures/GC>
- Key points: "Perpetual license – once downloaded, the data is yours to use forever. It may be stored on your own servers and used in perpetuity, there is no expiry." and "Your team and providers – affiliates, consultants and service providers (e.g. cloud hosting) may use the data on your behalf."
- Grant: "(ii) allow a service provider … to access, copy and host Data on computers and other media at a service provider facility, regardless of location; (iii) import any Data into its various databases … (v) create derivative works, including, but not limited to, incorporating the Data into its internal models, published market research, or published academic research papers."
- Restrictions: "(i) not to resell or otherwise redistribute the Data as part of a commercial agreement with other third-parties. (ii) the Data is for the private use of the Subscriber only."
- The licence does not mention pre-existing CME terms. The data is described as "sourced direct from major exchanges".
- FirstRate does **not** sell XAUUSD spot: its FX list (<https://firstratedata.com/it/fx>) has 74 tickers and no XAU.
- The FirstRate AI-licensing page was read only as a summary.

### EODHD. <https://eodhd.com/financial-apis/terms-conditions>, <https://eodhd.com/pricing>, <https://eodhd.com/financial-apis/intraday-historical-data-api>, <https://eodhd.com/financial-summary/XAUUSD.FOREX>
- "Non-Professional Users are permitted to store, manipulate, and analyze the data for private, non-commercial purposes. However, they are prohibited from: Sharing access to their account with others, including within groups. Selling, reselling, retransmitting, redistributing, displaying, or granting access to the Information or Services, whether in its original or repackaged form."
- EODHD "may without notice terminate its contract with the User immediately if the User fails to comply…"; "Customers wishing to cancel subscription-based services can cancel it anytime." No data-deletion obligation.
- "All subscription payments made to EOD Historical Data are final and non-refundable."
- "One API key allows querying 100 000 API requests per day."
- Intraday docs: "1-minute data available since 2009"; 1m requests capped at 120 days; UTC timestamps. XAUUSD page: "All CFDs … and Forex are not provided by exchanges".

### HistData. <https://www.histdata.com/f-a-q/>, <https://www.histdata.com/about-us/>, <https://www.histdata.com/download-by-ftp/>
- No terms page: `/terms-of-use/` returns 404, and the homepage links only `/cookies/` and `/privacy-policy-2/` among legal-type pages.
- FAQ (summary): "Use the data at your own will and risk"; "Eastern Standard Time (EST) time-zone WITHOUT Day Light Savings adjustments"; M1 bars "are based on the tick Bid price"; "The Ask price is only included in the Tick data of the Generic ASCII format."
- FTP (summary): "$27 USD"; "We only allow connections from one IP at the time"; access lasts "for 15 days".
- Strict reading: with no licence, default copyright and database rights apply, so the verdict is **unclear**. A written reply would settle it.

### OANDA. <https://legal.oanda.com/oc/api_license_agreement_oc/en>, <https://legal.oanda.com/oap/api_license_agreement_oap/en>
- OC §3.1 purposes: "(b) accessing the OANDA Trading System Rate Feed in order to retrieve OANDA Trading System Rates; … (d) creating automated trading systems…"
- "Internal Use" shall mean access to and use of the Licensed Materials for performance of research and analysis … and for other data processing use, analysis … but not for redistribution.
- §3.2: "(a) make no more copies of the Licensed Materials than are necessary for your installation of the Licensed Materials and to create backup copies for archival or emergency restart purposes"; "(i) not transmit, publish, disseminate, duplicate … the OANDA Trading System Rates to any third party."
- §13(a): "you will not retransmit the same from your premises for any purpose, including retransmission to other premises of yours, without written permission from OANDA."
- OAP (Singapore): "(a) make no more copies…", "(g) use the OANDA System and the Licensed Materials for its own internal use only".

### Massive (Polygon). <https://massive.com/legal/market-data-terms-of-service> (updated 3 Nov 2026), <https://massive.com/legal/individuals-terms-of-service>
- §1: "access, download, store and use the Information … solely for your own personal, non-business and non-commercial use. Personal use includes your own research, analysis and investment activities".
- §5(g): "Use Information to train, fine-tune or distill an artificial-intelligence or machine-learning model unless expressly permitted and all applicable Third Party Provider requirements are satisfied".
- §8: "delete it promptly, and in any event within thirty (30) days, including downloaded files, stored copies and caches."
- The individuals ToS was read only as a summary. Coverage: <https://massive.com/knowledge-base/article/how-far-back-does-massives-forex-and-gold-history-go-and-are-there-gaps>

### Twelve Data. <https://twelvedata.com/terms> (updated 1 Jan 2026)
- §2.2(a) "Access, receive, process, and store Data solely for Internal Use"; §2.3(g) "Store or cache Data beyond permitted timeframes specified in the Documentation"; §16.2 "Upon termination or expiration: (a) All Data must be deleted within 30 days".
- 1-minute depth from 10 Feb 2020: <https://support.twelvedata.com/en/articles/5656039-how-to-get-historical-prices>

### Databento. <https://databento.com/legal/databento-user-agreement>
- "Customer agrees to limit all use of Third-Party Data by itself and its Authorized Users to internal Customer purposes and not to engage in any Redistribution".
- §9.3: "Upon termination for any reason: (i) … Customer may no longer download, access, or use any Third-Party Data".
- §3.5: "Databento commonly grants new users of the Platform one hundred twenty-five (125) Credits".

### Alpha Vantage. <https://www.alphavantage.co/terms_of_service/>, <https://www.alphavantage.co/documentation/>
- §2a: "for personal, non-commercial use"; use is commercial if it "goes beyond investment analysis, research, testing, monitoring, and any other activities that are private and individual in nature".
- `GOLD_SILVER_HISTORY`: "Strings daily, weekly and monthly are accepted". FX_INTRADAY: "this is a premium API function".

### Stooq. <https://stooq.com/terms.html>, <https://stooq.com/db/h/>
- §5.3 "Redistribution of data found on the website is not allowed without the consent of Stooq."
- Bulk page: "This data is intended solely for personal use. Any commercial use is prohibited."

### MetaQuotes / MT5. <https://www.metaquotes.net/licenses/terminal/mt5>, <https://www.mql5.com/en/about/terms>
- EULA: "MetaQuotes Ltd is a software development company and does not provide any financial, investment, brokerage, trading or data feed services".
- §2.1 purpose: "for organizing a trader's workstation and trading in the financial markets via Financial Institutions…"
- "All title and IP Rights in and to any third party content that is not contained in the MetaQuotes' Software, but may be accessed through use of the MetaQuotes' Software, is the property of the respective content owners".
- mql5.com Terms §3.7 ("not to access the website www.mql5.com through any automated means") covers the website, not terminal history.

### Tiingo, TrueFX, FXCM repo
- Read only as summaries (see the table).

## 3. Kaggle / Hugging Face: provenance red flags

Licences come from the Kaggle API (`/api/v1/datasets/view/<ref>`).

| Dataset | Licence | Provenance | Flag |
|---|---|---|---|
| `novandraanugrah/xauusd-gold-price-historical-data-2004-2024` (107k downloads) | CC0 | Not stated. MetaTrader-format timestamps (`2004.06.11 07:15`). M5 and above (not M1) | Unknown source, CC0 claimed: **red flag** |
| `rescoto/gold-spot-xauusd-5-minute-ohlc-candles` | CC0 | Not stated. Starts **2003-05-05**, the Dukascopy XAU/USD inception date (`startDayForMinuteCandles: 2003-05-05` in the dukascopy-node metadata) | **Likely Dukascopy: red flag** |
| `mbmanjunathkulal/xauusd-tick` | Unknown | Tick bid/ask with volumes from 2003-05-05 (Dukascopy fingerprint) | **Red flag** |
| `feriandanaputra/comprehensive-xauusd-historical-price-data` | CC0 | States: "sourced from the MetaTrader 5 (MT5) trading platform via Dukascopy Bank SA" | **Red flag (Dukascopy relicensed as CC0)** |
| `mahdimasoumian/xauusd-2020-2026` (M5) | MIT | "Downloaded from MetaTrader5" (broker unknown) | Broker data relicensed as MIT: red flag |
| HF `Pcitycrypto/xauusd` (M1, 2011-11 to 2024-01) | CC-BY-4.0 | Not stated. Dropped `VOLUME`/`SPREAD`, kept `tickvol`: looks like an MT terminal export | Red flag |
| `sheldonrobinson/histdata-forex-xauusd` | "Data files © Original Authors" | HistData | Honest licence; inherits HistData's unclear status |
| `orkadd/*`, `alchimistvq/*`, `getdatafinance/*` | MIT (samples) | Teasers for a paid shop (ork.ad) | Vendor sample; source and full terms not reviewed |

No Kaggle or HF XAUUSD M1 set has a credible chain of title. Several are visibly Dukascopy or broker data relabelled CC0, MIT or CC-BY.

## 4. FirstRate GC: verified format facts (public pages only)

Read on 2026-11-28 from FirstRate's public pages; every quote below was checked against the raw page text (FAQ, dataset card, sample page, price-adjustment page, data-quality page, GC product page). No sample file was downloaded, so nothing here was checked against an actual file. The importer (`FirstRateGcSource`) is written to these facts and refuses what does not fit them.

| Fact | Finding | Source |
|---|---|---|
| Timezone | "All data is in US Eastern time (EST or EDT depending on the time of year), except crypto data, which is in UTC." The dataset card: "Bar start time, US Eastern." | <https://firstratedata.com/about/FAQ>, <https://firstratedata.com/ai/datasets/futures-intraday> |
| DST handling | **Not stated.** "EST or EDT depending on the time of year" says the clock follows DST, but no page says how the repeated 01:00 hour in November is written. Not a practical issue for GC: both US DST changes fall at 02:00 local time on a Sunday, when CME Globex is closed. The parser refuses an ambiguous or non-existent Eastern time instead of guessing | FAQ (above); inference, not a vendor statement |
| Bar timestamp | "The intraday bars are all stamped as of the start time of the bar. Therefore the 09:30 1min bar runs from 09:30.00 to 09:30.59 inclusive." | FAQ |
| No-trade minutes | **Omitted.** "We don't include bars where no trades took place, so a missing minute means there was no trading in it." Data quality page: "we do not include zero volume bars in the data so when there is no trading there will be a gap in the sequence." | FAQ; <https://firstratedata.com/about/dataquality> |
| Series shipped | "Every futures purchase includes three continuous series for each ticker, in every timeframe: an unadjusted series of the raw contract prices, a ratio-adjusted series, and an absolute-adjusted series." Plus "the full chain of individual contracts". GC: "Continuous data is from 31-Jan-2008, Individual contracts start with GCZ08 (December 2008)" | FAQ; <https://firstratedata.com/i/futures/GC> |
| Roll rule | "We roll to the next contract on a fixed date for each ticker, set from that ticker's historical volume changeover. This is almost always before expiry — for many CME contracts, such as ES and NQ, roughly 7–10 days beforehand. A file listing every contract used to build each continuous series is under the 'Other Data' tab and is updated daily." **GC's own roll dates are not public**; that file is behind the customer page | FAQ |
| Roll time | Dataset card: rolls happen "at midnight ET". Price-adjustment page: the factor applies "from the start of the new contract's first trading session. For a Monday roll this is the Sunday evening session; for other roll dates it is midnight of the roll date." | <https://firstratedata.com/ai/datasets/futures-intraday>, <https://firstratedata.com/about/price_adjustment> |
| Adjustment method | "Ratio series: factor = new contract close ÷ old contract close"; "Absolute series: offset = new contract close − old contract close", from "the daily settlement closes … normally the day before the roll date". "The current contract is never adjusted … Factors compound backward". "This leaves a small residual at the join, typically a tick or two on liquid index and bond contracts". The same page's older paragraph describes the differential as "between the final closing price of the expiring contract and the opening price of the new front-month contract"; the two descriptions differ, and the settlement-based one is the more detailed | price_adjustment |
| Columns | FAQ: "Intraday bars: DateTime (yyyy-MM-dd HH:mm:ss), Open, High, Low, Close, Volume". Dataset card: volume is "Contracts traded in the bar". FAQ: "In adjusted data, volume can include decimal places … so it is safest to read the volume column as a floating-point number." | FAQ; dataset card |
| Delimiter | "All data is in csv (comma-separated values) format" | FAQ |
| Header row | **Conflicting.** The sample page says "Comma-separated, no header row, one bar per line in time order." The dataset card's example starts with `timestamp,open,high,low,close,volume`. The FAQ defers to "the ReadMe linked from your customer page, and in the sample download". The parser accepts either: a first line that names the six columns is skipped, anything else must parse as a bar | <https://firstratedata.com/free-intraday-data>; dataset card; FAQ |
| File name | `{ticker}_{period}_{timeframe}_{adjustment}`, e.g. `ES_full_1min_continuous_absolute_adjusted.txt`. **The exact GC ratio-adjusted name is not verified**: `GC_full_1min_continuous_ratio_adjusted.txt` is extrapolated from that one ES example. The reader accepts any name containing `_1min_continuous_ratio_adjusted`; if the real name differs, rename the file or adjust the check | FAQ |
| Session hours | FAQ, for daily bars: "for most CME contracts, 6pm US Eastern on the previous calendar day to 5pm." This matches CME's published GC hours (Sunday–Friday 18:00–17:00 ET, a 60-minute break from 17:00 ET), but **the CME pages could not be fetched** (timeouts); the CME figure comes from a search-result summary only. FirstRate: "We do not provide any futures metadata such as contract settlement details, trading calendars etc.." | FAQ; CME Group gold contract specs (not fetched) |
| Session hours over time | **Not verified.** One static schedule is applied back to 2008. Whether CME Globex metals hours (for example the length or start of the daily break) changed since then was not checked. If they did, early years show bars inside the schedule's closed time: `ImportReport.in_closed` reports them | — |
| Holidays | **Not covered** by FirstRate (no trading calendar) and not modelled here: a CME holiday or early close appears as a real gap | FAQ |

## 5. Rules adopted for the FirstRate import (#128)

These are pre-declared before any FirstRate data exists. They are marked **[PROPOSED]** and must be checked with the import report (`audit`, filled-minute counts) when the data arrives.

1. **Schedule** `cme-globex-gc-et`: closed daily 17:00–18:00 America/New_York and from Friday 17:00 to Sunday 18:00, DST-aware through the zone. No holiday calendar: holidays and early closes are reported as real gaps (fail-closed).
2. **No-trade minutes.** A run of up to **15** missing open-session minutes between two traded minutes is "no trade", not missing data: each minute becomes a flat bar at the previous close with volume 0, marked synthetic. An M5/M15/H1 bar built only from such minutes carries the `NO_TRADE` quality flag. Each of these is a real gap and flags `GAP` as before:
   - a run longer than 15 open minutes;
   - a run with no traded minute after it in the imported range;
   - a run holding a minute whose row validation dropped;
   - the open minutes before a scheduled closure, because a run never spans a closure.

   The fill therefore looks ahead at most 15 open minutes, within one session. Consequence: a range that ends inside a run leaves its tail as a gap, so extending that range later would rebuild a stored bar differently, and the store refuses it (`CandleConflict`, fail-closed); import the longer range as a new dataset version. The report lists the length of every filled run and counts the real gaps (`unexpected_gaps`), but not the length of runs over the limit, so checking the limit needs a separate look at the raw minute gaps.
3. **Roll.** Import the **ratio-adjusted** (multiplicative back-adjusted) continuous series only; the source refuses any other file name. Ratio adjustment multiplies a whole segment by one factor, so returns, ATR-normalised and other scale-free features are unchanged by it. An additive adjustment would change percentage returns and can go negative, so it is not used. Rows that cross a roll are not excluded; the remaining artefact is FirstRate's small residual at the join. Roll dates are recorded per dataset version in `candle_rolls` (from FirstRate's contract listing, converted by the owner or agent once the data arrives), so an exclusion arm or a residual check can use them later.
4. **Prices are not tradable levels.** Ratio-adjusted history is rescaled at every new roll, so a later download changes every older price: import it as a **new dataset version**, never an append. Adjusted prices, and futures prices in general, must never be compared with the broker's spot price levels. Returns and ATR-normalised values are unchanged by the adjustment, but anything in price units is scaled by the cumulative roll factor in older history: the raw outcome `move`, and any fixed spread or slippage buffer in price units (#129) must be scaled or expressed in ATR or percent terms.
5. **No spread.** `spread` is empty; the label's locked spread buffer applies (#129).
6. **Volume** is contracts traded (a decimal is allowed), not tick volume. Normalise or exclude it per data source (#129).
