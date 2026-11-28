# Dukascopy historical data: terms of use

Status: **SNAPSHOT** (2026-11-28). This reads Dukascopy's published terms for #128 / PRD #120. It is not legal advice. **The owner decides** whether and how to use Dukascopy data. Nothing was downloaded for this note.

## Question

1. May a private individual use Dukascopy historical data (the historical data feed / export, JForex history) to train a model used for their own personal trading?
2. May the data, or anything derived from it, be redistributed? For example: committed to git, shared, or published.

## What the text says

**Main source:** [Terms of Use :: Dukascopy Bank SA](https://www.dukascopy.com/swiss/english/legal-pages/terms-of-use/).
- The page shows no version or effective date; the footer reads "© 1998-2026 Dukascopy® Bank SA".
- §11 says Swiss law applies and disputes go to arbitration in Geneva.
- The quotes below were re-fetched on 2026-11-28. The fetch tool limits the length of a quote, so longer sentences appear as exact fragments.

| Topic | Verbatim text |
|---|---|
| Scope | "By continuing to access, link to, or use this website, or any service accessible via this website" (introduction). §15 says the terms "govern your use of the WEBSITE." |
| Personal use only | §3: "you may download material from the WEBSITE and/or make one print copy for your own personal, non-commercial use". §4(i): "Such download is licensed to you by DUKASCOPY ONLY for your own personal, non-commercial use" |
| Databases | §3: "The WEBSITE and the information contained therein may not be used to construct a database of any kind." Also: "Nor may the WEBSITE be stored (in its entirety or in any part) in databases for access by you or any third party" |
| Redistribution | §3: "You may not recirculate, redistribute or publish the analysis and presentation included in the WEBSITE" (without prior written consent). §3 also forbids copying, distributing or publishing any part of the WEBSITE, except as the download permission allows. |
| Automated access | §3: "You shall not use or attempt to use any "scraper," "robot," "bot," "spider," "data mining," "computer code,"" (without prior express written consent) |

**Other pages checked:**
- [Important Disclaimer](https://www.dukascopy.com/swiss/english/legal-pages/important-disclaimer/) has no clause on use or redistribution. It says only that downloaded data is made available "at your request and discretion only" (quoted by a research subagent; not re-verified).
- The [historical data export page](https://www.dukascopy.com/swiss/english/marketwatch/historical/) renders its tool in JavaScript, so the fetch saw no terms next to it.
- The JForex forum threads returned HTTP 410. The wording of the JForex Historical Data Manager download prompt was **not found**.
- **Secondary, unverified:** a Tickstory staff post (<https://support.tickstory.com/viewtopic.php?p=8363>, 2018) says users are "bound by Dukascopy's license terms". A user in the same thread reports that the JForex prompt says "PERSONAL USE".

## Reading

- **Training a model for personal trading: not addressed explicitly.** It fits "personal" use. Three things are unclear:
  - whether trading for one's own profit counts as "non-commercial";
  - whether a stored multi-year M1 set breaks "construct a database of any kind" / "stored ... in databases";
  - whether bulk download with tools (dukascopy-node, duka, Tickstory) needs the written consent the automated-access clause requires.
- **Redistribution: the text points to no.** The download licence is "ONLY" for personal use, and §3 forbids distributing and publishing. Committing raw data to this repo (private today) would be "storing ... in databases for access by ... any third party" as soon as anyone else has access. The terms do not address derived artefacts (features, model weights).
- **Main ambiguity:** these are website terms. They cover the data feed only through "any service accessible via this website". The JForex prompt may hold more specific terms. Only written clarification from Dukascopy settles the commercial-use, database and automated-download questions.

## Consequences already in the code (#128)

- Raw vendor data is never committed. `.gitignore` ignores `data/**`, and also `data/historical/**` explicitly, with no README exception.
- No downloader or network fetcher exists. The import reads a local CSV file (`CsvM1Source`) that the owner provides.
- The **Data Source** of every bar and dataset row is recorded (`candle_sources`, export `data_source`), so Dukascopy-derived data can be found and removed if the owner decides against it.

## Open for the owner

- Decide whether to use Dukascopy data at all, given the "personal, non-commercial" licence and the database clause.
- If yes, consider asking Dukascopy in writing about model training for personal trading and about storing the data locally.
- Decide whether derived artefacts (model weights trained on it) may leave the machine, e.g. through the Colab runner (#134).
