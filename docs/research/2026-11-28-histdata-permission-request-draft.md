# HistData.com: written-permission request (DRAFT, NOT SENT)

Status: **DRAFT** (2026-11-28). Nothing has been sent. **The owner decides** whether to send it, and sends it from their own account through <https://www.histdata.com/support/>. Context: `2026-11-28-xauusd-data-sources.md` (HistData ranks third: it has the best spot data fit, but no terms of use or licence page exists, so its status is unclear).

Fill in the bracketed fields before sending. Keep the reply (or the absence of one) with this note: a written grant turns the verdict into permissive; a refusal or silence keeps HistData out of the training corpus.

---

**Subject:** Permission request: personal, non-commercial use of XAUUSD M1 data for model training

Hello HistData team,

I would like to use your free XAUUSD 1-minute bar data (the M1 files, 2016 to the present) and I could not find terms of use or a licence on your site. Before downloading anything, I am asking for your written permission for the use described below.

1. **Purpose: personal machine-learning training.** I am building a personal trading research tool. I would use the data to train and evaluate statistical / machine-learning models that I use only for my own trading decisions. It is not a commercial product, service or signal, and no one else would use the models or the data.
2. **Local storage.** I would convert the files and store the bars, and values derived from them (resampled bars, indicators, labels), in a local SQLite database on my own computer, kept for as long as I use the models.
3. **Cloud processing.** Model training would run on Google Colab (a cloud notebook service). The data would be copied to a private Colab session only for the duration of a training run, with no public sharing. Only the trained model and its metrics would come back.
4. **No redistribution.** I would not publish, share, sell or redistribute the data or any file containing it, in raw or converted form. It would not be committed to any public or shared code repository. Trained model weights would stay private.
5. **Download method.** I would download the files manually through your website forms (or your paid FTP service, if you prefer), not with automated scraping.

Could you confirm in writing whether this use is permitted, and whether any conditions apply (for example attribution, a time limit, or deletion on request)? It would also help to know where the data comes from, so I can describe it correctly in my own records.

Thank you for providing this data.

Kind regards,
[Owner name]
[Contact address]
