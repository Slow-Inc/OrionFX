# M04 Prediction Engine: แผน dataset และการเทรน (ปรับจากคู่มือภายนอกให้เข้ากับ OrionFX)

**Status:** SNAPSHOT (2026-11-28) — แผนก่อนเทรน ไม่ใช่ผลงานที่ส่งมอบแล้ว ห้ามแก้ ถ้าแผนเปลี่ยนให้เพิ่มไฟล์ใหม่
**Issue:** #48 (M04 slice C: trained XGBoost/LightGBM artifacts — ⛔ blocked) · ข้อมูลจริงจาก M07 dataset (PRD #107: #108–#110)
**ที่มา:** คู่มือ "OrionFX (M04 Prediction Engine): คู่มือคัดแหล่ง Dataset, เทคนิคการเทรน และสถาปัตยกรรม Multi-Model" ที่ผู้พัฒนาอัปโหลด (ไม่ได้ commit ไว้ใน repo) ผู้เขียนคู่มือระบุว่าเปิด `docs/plans/master_planV1.1.md` และ `backend/app/prediction/` ไม่ได้ เอกสารนี้จึงเทียบคู่มือกับ master plan และโค้ดปัจจุบัน แล้วบันทึกว่ารับอะไร ปรับอะไร เลื่อนอะไร และเพิ่มอะไร
**ไม่แตะ master plan:** ทุกอย่างในนี้อยู่ภายใต้ PRE-01..05, VAL-02, §M04 "PRD must specify" และ §Validation อยู่แล้ว ไม่มีข้อไหนเปลี่ยนรายการ [LOCKED]

> **ข้อเท็จจริงภายนอกยังไม่ได้ตรวจซ้ำ:** ข้อมูลของแหล่งข้อมูลภายนอกในคู่มือยังไม่ได้ตรวจซ้ำ ได้แก่ ความครอบคลุมของ Dukascopy/OANDA/Databento, เวลาประกาศ COT (ศุกร์ 15:30 ET), API ของ FMP/EODHD และเงื่อนไขการใช้ข้อมูล Dukascopy ต้องตรวจก่อนใช้จริงใน PRD
>
> **ตัวเลขทุกตัวในเอกสารนี้เป็น `[PROPOSED]`:** ยังไม่มีผลเทรนหรือความแม่นยำใดๆ และห้ามอ้างตัวเลขความแม่นยำจากเอกสารนี้

---

## 1. สถานะปัจจุบันที่แผนต้องต่อยอด

| ส่วน | มีแล้ว | ช่องว่างที่เกี่ยวกับการเทรน |
|---|---|---|
| Predictor port (M04) | `Predictor.predict(features) -> Mapping[Direction, Decimal]` เป็นการกระจายความน่าจะเป็นตาม**ทิศทาง** มี `ModelCard` ที่ระบุ `feature_schema`, `required_features`, `feature_ranges`, `horizon`, `horizon_bars` และ `calibrated` และ engine ปฏิเสธ feature ที่อยู่นอกช่วงที่ validate | ยังไม่มีโมเดลที่เทรนแล้ว (#48) มีแค่ baseline + ensemble |
| Feature (M02) | `m02.features.v1` คิดแบบ point-in-time ทุก timeframe | หลาย output เป็น**หน่วยราคาดิบ** ได้แก่ `sma`, `ema`, `wma`, Bollinger `middle/upper/lower`, `atr`, `macd/signal/histogram` ส่วน `tick_volume` เทียบข้ามแหล่งข้อมูลไม่ได้ |
| Candle | สร้างจาก **bid** (OHLC = bid) และ `spread` ต่อแท่ง = spread **สูงสุด**ในแท่ง (ค่าระมัดระวัง) บางแท่งไม่มี spread | label ฝั่ง long ออกที่ bid ส่วน short ออกที่ ask จึงต้องใช้ spread ต่อแท่ง |
| Label (M07) | `BarOutcome.move` = ราคาปิดปลาย horizon ลบราคาปิดต้น (หน่วยราคา) ใช้ทั้งใน `prediction_outcomes` และ `veto_report` | ไม่สะท้อนการเทรดจริงที่มี SL/TP |
| Export (M07) | `export_dataset` เรียงลำดับแน่นอน ต่อข่าว ณ `as_of` และ purge แถวที่ `as_of + longest > split.end` | ยังไม่มี **embargo**, provenance มีแค่ `LIVE/BACKFILL` และยังไม่มีมิติ**แหล่งข้อมูล** |

---

## 2. สิ่งที่รับจากคู่มือ (ตรงกับ master plan อยู่แล้ว)

- **เริ่มจาก LightGBM/XGBoost ก่อน PyTorch:** PRE-01 และ CAT-A01-01/02 ส่วน deep model เป็นงานระดับ ADV
- **walk-forward แบบเรียงตามเวลา + purge/embargo + calibration ที่แยกตามเวลา + ตัวชี้วัดหลังหักต้นทุน:** PRE-05, VAL-02, §M04 "PRD must specify" และ §Validation
- **แยกบทบาท + global model ตัวเดียว ยังไม่ทำ MoE ใน V1:** ตรงกับโครงสร้างปัจจุบัน M03 ตีความข่าว, M04 ทำนาย, M05 ตัดสินใจและเลือก WAIT (รวม `NEWS_VETO` #115), M06 คุมความเสี่ยง ส่วน regime ใส่เป็น feature (CAT-T03-06, CAT-A01-04)
- **MoE และ soft blending:** เลื่อนไป ADV และทำก็ต่อเมื่อ ablation บน walk-forward ชุดเดียวกันชนะ global model
- **Feature แบบ stationary, กันการรั่วข้าม timeframe และตัวชี้วัด (Brier/log-loss, precision ที่ความมั่นใจสูง, PF/Sharpe/DD หลังหักต้นทุน):** รับทั้งหมด

---

## 3. สิ่งที่ต้องปรับให้เข้ากับ OrionFX

### 3.1 Target: triple-barrier ต่อทิศทาง (ไม่ใช่ meta-labeling ใน V1)
**ข้อเท็จจริง:** port ปัจจุบันเป็นแบบทิศทาง (`Mapping[Direction, Decimal]`) ไม่ได้ให้ "P(win) ของ setup ที่ strategy เสนอ"

- **ตัวเลือกที่แก้น้อย (แนะนำสำหรับ V1):** ใช้ label แบบ triple-barrier "ชนกำแพงไหนก่อน" ซึ่งเข้ากับ port เดิม
  - UP = ชน TP ฝั่งขึ้นก่อน, DOWN = ชน TP ฝั่งลงก่อน, FLAT = หมดเวลา
  - เป็นการตัดสินใจที่ต้องระบุใน PRD
- **Meta-labeling (ให้ M04 ทำนาย P(win) ของ proposal):** เข้ากับแนวคิด EV ของ M05 แต่มีต้นทุนสูงกว่า จึงเป็นทางเลือกในภายหลัง
  - ต้องเปลี่ยน **contract** ของ predictor port
  - ต้อง replay strategy proposals ย้อนหลังทั้งหมด
  - ได้ sample น้อยกว่ามาก เพราะนับเฉพาะแท่งที่มี proposal

### 3.2 นิยาม triple-barrier
นิยามต้องระบุให้ชัดและมี version

- **กำแพง:** บน `+k1 × ATR`, ล่าง `−k2 × ATR`, เวลา `N` แท่ง โดย k1, k2, N เป็น `[PROPOSED]` และต้องล็อกก่อนเทรน
- **ราคา:** ใช้ bid OHLC
  - long ออกที่ bid เข้าที่ ask
  - short เข้าที่ bid ออกที่ ask ซึ่งต้องบวก `spread` ของแท่ง
  - แท่งที่ไม่มี spread ใช้ค่า buffer ที่ล็อกไว้ ไม่ใช้ศูนย์
- **ต้นทุน:** บวก slippage buffer `[PROPOSED]`
  - spread ของ Dukascopy ECN มักแคบกว่าโบรกเกอร์ MT5 จึงต้องบวก buffer เพิ่ม (ตามคู่มือ)
- **กรณีกำกวม:** ถ้าในแท่งเดียวกันชนทั้งสองกำแพง OHLC บอกลำดับไม่ได้
  - **กฎ V1:** ถือว่าชน SL ก่อน (ฝั่งระมัดระวัง)
  - **ทางเลือก:** ดูลำดับจาก timeframe ที่เล็กกว่า (M1 หรือ tick) เมื่อมีข้อมูล
- **การสร้าง label:** สร้างตอน export/train จาก OHLC ที่เก็บไว้แล้ว **ไม่ต้องแก้ recorder** แต่ label ต้องมี `label_version`
- **Offline/online parity (§M04):** การประเมิน live (`prediction_outcomes`, การ์ดโมเดล) ต้องใช้นิยาม label ชุดเดียวกับตอนเทรน
  - ให้ `move` แบบเดิมคงไว้เป็นข้อมูลดิบ
  - ส่วนการวัดผลโมเดลให้อ่าน triple-barrier label ที่มี version เดียวกัน
  - `veto_report` ใช้ `move` ต่อได้ เพราะวัดคนละคำถาม

### 3.3 Feature แบบ stationary: เป็นชั้น transform ใน M04 ที่ใช้ทางเดียวกันทั้ง train และ inference
- เป็นหน้าที่ของ M04 ตาม §16.5 "Feature transforms" ไม่แก้ schema ของ M02
- **ต้องเป็น code path เดียว:** สิ่งที่เทรนเห็นต้องเป็นสิ่งเดียวกับที่ `Predictor` เห็นตอนรันจริง โดยมี schema version ของตัวเอง (เช่น `m04.transforms.v1`) บันทึกใน `ModelCard`
- **ตัวอย่างการแปลง:**
  - `(close − sma)/atr`
  - ตำแหน่งในแถบ Bollinger
  - `macd/atr`
  - `atr/close`
  - log return หลายช่วง
  - `spread/atr`
  - `sin/cos` ของชั่วโมง UTC
- **`tick_volume`:** ตัดออก หรือ normalise แยกตามแหล่งข้อมูล เพราะ Dukascopy กับโบรกเกอร์นับต่างกัน

### 3.4 แหล่งข้อมูลเป็นมิติแยกจาก provenance
- `LIVE/BACKFILL` บอก "บันทึกอย่างไร" ส่วนแหล่งข้อมูลบอก "มาจากไหน" (Dukascopy / broker MT5 / OANDA)
- ต้องแยกกันได้ทุกแถว กลไกที่ใช้ให้ PRD ตัดสิน
- **ห้าม commit ข้อมูลดิบของผู้ให้บริการลง repo**

### 3.5 Backfill ต้องผ่าน feature builder ตัวเดียวกับ live
- ข้อมูลย้อนหลังต้องผ่าน `MarketRecorder` (provenance BACKFILL) และ `build_feature_snapshot` ตัวเดียวกัน **ไม่ทำ pipeline แยกด้วย pandas** เพื่อให้ offline กับ online ตรงกัน
- **ความเสี่ยงที่ต้องวัด:** การคำนวณแบบ Decimal กับข้อมูล M5 ราว 10 ปีอาจช้ามาก ต้องวัด throughput บนตัวอย่างก่อน (เช่น 1 เดือน) แล้วค่อยตัดสินใจ

---

## 4. Validation: ความเสี่ยงที่คู่มือไม่ได้พูดถึง

### 4.1 ห้ามให้ LLM ตีความข่าวย้อนหลังแล้วนำมาใช้ประเมิน (ข้อสำคัญที่สุด)
LLM ถูกเทรนหลังเหตุการณ์ในอดีต จึงรู้อยู่แล้วว่าตลาดไปทางไหน ถ้าเอาข่าวปี 2020–2025 ให้ model ปัจจุบันตีความ แล้วใช้ผลในการเทรนหรือ ablation แบบ "Chart + News (M03)" ผลจะ**ดีเกินจริง**

- **ข้อมูลข่าวที่ใช้ได้:** มีแค่ `news.assessment` ที่ M03 บันทึกไว้ **ตอนเกิดจริง** (`available_at`) ซึ่งคือ dataset ที่เริ่มเก็บใน #108–#110
- **ช่วงแรก:** baseline เป็น chart-only ส่วน Chart + News ต้องรอข้อมูลจาก Shadow/Demo สะสมก่อน
- **Lineage:** assessment แต่ละรายการมี model และ prompt version (`m03.reason.v2`) ติดมา
  - ถ้าเปลี่ยน LLM หรือ prompt การกระจายของ feature จะเปลี่ยน
  - ต้องแยกหรือ stratify ตาม lineage

### 4.2 การรั่วของเวลาข้ามแหล่งข้อมูล
Dukascopy กับ MT5 เป็นตลาดเดียวกัน

- ทุกหน้าต่าง validation/calibration ต้องอยู่**หลัง**ข้อมูลเทรนทุกแหล่ง
- purge และ embargo ต้องใช้ข้ามแหล่งด้วย
- เช่น ห้ามเทรนด้วย Dukascopy ปี 2026 แล้ว calibrate ด้วย MT5 ปี 2025

### 4.3 Embargo
- **เพิ่ม embargo ใน export:** อย่างน้อยเท่ากับ time barrier ที่ยาวที่สุด (`N` แท่ง) หลังขอบของแต่ละ split
- **ปัจจุบัน:** มีแค่ purge

### 4.4 Final holdout ต้องล็อกก่อนเทรน (แบบเดียวกับ `m03-llm-eval/v2/DECISION_RULE.md`)
| รายการ | ค่า `[PROPOSED]` | หมายเหตุ |
|---|---|---|
| ช่วง train/validate แบบ walk-forward | 2016-01-01 → 2025-06-30 (expanding window, fold ละ 6 เดือน) | ขึ้นกับความครอบคลุมจริงของแหล่งข้อมูล |
| Embargo | ≥ `N` แท่งของ horizon ยาวสุด | ข้ามแหล่งข้อมูลด้วย |
| **Final holdout** | 2025-07-01 → 2026-06-30 | **ห้ามแตะ**จนถึงการประเมินครั้งสุดท้าย ห้ามปรับ hyperparameter หรือเกณฑ์โดยดูผลจากช่วงนี้ |
| Forward ที่ไม่เคยเห็น | Shadow/Demo หลัง #17 | สุดท้ายต้องผ่านข้อมูลจริงที่เกิดหลังการเทรน |
| เกณฑ์ตัดสิน | เทียบ **no-trade baseline** และ `Baseline` เดิม โดยใช้ Brier/log-loss และ net expectancy หลังต้นทุน | precision ที่ `P ≥ 0.65` ใช้เป็นตัวชี้วัดรอง และ 0.65 เป็น `[PROPOSED]` |
| ค่าที่ต้องล็อก | k1, k2, N, slippage buffer, time-decay, จำนวน seed, จำนวน config ที่จะลอง | จำกัดจำนวน config ล่วงหน้าเพื่อกัน multiple testing |

- เจ้าของงาน (owner) ต้องยืนยันตารางนี้ก่อนเริ่มเทรน
- เกณฑ์ readiness เชิงตัวเลขอยู่ภายใต้ OD-08 และ §Validation เป็น protected thresholds

---

## 5. แหล่งข้อมูล: ลำดับการใช้

| แหล่ง | บทบาท | สถานะ |
|---|---|---|
| Dukascopy (tick/M1 → M5/M15/H1) | pre-train ระยะยาว | ทำได้เลย ต้องตรวจเงื่อนไขการใช้ข้อมูลก่อน |
| Broker MT5 (`copy_rates_range`/`copy_ticks_range`) | calibration และ validation ที่ตรงกับโบรกเกอร์จริง | ⛔ #17 |
| Recorded live (M07 dataset + `news.assessment`) | forward evaluation และ Chart + News ablation | ⛔ รอข้อมูล Shadow สะสม (#17, #41) |
| OANDA | ตรวจข้ามโบรกเกอร์ | ทีหลัง ต้องมีบัญชี practice |
| ALFRED/FRED, CFTC COT, Economic calendar | feature macro / positioning / surprise | ทีหลัง เพิ่มทีละตัวพร้อม ablation ต้องเป็น point-in-time (เช่น COT เลื่อนไปหลังเวลาประกาศจริง ตามคู่มือที่ยังไม่ได้ตรวจซ้ำ) |
| Databento (COMEX `GC`/`MGC`) | order flow | ไม่ทำใน V1 เพราะเสียเงิน |
| HistData / Kaggle | prototype เท่านั้น | ไม่ใช้เทรน production (เห็นด้วยกับคู่มือ) |

---

## 6. Roadmap พร้อมสถานะ

| # | ขั้น | สถานะ |
|---|---|---|
| 1 | PRD M04 training (ใต้ #48): ล็อกนิยาม label, transforms, splits, holdout และเกณฑ์ตัดสินในข้อ 4.4 | **ทำได้เลย** |
| 2 | Import Dukascopy เข้า candle store ผ่าน recorder BACKFILL ติดแหล่งข้อมูล + `dataset_version` และวัด throughput บนตัวอย่าง | **ทำได้เลย** (หลังตรวจเงื่อนไขการใช้ข้อมูล) |
| 3 | Triple-barrier label แบบมี version + embargo ใน export + parity กับการประเมิน live | **ทำได้เลย** |
| 4 | ชั้น stationary transform ใน M04 (code path เดียว มี schema version) | **ทำได้เลย** |
| 5 | LightGBM (+XGBoost) multi-seed + calibration (isotonic/Platt บนช่วงแยกตามเวลา) + purged walk-forward เทียบ no-trade/`Baseline` บน chart-only | ทำได้หลังข้อ 1–4 (เป็นการเทรน) |
| 6 | Calibrate และ validate ด้วยข้อมูลโบรกเกอร์ MT5 แล้วรัน Shadow | ⛔ #17 |
| 7 | Chart + News ablation ด้วย assessment ที่บันทึกตอนเกิดจริงเท่านั้น | ⛔ รอข้อมูล Shadow สะสม (#17, #41) |
| 8 | Macro/COT/calendar features, regime-specific/MoE (soft blending), PyTorch stacking | ADV ทำเมื่อ ablation ชนะ |

ยังไม่มีขั้นใดในตารางที่ทำแล้ว
