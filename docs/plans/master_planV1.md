# XAUUSD AI Auto Trading Bot — Master Plan V1.0

> **ไฟล์:** `master_planV1.md`  
> **ประเภท:** Master Requirements / Architecture / PRD Handoff  
> **สถานะ:** Requirements และ Conceptual Design เสร็จสิ้น; ยังไม่เริ่ม Implementation หรือ Validation  
> **กลุ่มผู้อ่าน:** Project Owner, Product/Technical Lead, Developer, QA, AI Coding Agent และผู้รับผิดชอบแต่ละโมดูล  
> **ภาษา:** ไทยสำหรับคำอธิบาย, English สำหรับ identifier และ technical contract

เอกสารนี้เป็นแหล่งอ้างอิงกลางสำหรับให้แต่ละคนหรือ AI Agent เขียน Product Requirements Document (PRD) ของส่วนที่รับผิดชอบได้อย่างอิสระ โดยทุก PRD ต้องสอดคล้องกับกฎธุรกิจ ขอบเขต ความปลอดภัย และ interface ที่ใช้ร่วมกันในเอกสารนี้ ไม่ใช่เอกสารยืนยันว่าบอทหรือกลยุทธ์ทำงานได้จริงแล้ว

## 0. วิธีใช้เอกสารและกติกาสำหรับผู้รับงาน

### 0.1 สถานะของข้อกำหนด

- **[LOCKED]** ข้อกำหนดที่ผู้ใช้เลือกแล้ว ห้ามเปลี่ยนเอง หากขัดแย้งกับงานของโมดูลให้ส่งกลับ Project Owner ผ่าน Change Request
- **[PROPOSED]** รายละเอียดทางเทคนิคที่เสนอเพื่อให้เริ่มเขียน PRD ได้ เป็นค่าเริ่มต้นของการออกแบบ ไม่ใช่ข้อกำหนดที่ผู้ใช้อนุมัติเป็นรายข้อ ผู้รับงานปรับได้ตามหลักฐานและการ review ร่วมกัน
- **[TBD]** ต้องตัดสินใจจากการออกแบบย่อย การทดลอง หรือข้อมูลจริง ห้ามสมมติว่าได้รับอนุมัติแล้ว และห้ามปล่อย Live หากเป็น safety-critical TBD
- **[MVP]** ต้องอยู่ใน Full AI MVP; **[ADV]** พัฒนาหลัง MVP ตาม roadmap ไม่ใช่การตัด requirement ออกจากโครงการ

ลำดับอำนาจ: ข้อกำหนด [LOCKED] ล่าสุดในเอกสารนี้ > PRD ที่ผ่านการ review > Technical Design > Implementation. กฎความปลอดภัยและการปฏิเสธคำสั่งเมื่อไม่สามารถยืนยันความปลอดภัยได้มีความสำคัญเหนือการทำกำไรหรือความพร้อมใช้งาน

### 0.2 ขั้นตอนทำงานของแต่ละคน / Agent

1. อ่านเอกสารนี้ทั้งหมดก่อนเริ่มงาน โดยเฉพาะ §3–5 และ §8–11
2. ระบุ Module ID ที่ได้รับมอบหมายจาก §2; ชื่อทีมเป็นบทบาท ไม่ได้กำหนดว่าต้องมี 8 คนพอดี
3. สร้าง PRD ตาม path และแม่แบบใน §12 ระบุ Requirement ID, dependency, input/output, failure behavior, acceptance test และขอบเขต MVP/ADV
4. ตรวจสอบ contract กับเจ้าของโมดูลต้นทางและปลายทางก่อน implement; ห้ามสร้าง contract ที่ขัดกันเอง
5. หากพบข้อกำหนดไม่ชัด ให้บันทึกใน Open Decisions พร้อมผลกระทบและทางเลือก ไม่ต้องกลับไปถาม Requirements ทั้งหมดอีก
6. เมื่อ PRD ผ่าน review จึงสร้าง implementation tasks, tests และ code ตามลำดับ dependency
7. ส่งมอบ PRD, evidence, test result และรายการสิ่งที่ยังไม่ผ่าน ห้ามรายงานว่า done เมื่อยังไม่ได้ทดสอบจริง

**ข้อห้าม:** ไม่เพิ่มฟีเจอร์นอก scope; ไม่เปลี่ยนความเสี่ยงหรือเป้ากำไรเอง; ไม่ใช้เงินจริงระหว่างสร้างและทดสอบ; ไม่ให้ LLM ส่งคำสั่งเทรดโดยตรง; ไม่ใส่ API key, password หรือข้อมูลบัญชีจริงใน repo, test fixture, log หรือ prompt.

---

## 1. Product vision & scope

### 1.1 Product vision [LOCKED]

สร้าง **Advanced AI Auto Trading Bot สำหรับใช้งานส่วนตัว** เทรด XAUUSD บน MT5 อัตโนมัติ โดย AI วิเคราะห์ข่าวและกราฟ คาดการณ์ตลาด เลือกและสลับกลยุทธ์ เปิดไม้ บริหารไม้/แก้ไม้ รักษากำไร และเรียนรู้จากผลลัพธ์ ภายใต้ Risk Engine ที่เป็นอิสระจาก AI.

ผู้ใช้กำหนด Initial Lot, Profit Target เป็น USD ต่อรอบ, Maximum Drawdown (%) และการตั้งค่าที่จำเป็น แล้วควบคุมผ่าน Web App. เมื่อถึงเป้ากำไรสุทธิรวม ให้สั่งปิดทุกไม้ของบอท และหาจังหวะเปิดรอบใหม่โดยอัตโนมัติหากยังได้รับอนุญาต. ไม่ต้องเปิดเบราว์เซอร์ไว้ตลอด แต่เครื่องที่รัน backend, execution และ MT5 ต้องทำงานอยู่.

### 1.2 เป้าหมายและตัวชี้วัด

- Functional success: ควบคุมบอทผ่าน Browser ได้; เทรด Demo อัตโนมัติ; คำนวณกำไร/DD ถูกต้อง; ปิดและกู้คืนสถานะได้; ไม่แตะไม้ของผู้ใช้
- AI success: ทำนายและวิเคราะห์ข่าว/กราฟได้ มีเหตุผลอ้างอิงข้อมูลจริง มี WAIT, confidence calibration, adaptive decision และหลักฐาน validation
- Risk success: คำสั่งที่ไม่ผ่านความเสี่ยงต้องถูกบล็อก; DD lock ต้องคงอยู่หลัง restart; ระบบ risk ไม่ต้องรอ LLM; เหตุการณ์สำคัญตรวจสอบย้อนหลังได้
- Outcome metrics: net P&L after costs, maximum drawdown, profit factor, expected value, cycle win rate, target hit rate, robustness, latency. ไม่รับประกันกำไรหรือกำหนดเป้าผลตอบแทนขั้นต่ำที่ยังไม่ได้ทดสอบ

### 1.3 In scope [LOCKED]

XAUUSD, MT5, M5/M15/H1, Hybrid AI + Technical Analysis + News, Adaptive Strategy, AI Recovery, USD Profit Cycle, Account Equity DD, Basket Trailing, Emergency Protection, AI Learning & Memory, Browser Dashboard, การจัดเก็บ Local/Cloud/Hybrid, การติดตั้ง Local/VPS/Hybrid, Backtest/Shadow/Demo/Forward/Live เมื่อผ่านเกณฑ์.

เป้าหมายรองรับเงินทุนเริ่มต้น $50–100 แต่ไม่รับประกันว่าบัญชีขนาดนี้จะเปิดไม้หรือแก้ไม้ได้อย่างปลอดภัย บอทต้องปฏิเสธหาก Lot ขั้นต่ำหรือ margin ไม่เหมาะสม. เป้าหมายรองรับหลายโบรกเกอร์ MT5 เป็นเรื่องของ compatibility testing ไม่ใช่การรับรองทุกโบรกเกอร์ล่วงหน้า.

### 1.4 Out of scope [LOCKED]

ไม่สร้าง Trading Platform, multi-user SaaS, subscription/billing, marketplace, copy/social trading, native mobile app, own cloud platform, Kubernetes หรือแยก microservices เกินความจำเป็น. ไม่เพิ่มระบบ cost-management product ที่ยังไม่ได้อนุมัติ; การลดการเรียก LLM ที่ไม่จำเป็นและการติดตามข้อผิดพลาด API เป็นส่วนของ reliability เดิม.

---

## 2. Work breakdown — แบ่งเจ้าของ PRD

**[PROPOSED]** แบ่งตาม 8 โมดูลเดิม พร้อมงานบูรณาการของ Technical Lead และ QA ซึ่งเป็นบทบาทสนับสนุน ไม่ใช่โมดูลผลิตภัณฑ์ใหม่.

| ID | Workstream / Owner role | PRD ที่ต้องส่ง | Requirement หลัก | Dependency หลัก |
|---|---|---|---|---|
| M01 | Web Dashboard / Frontend & UX | `docs/prd/PRD-01-web-dashboard.md` | UI-01–05 | M06, M08; อ่าน M02–05, M07 |
| M02 | Market Data Engine / Data Engineer | `docs/prd/PRD-02-market-data.md` | DAT-01–05 | M08, MT5 read adapter ของ M06 |
| M03 | LLM & News Engine / AI Engineer | `docs/prd/PRD-03-llm-news.md` | LLM-01–05 | M02, M08, M07 |
| M04 | Prediction Engine / ML Engineer | `docs/prd/PRD-04-prediction.md` | PRE-01–05 | M02, M03, M08; M07 สำหรับ learning |
| M05 | Adaptive Decision Engine / Quant & AI Engineer | `docs/prd/PRD-05-adaptive-decision.md` | DEC-01–07 | M02–04, M06, M07 |
| M06 | Risk & Execution Engine / MT5 & Risk Engineer | `docs/prd/PRD-06-risk-execution.md` | TRD-01–06, PFT-01–05, RSK-01–09 | M08, MT5, M05 (intent เท่านั้น) |
| M07 | Learning & Memory Engine / ML/MLOps Engineer | `docs/prd/PRD-07-learning-memory.md` | LRN-01–06 | M02–06, M08 |
| M08 | System Core / Backend & DevOps Engineer | `docs/prd/PRD-08-system-core.md` | SYS-01–08 | ทุกโมดูลผ่าน shared contract |
| X01 | Architecture & Integration / Tech Lead | `docs/prd/PRD-X01-integration.md` | INT-01–04 | ทุกโมดูล |
| X02 | Validation & QA / QA & Quant Validation | `docs/prd/PRD-X02-validation.md` | VAL-01–06 | ทุกโมดูล |

หาก M06 ใหญ่เกินไป สามารถแบ่ง PRD ย่อยเป็น MT5 Execution, Risk Guard, Profit Management ได้ แต่ M06 ต้องมีเจ้าของ integration เพียงหนึ่งบทบาทที่รับผิดชอบ invariants ทั้งหมด. X01 ดูแล contract/versioning; X02 ดูแล evidence และ gate policy ร่วมกับ M06/M07; Project Owner เป็นผู้อนุมัติการเปลี่ยน scope และการเปิด Live. ไม่มีการมอบหมายชื่อบุคคลจริงในเอกสารนี้.

### 2.1 Ownership rules

- M01 แสดงผลและส่ง control command เท่านั้น ไม่คำนวณผลการเทรดที่ใช้ตัดสินใจเอง
- M02 เป็นแหล่งข้อมูลตลาดมาตรฐาน; M03 เป็นเจ้าของการตีความข่าว/LLM; M04 เป็นเจ้าของ forecast และ calibration
- M05 เป็นผู้สร้าง trade intent ไม่ใช่ executor; M06 เป็นผู้ตัดสิน risk veto และส่งคำสั่งจริงเพียงทางเดียวผ่าน EA
- M07 เป็นเจ้าของ experience, memory, model lifecycle; ไม่มีสิทธิ์เปลี่ยน risk ceiling หรือ activate โมเดลเองนอก validation policy
- M08 เป็นเจ้าของ infrastructure, configuration storage, security และ recovery plumbing; ไม่ override business decision ของ M06
- ทุกโมดูลมี unit tests ของตนเอง; X02 รับผิดชอบ integration/replay/evidence แต่ไม่ได้แทนที่การทดสอบของเจ้าของโมดูล

---

## 3. Global requirements registry [LOCKED]

ID ต่อไปนี้เป็นชุดอ้างอิงสำหรับ PRD และ test case; รายละเอียด implementation ที่ไม่ได้ระบุถือเป็น PROPOSED/TBD ตามบริบท.

### 3.1 Trading & profit

| ID | Requirement | Phase |
|---|---|---|
| TRD-01 | XAUUSD บน MT5, fully automated เมื่อได้รับอนุญาต; ทำงานเมื่อโบรกเกอร์เปิดตลาด | MVP |
| TRD-02 | ผู้ใช้กำหนด Initial Lot; AI ประเมิน lot/จำนวน/จังหวะของไม้เพิ่มเติมภายใต้ Risk Engine; ไม่เปิดเมื่อ initial lot ไม่ปลอดภัย | MVP |
| TRD-03 | Buy/Sell และ Hedge เมื่อบัญชีรองรับ; ห้ามฝืนข้อจำกัดของ Netting | MVP |
| TRD-04 | Bot Orders Only: จัดการเฉพาะ order/position/deal ที่พิสูจน์ว่าเป็นของบอท; ผู้ใช้เทรดเองได้ | MVP |
| TRD-05 | Smart Stop Control: Start, Pause Entry, Finish Cycle, Emergency Stop; Risk Guard ทำงานต่อขณะมีไม้ค้าง | MVP |
| TRD-06 | รองรับ MT5 broker specifications แบบ dynamic และทดสอบ broker-by-broker | MVP→ADV |
| PFT-01 | ผู้ใช้กำหนด Fixed USD Profit Target ต่อรอบ; ไม่ใช้เป้ากำไรแบบ % อีก | MVP |
| PFT-02 | คำนวณ realized + floating net P&L ของ cycle เดียวกัน หลังต้นทุน; ถึง target ส่งคำสั่งปิดทุกไม้ของบอทและยกเลิก pending | MVP |
| PFT-03 | Adaptive Basket Trailing: ติดตาม peak profit, ปรับจุดเริ่ม/ระยะตามตลาด; floor ที่ activate แล้วไม่ลดลง; อาจปิดก่อน target | MVP |
| PFT-04 | Adaptive Cycle Restart: ปิด/ยืนยัน/reconcile รอบเดิมก่อน; ประเมินข่าว กราฟ และสัญญาณใหม่; WAIT ได้; ไม่ reset DD ตาม cycle | MVP |
| PFT-05 | แยก individual win rate, cycle win rate, target hit rate; ใช้กำไร realized net หลังปิดจริงสำหรับผลรอบ | MVP |

### 3.2 Risk & safety

| ID | Requirement | Phase |
|---|---|---|
| RSK-01 | Account Equity Drawdown รวมทั้งออเดอร์ผู้ใช้และบอท; เมื่อถึง limit ปิดเฉพาะไม้บอทและหยุด | MVP |
| RSK-02 | Dual DD tracking: Risk Epoch + Lifetime peak; ไม่ reset จาก cycle หรือ restart | MVP |
| RSK-03 | Adaptive Protected Limit: ผู้ใช้กำหนด max loss; ระบบกำหนด protected ceilings ภายในขอบเขตนั้น; AI ไม่มีสิทธิ์เพิ่มเอง | MVP |
| RSK-04 | Adaptive risk reduction: จำกัดการเพิ่มความเสี่ยงก่อนถึง DD limit; ปิดเมื่อถึง limit; risk veto มีอำนาจเหนือ AI | MVP |
| RSK-05 | ไม่ใช้ normal per-position SL; ใช้ Equity SL และ broker-side Emergency SL รายไม้ + independent watchdog | MVP |
| RSK-06 | Martingale ทดลองเฉพาะ Shadow/Demo; Live ห้ามใช้ Martingale หรือการเพิ่ม exposure แบบทวีคูณเพื่อไล่คืนทุน | MVP |
| RSK-07 | หลัง DD breach ต้อง lock, วิเคราะห์, ทดสอบ Shadow/Demo ใหม่, ผ่าน validation และให้ผู้ใช้อนุมัติก่อน resume | MVP |
| RSK-08 | Pre-trade check: margin, lot, spread, exposure, quote freshness, account, ownership, broker limits; fail closed เมื่อข้อมูลไม่พร้อม | MVP |
| RSK-09 | Demo / Low Risk / Normal / High Risk profiles อยู่ใต้ max loss ของผู้ใช้; Live เริ่ม Low Risk หลัง qualification และ approval | MVP→ADV |

### 3.3 Market data, AI & learning

| ID | Requirement | Phase |
|---|---|---|
| DAT-01 | H1 trend, M15 setup, M5 entry; รับ OHLC/tick/spread/volume พร้อม timestamps | MVP |
| DAT-02 | Technical indicators, price action, market regime, historical data และ feature selection | MVP |
| DAT-03 | Cross-market data เช่น DXY/yields และแหล่งข้อมูลอื่นที่เปิด/ปิดได้ | MVP→ADV |
| DAT-04 | คุณภาพข้อมูล, freshness, provenance, point-in-time replay; ไม่ใช้ข้อมูลอนาคต | MVP |
| DAT-05 | Adaptive training data selection; ผู้ใช้ไม่เปลี่ยน training windows เอง | MVP→ADV |
| LLM-01 | Configurable LLM provider/model/API key; LLM + Python AI ร่วมกัน; การตั้งอำนาจ LLM ไม่ข้าม risk veto | MVP |
| LLM-02 | Hybrid Smart Search: ข่าว/ปฏิทินเศรษฐกิจ, cache, event/adaptive schedule; ไม่เรียกทุก tick | MVP |
| LLM-03 | News sentiment/impact/confidence พร้อมแหล่งข่าว, เวลา และอายุข้อมูล | MVP |
| LLM-04 | LLM reasoning เชื่อมข่าวและกราฟ, structured output, อธิบายเหตุผล, แยกข้อมูลภายนอกจาก instruction | MVP |
| LLM-05 | News handling/configurable sources; degraded behavior เมื่อ provider/search ล้มเหลว | MVP→ADV |
| PRE-01 | XGBoost + LightGBM เริ่มก่อน; รองรับ LSTM/GRU/Transformer และ model on/off ภายหลัง | MVP→ADV |
| PRE-02 | Forecast direction, price range, entry/exit opportunities, volatility, multi-horizon; confidence และ expiry | MVP→ADV |
| PRE-03 | Adaptive ensemble; ปรับน้ำหนักจาก validation/market regime/data quality; ไม่ double count correlated inputs | MVP→ADV |
| PRE-04 | แยก chart-only และ news-enhanced forecasts; คาดการณ์ market risk ไม่ใช่ user risk ceiling | MVP→ADV |
| PRE-05 | Historical training + pretrained models เมื่อเหมาะสม + live experience; chronological walk-forward & calibration | MVP |
| DEC-01 | Adaptive hybrid: news confidence + chart confidence + LLM reasoning + algorithm final decision | MVP |
| DEC-02 | Adaptive smart entry: confidence, positive expected value หลังต้นทุน, โอกาสตลาด และ risk approval; WAIT ได้ | MVP |
| DEC-03 | Strategy enable/disable: Trend Following, Scalping, Breakout, Pullback, Range; AI เลือกจากที่อนุญาต | MVP→ADV |
| DEC-04 | Adaptive smart exit / position scaling / recovery; ไม่ต้องใช้ normal per-position SL | MVP |
| DEC-05 | Controlled Grid, Hedging, Averaging, Pyramiding, Smart Cut Loss ใช้ Live ได้เฉพาะผ่าน validation และ risk; Martingale Demo/Shadow เท่านั้น | MVP→ADV |
| DEC-06 | Adaptive Strategy Switching ระหว่างถือไม้หรือหลายกลยุทธ์พร้อมกันเมื่อเหมาะสม; ไม่ reset target/DD | MVP→ADV |
| DEC-07 | Explainable AI: input, decision trace, reasons, risk rejection, counterfactual และ post-trade analysis ที่อ้างอิงหลักฐาน | MVP→ADV |
| LRN-01 | Frozen LLM + ICL + RAG; ไม่แก้ LLM weights จากผลเทรดรายไม้ | MVP→ADV |
| LRN-02 | Short/long/episodic/semantic memory; เก็บทั้งเทรดที่เกิดขึ้นและสัญญาณที่ไม่เทรด | MVP→ADV |
| LRN-03 | Hybrid pattern matching: DTW/vector/features/regime; similarity ไม่ใช่ win probability | ADV |
| LRN-04 | Hybrid live learning: passive memory, active evaluation, adaptive weights ที่ผ่าน policy; champion/challenger | MVP→ADV |
| LRN-05 | Retrain เมื่อจำเป็น; โมเดลใหม่และกลยุทธ์ใหม่ต้องผ่าน Shadow/Demo/Validation ก่อน Live | MVP→ADV |
| LRN-06 | Model/version/feature provenance, model promotion/rollback; meta-learning เมื่อมีหลักฐานเพียงพอ | MVP→ADV |

### 3.4 UI, system & validation

| ID | Requirement | Phase |
|---|---|---|
| UI-01 | Responsive Web App; Simple Control + Advanced Insights; ไม่ต้องมี Desktop/Native mobile app | MVP |
| UI-02 | Trading view: account, equity/DD, bot positions, cycle profit/target/trailing, controls, connection status | MVP |
| UI-03 | AI Insights: ข่าว/กราฟ/confidence/prediction/strategy/เหตุผล/learning status | MVP→ADV |
| UI-04 | History: รายไม้/รายรอบ, chart, AI decision history, daily report; แจ้งเตือนภายใน Dashboard เท่านั้น | MVP |
| UI-05 | Smart Automatic Settings; Initial Lot, USD Target, max DD, account/risk profile, LLM provider/key, strategy/news/storage/deployment; ป้องกัน protected policy | MVP→ADV |
| SYS-01 | Python + MQL5; Modular/Event-driven, scalable เมื่อจำเป็น; React Web + FastAPI เป็น tech stack ที่เสนอ | MVP |
| SYS-02 | Flexible Deployment: Local, VPS, Hybrid; ระบบ execution/risk ไม่พึ่ง cloud เพื่อออกคำสั่งฉุกเฉิน | MVP→ADV |
| SYS-03 | Local/Cloud/Hybrid storage; local durable safety state และ replay/reconciliation | MVP→ADV |
| SYS-04 | Self-healing, reconnect, watchdog, safe mode, recovery/failover ตามความเสี่ยง | MVP→ADV |
| SYS-05 | Adaptive backup: critical event-driven, high 1–5 min, medium 15–60 min, low daily, model on change; restore verification | MVP→ADV |
| SYS-06 | Adaptive resource CPU/GPU/RAM local/cloud/hybrid; ไม่ให้ AI workload ทำให้ risk/execution หยุด | MVP→ADV |
| SYS-07 | Update verification, backup, rollback, signed update; ไม่ hot-update risk/execution ขณะมีสถานะเปิด | MVP→ADV |
| SYS-08 | Authentication, access control, encryption, key management, audit, prompt-injection/network security, monitoring | MVP→ADV |
| VAL-01 | Architecture first → Full AI MVP → Backtest/Shadow → Live Demo/Forward → qualification/Live | MVP→ADV |
| VAL-02 | Multi-metric validation, chronological walk-forward, benchmark, cost/slippage simulation, no leakage | MVP |
| VAL-03 | ทดสอบตลาดจริงและตลาดผันผวนบน Shadow/Demo รวม failure scenarios | MVP |
| VAL-04 | Adaptive readiness: duration, sample size, regime coverage, net performance, DD, operational stability; protected thresholds | MVP→ADV |
| VAL-05 | Live ต้อง manual approval หลังผ่านเกณฑ์, เริ่ม Low Risk, shadow parallel, reduce risk/revert Demo หากเสื่อม | ADV |
| VAL-06 | ผู้ใช้ไม่แก้ certification policy; เก็บ evidence ของผลทดสอบและประวัติ model/policy | MVP→ADV |

### 3.5 Decision traceability

| Design decision ที่ตกลง | Mapping |
|---|---|
| D1–D9: Phased Full AI MVP, hybrid training, validation, readiness, low-risk live | VAL, PRE, LRN |
| D10–D14: smart settings, adaptive entry/exit/risk/position scaling | UI-05, DEC, RSK |
| D14.1–D14.2: AI recovery, Martingale Demo/Shadow only, no normal SL + Equity SL | DEC-04–05, RSK-05–06 |
| D15–D15.1: realized+floating net cycle, separate trade/cycle/target statistics | PFT-02, PFT-05 |
| D16–D19: Equity DD, recovery lock, dual tracking, protected limits | RSK-01–04, RSK-07 |
| D20–D23: initial lot by user, emergency protection, adaptive direction and switching | TRD-02–03, RSK-05, DEC-06 |
| D24: Smart Stop | TRD-05 |
| D25: Bot Orders Only | TRD-04 |
| D26: Account Drawdown | RSK-01 |
| D27: Fixed USD target and continuous cycle | PFT-01–02, PFT-04 |
| D28–D29: Basket Trailing + Adaptive Basket Trailing | PFT-03 |
| D30: Adaptive Cycle Restart | PFT-04 |
| D31: Simple Control + Advanced Insights | UI-01–05 |
| D32: Web App / Browser | UI-01, SYS-01 |
| D33: Flexible Deployment | SYS-02 |

คำตอบล่าสุดแทนคำตอบก่อนหน้า: Web App แทน Desktop App; USD-only แทน USD+%; Initial Lot โดยผู้ใช้ แทน lot mode เดิม; Account Equity DD แทนการจำกัดความเสี่ยงแบบ daily limit เป็นค่า default; ไม่มี normal SL รายไม้ แต่มี Emergency SL. ไม่ตีความว่า AI สามารถเพิ่ม risk ceiling เพื่อแก้ไม้ได้.

---

## 4. Shared business rules — ต้องใช้ตรงกันทุก PRD

### 4.1 Trading cycle & net profit

1. เปิด cycle เมื่อเริ่มกระบวนการเทรดตามสัญญาณที่ได้รับอนุญาต; assign `cycle_id` ถาวรก่อนส่ง intent แรก.
2. รวม realized net P&L จาก deal ของ cycle เดียวกันกับ floating P&L ของสถานะของบอท และหักต้นทุนที่เกิดแล้วหรือประมาณการต้นทุนปิดที่ยังไม่เกิด โดยห้ามนับซ้ำ.
3. เมื่อกำไรสุทธิประมาณการ `cycle_net_usd >= target_usd` ให้ latch `CLOSING` หยุด entry/recovery, ยกเลิก pending ของบอท, ส่งคำสั่งปิดสถานะทั้งหมด; ไม่รอ LLM หรือแท่งเทียนปิด.
4. การปิดเป็นหลายคำสั่ง ไม่ใช่ atomic operation; ตรวจ partial fill, rejection, timeout และ unknown execution outcome; reconcile กับ MT5 ก่อน retry.
5. `CLOSED` เมื่อยืนยัน positions/pending/คำสั่งค้างของบอทครบแล้ว และปิดบัญชีกำไรจริง; ไม่มีการเปิด cycle ใหม่ขณะยังอยู่ `CLOSING`.
6. หากปิดแล้วกำไรต่ำกว่าเป้าเพราะ slippage ให้บันทึกกำไรจริงและเหตุผล ไม่เปิดไม้ใหม่เพื่อเติมกำไรใน cycle ที่ปิดแล้ว.
7. เมื่อได้รับอนุญาตให้ทำงานต่อ AI ประเมินตลาดใหม่; ไม่มีจำนวนรอบต่อวันที่บังคับ, ไม่มีการฝืนเปิดไม้; ไม่ reset DD ตามรอบ.

สูตรระดับแนวคิด:

```text
cycle_net_estimated_usd = realized_net_usd
                        + floating_net_to_date_usd
                        - estimated_remaining_close_cost_usd
cycle_final_net_usd = sum(actual attributable deal P&L + signed fees + signed swaps)
```

M06 ต้องระบุ accounting ledger ของแต่ละ cost field และ lifecycle partial close โดยใช้ข้อมูล broker/MT5 จริง. Spread ที่สะท้อนใน valuation แล้วไม่หักซ้ำ. หากบัญชีไม่ได้ denominated เป็น USD ต้องมี conversion policy ที่ทดสอบแล้ว; MVP เสนอให้เริ่มบัญชี USD และ reject สกุล/cent account ที่ยังไม่รองรับ.

ตัวอย่าง: 4 ไม้กำไรสุทธิรวม $25 และ 6 ไม้ขาดทุนสุทธิรวม $15 → net $10; target $10 → ส่งคำสั่งปิด. หาก final realized $9.80 ให้ cycle win = true, target achieved = false, close reason = TARGET_TRIGGERED; เก็บ target-trigger และ target-achieved แยกกัน. Individual win rate ในตัวอย่าง = 40%.

**[PROPOSED]** snapshot target และ config version ต่อ cycle; การเปลี่ยน target/initial lot มีผลรอบถัดไปเพื่อลดความสับสน (risk limit ที่เข้มขึ้นต้องประเมินทันที); ให้ M06/M08/M01 ระบุ policy และ review ร่วมกัน. ห้ามปรับ target ขึ้นเองด้วย AI.

### 4.2 Adaptive Basket Trailing

- ติดตาม `peak_cycle_net_usd` และ `trailing_floor_usd` เฉพาะ cycle ปัจจุบัน; เก็บ state ถาวรและกู้คืนได้.
- ก่อน activate ให้ AI/algorithm เลือก activation threshold และ trailing distance ตาม volatility, market quality, cost, target และข้อมูลที่ผ่าน validation.
- หลัง activate floor ใหม่ต้องไม่น้อยกว่า floor เดิม; ไม่ปิดหรือเลื่อน floor ลงเพื่อเลี่ยงการปิด.
- เมื่อ `cycle_net <= trailing_floor` ให้ latch CLOSING; ถ้า target ถึงก่อนให้สั่งปิดทันที.
- ปิดก่อน target ได้; แยก close reason, realized win/loss และ target hit; ไม่สัญญาว่ากำไรจริงเท่ากับ floor.
- **[TBD]** activation/distance bounds, fallback เมื่อ AI ไม่พร้อม และ update cadence ต้องอยู่ใน protected policy ที่ M06/M05/X02 ทดสอบ; ขาด AI แล้วต้องไม่สูญเสีย floor เดิม.

### 4.3 Account DD & protected limits

```text
DD% = max(0, (peak_equity - current_equity) / peak_equity * 100)
```

สูตรนี้ใช้กับฐาน equity ที่ปรับ cash flow อย่างสอดคล้องแล้ว และ peak > 0. คำนวณจากทั้งบัญชี MT5 ไม่ใช่เฉพาะ bot P&L; M06 ต้องกำหนดการฝากถอน/เครดิต/fee/currency ให้ชัดก่อน qualification. ตัวอย่างเมื่อ peak $120 และ equity $105 (ไม่มี cash flow) DD = 12.5%.

- เก็บ `epoch_peak`, `lifetime_peak`, current equity, cash flow reference, risk policy version, risk lock และเหตุการณ์ breach ถาวร.
- User max DD เป็นเพดานที่ AI ไม่เพิ่มเอง; protected epoch/lifetime thresholds ต้องอยู่ภายในกรอบที่ได้รับอนุมัติ; ใช้ข้อจำกัดที่เข้มกว่าเมื่อหลายข้อมีผล.
- DD breach จาก manual trade ก็ trigger lock ได้; บอทปิดเฉพาะ bot positions/pending ไม่จัดการ manual orders แม้ DD ยังเพิ่มต่อ.
- เมื่อ DD breach ให้หยุดเพิ่มความเสี่ยง, latch lock, ปิดไม้ที่เป็นของบอท, แจ้งเหตุใน Dashboard, ทำ recovery analysis. ห้าม auto-resume เพราะ equity ฟื้นหรือ restart.
- Recovery หลัง DD: shadow/demo validation ใหม่และ user approval ก่อน epoch ใหม่; lifetime tracking ไม่ reset. หาก lifetime limit ยัง breach ห้าม resume เพียงเพราะ epoch ใหม่.
- ไม่มีการตั้ง daily loss limit เป็น default แยกเพิ่มเอง; protected exposure/margin checks ไม่ใช่ daily DD ใหม่.
- **[TBD]** สูตร cash-flow adjusted peak, epoch/lifetime limit relationship, lifetime lock remediation, handling equity <= 0, account changes และการเปลี่ยน max DD ต้องมี risk policy และ review; ก่อน Live ห้ามใช้ค่าเดาสุ่ม.

### 4.4 Ownership & execution boundary

MT5 เป็น source of truth สำหรับ account, position, order, deal และ broker execution. Local database เป็น durable ledger ของ intent/state/history; reconcile ก่อนเชื่อว่า state ถูกต้อง.

- ใช้ Magic Number + account/server + symbol + order/deal/position identifiers และ mapping ที่ persist เพื่อพิสูจน์ ownership; Magic Number เพียงอย่างเดียวไม่เพียงพอในทุกกรณี.
- Hedge account รองรับแยกไม้; Netting รวมสถานะต่อ symbol ทำให้ manual/bot XAUUSD ปะปนได้. MVP ต้อง block กรณี ownership ไม่ชัด; หากต้องใช้ XAUUSD ร่วมกับ manual ให้ใช้ Hedging.
- หากผู้ใช้แก้หรือปิดสถานะของบอทด้วยมือ ให้ reconcile และบันทึกผลตาม deal จริง; ห้ามปิดไม้ใหม่หรือ adopt manual trade โดยอัตโนมัติ.
- Lot rounding, min/max/step, contract size, tick size/value, stop/freeze levels, trading session, order/filling mode และ margin ต้องอ้างอิง symbol/account specifications จริง ไม่ hardcode.
- `trade_intent` จาก M05 เป็นข้อเสนอ; M06 ตรวจ policy และ execution state ใหม่ทันทีที่ส่งคำสั่ง; Python อ่าน MT5 ได้ แต่ไม่ส่งคำสั่งซื้อขายซ้ำกับ EA.
- Emergency SL ฝั่ง broker ต้อง attach และ verify; หาก attach ไม่ได้ให้ block entry หรือจัดการปิดสถานะที่เกิดขึ้นแล้วอย่างปลอดภัยตาม policy. ไม่ขยาย SL เพื่อไล่คืนทุน.
- Gap, slippage, market closed, disconnect และ broker restrictions อาจทำให้คำสั่งปิดล่าช้า/ขาดทุนเกิน DD. ห้ามแสดงข้อความรับประกัน exact stop หรือ profit.

### 4.5 Control commands

| Command | Behavior | Non-negotiable |
|---|---|---|
| Start | ตรวจ startup/reconciliation/account/mode/risk/readiness ก่อนเริ่ม | ไม่ bypass risk lock หรือ Live gate |
| Pause | ไม่เปิดไม้ใหม่รวมถึง recovery entry; ดูแลไม้เดิม/ปิดความเสี่ยงได้ | risk/profit/SL ยังทำงาน; จัดการ pending ที่จะเพิ่ม exposure |
| Finish Cycle | ทำรอบเดิมให้จบภายใต้ risk policy แล้วหยุด | ไม่เปิด cycle ใหม่; DD stop มี priority |
| Emergency Stop | latch stop; cancel pending; ปิด bot positions; reconcile | ไม่แตะ manual; ถ้าปิดไม่ได้แสดง pending/unresolved และ retry ตาม policy |
| Approve Recovery | อนุมัติหลังผ่าน recovery gate | ไม่สามารถ override lifetime breach หรือ failed validation |

**Priority:** emergency/DD/risk safety > close-in-progress > profit exit/trailing > control intent ที่จำกัดการเทรด > entry/recovery intent. การจำกัด entry ไม่ขัดขวางการลดความเสี่ยง. M06/X01 ต้องทำ transition/priority matrix รายกรณี โดยเมื่อหลายเหตุการณ์เกิดพร้อมกันต้องบันทึกเหตุผลทั้งหมดและมี primary reason ที่กำหนดแน่นอน.

### 4.6 AI autonomy boundaries

- AI วิเคราะห์และเสนอกลยุทธ์/lot/entry/exit/recovery/WAIT; ไม่มีสิทธิ์เพิ่ม max DD, เปลี่ยน target, activate live, ปิด manual positions หรือส่งคำสั่ง broker โดยตรง.
- ใช้ recovery เพื่อบริหารความเสี่ยงและโอกาส ไม่บังคับคืนทุน; controlled grid/averaging/hedge/pyramiding ใช้ Live เฉพาะผ่าน validation; Martingale Shadow/Demo เท่านั้น.
- LLM score ไม่ใช่ calibrated probability โดยอัตโนมัติ; model confidence ต้องระบุ target, horizon, method, calibration state และ expiry.
- ข่าวและเว็บเป็น untrusted data; ไม่ให้ content สั่งเปลี่ยน config หรือใช้ tools ที่ทำ trade; structured output ต้อง validate schema และ provenance.
- สัญญาณเก่า, provider failure, missing mandatory data หรือ invalid model → block new exposure ตาม policy; exits และ risk guard ยังทำงาน.

---

## 5. System architecture & technology

### 5.1 Logical architecture

```mermaid
flowchart TD
    UI[M01 Web Dashboard] -->|Authenticated control| CORE[M08 API and System Core]
    CORE -->|Control command| RISK[M06 Risk and Execution]
    MT5[MT5 Terminal and Broker] -->|Market and account data| DATA[M02 Market Data]
    DATA --> LLM[M03 News and LLM]
    DATA --> PRED[M04 Prediction]
    LLM --> PRED
    DATA --> DEC[M05 Adaptive Decision]
    LLM --> DEC
    PRED --> DEC
    DEC -->|Trade intent only| RISK
    RISK -->|Approved execution| EA[MQL5 Execution EA and Risk Guard]
    EA <-->|Orders and transactions| MT5
    RISK -->|Execution outcomes| MEM[M07 Learning and Memory]
    DEC --> MEM
    PRED --> MEM
    MEM -->|Approved knowledge and models| PRED
    MEM --> DEC
    DATA --> CORE
    LLM --> CORE
    PRED --> CORE
    DEC --> CORE
    RISK --> CORE
    MEM --> CORE
    CORE -->|Read models and status| UI
```

MQL5 guard ต้องมีข้อมูล risk configuration/state ที่จำเป็นและทำ emergency handling ได้เมื่อ Python/LLM ขัดข้องตราบใดที่ Terminal และการเชื่อมต่อ broker ยังใช้ได้. Broker-side Emergency SL เป็นชั้นสำรองเมื่อ Terminal ใช้งานไม่ได้. ไม่ถือว่า Web UI เป็นส่วน safety-critical ที่ต้องเปิดค้าง.

### 5.2 Technology baseline [PROPOSED]

| Layer | Baseline | Notes |
|---|---|---|
| Frontend | React, TypeScript, Tailwind CSS, Lightweight Charts | Responsive single-user dashboard |
| API | Python, FastAPI, REST, WebSocket | auth and schema validation |
| Execution | MT5, MQL5 EA, MetaTrader5 Python API for read/integration | single trade writer through EA |
| Data | NumPy, Pandas, Parquet | point-in-time data, feature schema |
| ML | XGBoost, LightGBM, scikit-learn | MVP; calibration and validation |
| Advanced ML | PyTorch (LSTM/GRU/Transformer) | ADV, if validated |
| Memory | SQLite + local vector index / FAISS candidate | phase advanced similarity search |
| Storage | SQLite + SQLAlchemy, migrations, Parquet | local durable state; cloud adapter later |
| Processing | AsyncIO, dedicated workers/processes, durable event records | risk/execution priority independent of ML |
| Test | Pytest, Python historical replay, MT5 Strategy Tester, frontend test tooling | verify terminal execution separately |
| Deployment | Windows local / Windows VPS / Hybrid | pinned dependencies, reproducible setup |
| Version control | Git | no secrets or live data in repo |

Library versions, EA/Python bridge protocol, DB migration tool, vector index, chart license and dependencies must be verified in implementation. Do not assume latest versions or runtime compatibility.

### 5.3 Deployment profiles [LOCKED capability; PROPOSED order]

| Profile | Placement | Requirement |
|---|---|---|
| Local | Browser + Python + MT5 on Windows machine | first development/demo; bind localhost by default |
| VPS | MT5 + risk/execution + API on Windows VPS | remote access via authenticated HTTPS/VPN; no public unsecured API |
| Hybrid | risk/execution/MT5 local or VPS; selected AI compute/LLM/storage cloud | disconnect cloud must not block emergency exit |

Storage choices Local/Cloud/Hybrid are distinct from deployment profile. For Cloud storage mode, safety-critical execution state still needs recoverable local durable cache/journal while online sync is unavailable; this is a proposed implementation constraint, not a new product feature. One active executor per account/symbol/strategy namespace; moving hosts must reconcile and avoid split brain before reactivation.

---

## 6. Module-level PRD briefs

แต่ละส่วนต่อไปนี้เป็น assignment brief ไม่ใช่ PRD ที่เสร็จแล้ว เจ้าของต้องแตก user stories, functional requirements, data contracts, test cases และ acceptance criteria เพิ่มเติมตาม §12 โดยไม่เพิ่ม product scope.

### M01 — Web Dashboard

**Goal:** ให้ผู้ใช้ควบคุมและตรวจสอบบอทผ่าน Browser ได้ง่าย โดยแสดงข้อมูลที่ยืนยันจาก backend.

**Scope / MVP:** Trading (Balance/Equity/DD/margin, bot positions, target/trailing, cycle & controls); AI Insights (ข่าว, forecast, confidence, reasons, strategy); History (deal/cycle/metrics/reports); Settings (Lot, USD target, DD, account/mode, profile, LLM, strategy/news, deployment/storage ตาม capability ที่พร้อม); dashboard-only notification; auth integration.

**ADV:** detailed learning/model diagnostics, advanced chart/pattern visualization และ history search ตามข้อมูลที่ backend รองรับ.

**Inputs:** M08 read API/WS, M06 account/bot/cycle/risk snapshots and command outcomes, M03–M05 predictions/decisions, M07 learning status. **Outputs:** authenticated control/config requests; ไม่ส่ง broker orders.

**PRD must specify:** route/page wireframes, UI states (loading/empty/stale/disconnect/permission/closing/risk-lock), command confirmation and status, validation, account/mode display, stale timestamp, accessibility and responsive layout, secret field masking.

**Acceptance:** browser close ไม่หยุดบอท; reconnect แสดง snapshot ที่สอดคล้อง; stale status ไม่ถูกแสดงว่า live; emergency command ไม่แสดงว่าปิดสำเร็จจน reconcile; DD ทั้งบัญชีแยกจาก bot cycle profit; UI ไม่เปิด Live ด้วยการ toggle อย่างเดียว.

**Non-goals:** native app, multi-user, independent trading calculator for live decisions.

### M02 — Market Data Engine

**Goal:** สร้าง market data ที่มีคุณภาพและใช้ร่วมกันระหว่าง forecast, decision, backtest และ learning.

**Scope / MVP:** XAUUSD broker symbol mapping, H1/M15/M5 OHLC, ticks/spread/volume; closed candle event; indicators/price action/regime features; historical storage; validation of gaps/duplicates/order/freshness; UTC normalization with broker-time metadata; point-in-time dataset builder.

**ADV:** cross-market feeds, broader feature selection, adaptive data sources and data-window policies ร่วมกับ M04/M07.

**Inputs:** MT5 read adapter, historical datasets, optional DXY/yields sources; **Outputs:** `market.snapshot`, `candle.closed`, `feature.snapshot`, quality/freshness events, versioned datasets.

**PRD must specify:** source license/availability, timestamp semantics, symbol mapping, warm-up lengths, bar finality, missing values, feature schema, storage partitions, dataset version and availability time, offline replay.

**Acceptance:** identical event replay gives consistent features within numeric tolerance; no future data; missing/stale mandatory feature triggers quality flag; symbol/quote mismatch blocked; test DST/server-time conversions and out-of-order ticks.

### M03 — LLM & News Engine

**Goal:** วิเคราะห์ข่าวและผลกระทบต่อ XAUUSD พร้อมหลักฐาน ให้ reasoning ประกอบผลกราฟโดยไม่ควบคุม execution.

**Scope / MVP:** configurable provider adapter, economic calendar/news retrieval with cache/dedup, adaptive search, news sentiment/impact/confidence, source IDs/URLs/timestamps, structured reasoning result, schema validation, timeout/retry/degraded modes, prompt-injection defense.

**ADV:** provider failover, advanced search/data-source weighting, improved context retrieval and calibrated impact estimates.

**Inputs:** news/calendar, M02 data summary, M04 forecast for reasoning stage, M07 approved context. **Outputs:** `news.assessment`, `llm.reasoning`, health/quality status; source provenance.

**PRD must specify:** provider interface, allowed tools, prompt/schema/version, search frequency/events/cache/expiry, timeouts, data retention/redaction, conflicting news, untrusted-content policy, fallback and score interpretation. Separate news ingestion/assessment from downstream reasoning to avoid runtime dependency cycles with M04.

**Acceptance:** malformed/injected/expired results rejected; evidence-linked analysis available; unavailable LLM cannot disable risk engine; no trade-capable tools exposed; duplicate news not treated as independent evidence; no claims of news access without an available source.

### M04 — Prediction Engine

**Goal:** สร้าง probabilistic forecast ที่ใช้ประเมินโอกาสตลาดและความเสี่ยงตลาด ไม่ใช่กำหนด user risk ceiling.

**Scope / MVP:** chart-only and news-enhanced feature sets where data supports, XGBoost/LightGBM, direction/price range/volatility forecasts with horizons, opportunity estimates, calibration, expiry, adaptive ensemble, versioned model artifacts and deterministic inference where feasible.

**ADV:** LSTM/GRU/Transformer, richer multi-horizon, adaptive model selection and meta-learning in collaboration with M07.

**Inputs:** M02 features, M03 news assessment, M07 approved model artifacts. **Outputs:** `prediction.generated` with model version, horizon, direction, probability/score semantics, uncertainty, data quality, validity and explanation features.

**PRD must specify:** target labels, dataset/windows, splits/purge/embargo when labels overlap, baseline, cost-aware evaluation, calibration, missing features, out-of-distribution detection, model versioning, inference latency budget, offline/online parity.

**Acceptance:** held-out/walk-forward evidence; no leakage; chart-only path test; stale or unapproved artifact rejected; probability semantics documented; comparison against baseline, no invented accuracy claims.

### M05 — Adaptive Decision Engine

**Goal:** รวมข่าว กราฟ LLM และโมเดลเป็นการตัดสินใจที่อธิบายได้และผ่าน risk constraints.

**Scope / MVP:** strategy registry and enable flags, signal fusion, risk-aware intent generation, entry/exit/WAIT, adaptive restart, position scaling/recovery proposals, bounded strategy switching, reason codes and decision trace; initial strategies implemented in order of validated readiness.

**ADV:** richer strategy combinations, advanced recovery strategies, meta-learning driven weights, counterfactual diagnostics.

**Inputs:** M02–M04 signals, M06 read-only risk/positions/cycle state, M07 memory/approved policy. **Outputs:** `decision.created`, `trade.intent` (not broker command), trailing/recovery proposals; evidence and confidence.

**PRD must specify:** strategy contracts, eligibility and risk preconditions, expected value after costs, signal disagreement, freshness checks, adaptive weights without double-counting, cooldown/restart logic, opportunity vs forced recovery, allowed actions by operating mode, event idempotency, fallback/WAIT.

**Acceptance:** negative EV/stale/no-opportunity → no entry; disabled strategy not selected; no Martingale in Live; risk rejection respected; no reset of cycle/DD on strategy change; rationale reproducible from stored inputs; no access to broker order API.

### M06 — Risk & Execution Engine (critical path)

**Goal:** เป็นผู้อนุมัติ/ปฏิเสธและส่งคำสั่งเทรดเพียงทางเดียว พร้อมบังคับทุกกฎใน §4 แม้ AI ไม่พร้อม.

**Sub-workstreams:** (a) MT5 adapter/EA & order ownership, (b) risk policy/DD/emergency guard, (c) cycle net P&L/profit trailing/control state. หากแบ่งผู้รับผิดชอบให้เขียน PRD ย่อยภายใต้ parent PRD เดียว.

**Scope / MVP:** read account/symbol specs; validated intents; request ID and durable ledger; MQL5 execution; partial fills/rejections/reconcile; bot-only ownership; fixed USD target; adaptive trailing; dual DD; risk lock & approval; risk checks; watchdog; emergency SL; start/pause/finish/emergency; risk profiles; history accounting.

**ADV:** broker compatibility expansion, advanced adaptive exposure policy and recovery/failover hardening; core risk safety ห้ามเลื่อนไป ADV.

**Inputs:** M05 intent/proposal; M08 control/config, MT5 account/order/deal/tick data. **Outputs:** risk decisions, execution status, account/risk/cycle snapshots, close reason, realized ledger, events to M07/M08/M01.

**PRD must specify:** exact state machines and concurrency lock, close workflow, authoritative accounting, cash-flow-adjusted DD, protected limit policy, emergency SL placement, stale state handling, duplicate prevention, unknown outcomes, pause/pending behavior, broker sessions/retry, startup/restart, ownership verification, mode isolation and recovery approval.

**Acceptance:** target triggers bot-only close; manual positions unchanged; DD breach latches across restart; no new exposure after closing/lock; partial-close/timeout does not duplicate orders; emergency SL verified; loss of Python/LLM does not disable EA safety when terminal connected; min lot unsafe -> reject; netting ambiguous -> block. Test each with actual MT5 Demo evidence before Live gate.

### M07 — Learning & Memory Engine

**Goal:** ให้ AI ใช้ประสบการณ์ย้อนหลังและพัฒนาอย่างควบคุมได้ โดยไม่เปลี่ยนโมเดลหลักหรือ risk policy จากผลเทรดรายไม้โดยอัตโนมัติ.

**Scope / MVP:** experience logging (traded + skipped), short/long/episodic/semantic memory baseline, RAG context, offline training/evaluation hooks, model registry, candidate/champion lifecycle, approval evidence, rollback and reproducibility.

**ADV:** DTW + vector + feature + regime pattern matching, deep models, meta-learning, richer live adaptation, parallel challenger shadow.

**Inputs:** market/features, news, forecasts, decisions including WAIT, actual deals/cycles/risk events; **Outputs:** retrieval context with IDs/similarity, training dataset/version, candidate models, approved model references, validation metrics and learning reports.

**PRD must specify:** memory schemas/retention, labeling untraded signals without fabricating fills, temporal retrieval, similarity semantics, retrain trigger, dataset isolation, drift metrics, champion/challenger, model promotion/rollback policy, secure artifact storage.

**Acceptance:** historical retrieval cannot leak future outcomes into replay; similarity not shown as win probability; unapproved model cannot be activated Live; model rollback test; learning events attributable to source data and model/policy version.

### M08 — System Core

**Goal:** จัดหา infrastructure ที่เรียบง่าย ปลอดภัย และรองรับ Local/VPS/Hybrid ให้ทุกโมดูลใช้ contract ร่วมกัน.

**Scope / MVP:** FastAPI/WS, authentication, configuration/version/secret management, durable event/state storage and migration, service lifecycle, heartbeat, structured logs, monitoring, local backup/restore, reconciliation hooks, process separation, Windows local install and docs.

**ADV:** VPS/Hybrid packaging hardening, cloud sync/storage, adaptive resource orchestration, signed update/rollback, advanced backup/failover. การย้ายไป VPS เพื่อทดสอบทำได้เมื่อมี minimum security และ operational checks.

**Inputs:** module events, UI commands, config; **Outputs:** durable event transport, API/read models, health/status, configuration snapshots, backup/restore and deployment tooling.

**PRD must specify:** process topology, API auth/session/CSRF/CORS, secrets handling, storage single-writer/transactions, replay/idempotency, health and safe mode, resource priorities, retention and recovery objectives, install/upgrade/rollback, sync conflict handling, single active executor.

**Acceptance:** local start/stop documented; unauthorized access denied; secrets redacted; restore verified; DB/worker crash leads to safe mode/reconcile; UI disconnect harmless; cloud loss does not block local safety; two hosts cannot both trade same namespace; risk-critical updates blocked with positions open.

### X01 — Architecture & Integration

**Goal:** ทำให้ PRD แต่ละส่วนรวมเป็นระบบเดียว โดยไม่ให้เกิด conflicting interfaces หรือสองแหล่งตัดสิน risk/execution.

- INT-01: shared schemas, event/topic names, API/error contracts, units/time, versioning and ownership.
- INT-02: dependency graph, test doubles, integration harness, decision log and review coordination.
- INT-03: system state transition/priority matrix, idempotency/reconciliation rules, process failure modes.
- INT-04: maintain traceability across requirements, PRDs, tests and release gates; manage change requests.

**Deliverable:** approved contract files and cross-module integration test matrix; review PRDs against invariants before parallel implementation. No extra trading features.

### X02 — Validation & QA

**Goal:** ประเมิน functional correctness, model quality, financial risk และ execution reliability ด้วยหลักฐานที่ทำซ้ำได้.

**Scope:** test strategy, fixtures/replay, broker demo scenarios, walk-forward and metrics, release/readiness gates, bug triage, test evidence, regression and incident recovery drills.

**Acceptance:** safety-critical tests passed with evidence; chronological splits and cost assumptions recorded; all TBD thresholds required for release resolved and protected; no live approval until qualified and explicitly approved by user. QA does not substitute for M06 risk authority.

---

## 7. Repository & data architecture [PROPOSED]

### 7.1 Proposed repository layout

```text
xauusd-ai-trading-bot/
├── master_planV1.md
├── README.md
├── .env.example
├── .gitignore
├── config.example.yaml
├── pyproject.toml
├── docs/
│   ├── prd/                    # PRDs from §2
│   ├── contracts/              # versioned cross-module schemas
│   ├── adr/                    # Architecture Decision Records
│   ├── validation/            # policies and evidence summaries
│   └── runbooks/               # install, operations, recovery
├── frontend/                   # M01
│   └── src/{pages,components,charts,services,stores}/
├── backend/
│   └── app/
│       ├── api/                # M08
│       ├── core/               # M08, shared contracts from X01
│       ├── market_data/        # M02
│       ├── llm/                # M03
│       ├── prediction/         # M04
│       ├── decision/           # M05
│       ├── strategies/         # M05
│       ├── risk/               # M06
│       ├── execution/          # M06
│       ├── profit/             # M06
│       ├── learning/           # M07
│       ├── memory/             # M07
│       └── storage/            # M08
├── mt5/                        # M06
│   ├── TradingExecutor.mq5
│   ├── RiskGuard.mqh
│   ├── OrderManager.mqh
│   ├── Bridge.mqh
│   └── Watchdog.mqh
├── migrations/                 # M08 with table owners
├── models/{champion,challenger,metadata}/
├── data/{market,news,features,memory,backtests}/
├── tests/{unit,integration,contract,backtest,simulation,forward}/
└── deployment/{local,vps,hybrid}/
```

Directory names are suggested contracts; do not create empty production services just to match folders. `models/` and `data/` contain runtime artifacts, normally gitignored except safe fixtures, schemas and manifests. No real account credentials or unrestricted market/news data in Git.

### 7.2 Conceptual data ownership

| Table / dataset | Logical key and important fields | Authoritative owner |
|---|---|---|
| accounts | account_id; server, currency, type, mode | M06/M08 |
| settings | config_version; approved settings, effective time | M08 |
| bot_state | bot_id, account_id; lifecycle, lock, current cycle, version | M06 |
| trade_cycles | cycle_id; account, target snapshot, net/peak/floor, status, close reason | M06 |
| orders | account + order_ticket; request_id, magic, cycle_id, status | M06 |
| positions | account + position_identifier; owner, symbol, exposure, cycle | M06 |
| deals | account + deal_ticket; order/position link, pnl, fees, swap, time | M06 |
| equity_snapshots | account + observed_at; balance, equity, margin, currency | M06 |
| drawdown_history | epoch_id + observed_at; peak, cash-flow baseline, policy, DD | M06 |
| risk_events | risk_event_id; decision/intent, verdict, reason codes, policy | M06 |
| execution_events | execution_event_id; request_id, broker refs, status, error | M06 |
| market_metadata / parquet | dataset_version, symbol/timeframe/time, source, schema | M02 |
| news_events | news_id; source, available_at, impact, validity, provenance | M03 |
| predictions | prediction_id; horizon, input refs, version, forecast, validity | M04 |
| ai_decisions | decision_id; inputs, strategy, action, reasons, policy | M05 |
| learning_experiences | experience_id; decision/outcome refs, label availability | M07 |
| pattern_memory | memory_id; feature/regime/vector/version/outcome refs | M07 |
| model_registry | model_id + version; dataset, artifact hash, status | M07/M04 |
| validation_results | validation_id; run, metrics, split, evidence, policy | X02/M07 |
| system_logs / event journal | event_id; correlation, timestamp, severity, payload ref | M08 |
| daily_reports | report_id; period, account, bot metrics, cutoff/source | M08/M06 |

M08 owns migrations and storage implementation; table owner owns semantic fields and writes through approved repository contract. Read models are derived and must not override authoritative state. PRDs must specify primary/foreign keys, unique constraints, money precision, indexes, retention and transactional boundaries. The table is conceptual, not executable SQL.

---

## 8. Cross-module contracts [PROPOSED baseline; X01 must review]

### 8.1 Common event envelope

```json
{
  "schema_version": "1.0",
  "event_id": "unique-event-id",
  "event_type": "trade.intent",
  "occurred_at": "2026-01-01T00:00:00Z",
  "recorded_at": "2026-01-01T00:00:00Z",
  "source": "M05",
  "correlation_id": "trace-id",
  "causation_id": "prior-event-id-or-null",
  "account_id": "demo-account-alias",
  "bot_id": "bot-instance-id",
  "cycle_id": "cycle-id-or-null",
  "mode": "DEMO",
  "payload": {}
}
```

Example only. UTC timestamps with broker time metadata where needed. `available_at` is required for market/news features used by AI. Units: USD for target/net cycle, account currency explicitly labeled, DD percent as 0–100, lot as broker lot, price in quote currency, confidence/probability 0–1 internally with semantics; UI may render percent. Use Decimal/fixed-point or explicit precision/rounding for money, lots and thresholds. Define nullable fields and stale-data semantics; do not substitute zero for unknown. IDs immutable; mode/account isolation enforced at execution.

### 8.2 Event catalog and ownership

| Event | Producer → Consumer | Required payload concept |
|---|---|---|
| `market.snapshot` | M02 → M03/04/05/08 | symbol, quote, tick/bar refs, timestamp, quality |
| `candle.closed` | M02 → M04/05 | timeframe, final OHLC, available_at |
| `feature.snapshot` | M02 → M04/05/07 | feature_schema, values, quality, expiry |
| `news.assessment` | M03 → M04/05/07/08 | source refs, direction/impact/score, available_at, expiry |
| `prediction.generated` | M04 → M03/05/07/08 | prediction_id, model/version, horizon, forecast, confidence, valid_until |
| `llm.reasoning` | M03 → M05/07/08 | referenced news/prediction, structured rationale, score semantics, validity |
| `decision.created` | M05 → M06/07/08 | decision_id, action, strategy, EV, reason codes, evidence refs |
| `trade.intent` | M05 → M06 | intent_id, action, side, requested_lot, position refs, strategy, expiry, decision_id |
| `risk.decision` | M06 → M05/07/08 | intent_id, allow/deny, approved bounds, policy version, reasons |
| `execution.updated` | M06 → M05/07/08 | request_id, state, broker identifiers, actual fill, error |
| `account.snapshot` | M06 → M05/08 | equity, balance, margin, quote/connection status, account currency |
| `cycle.updated` | M06 → M05/07/08 | cycle state, net estimate, target, peak/floor, actual net if closed |
| `risk.updated` | M06 → M05/08 | DD values, ceilings, lock, reasons, policy version |
| `control.requested` | M08 → M06 | command_id, command, actor, expected state/config version |
| `control.completed` | M06 → M08 | command_id, status, resulting state, unresolved details |
| `model.candidate` | M07 → X02/M04 | model/version, dataset refs, artifact hash, evaluation |
| `model.approved` | M07 → M04/05/08 | approval/evidence/policy refs, allowed modes, version |
| `health.updated` | each module → M08/M06 where relevant | health, last heartbeat, degradation/error |

Rules: delivery can be at-least-once; consumers deduplicate by event/request ID. No assume global ordering; define per-account/bot sequence or state version where required. Risk/execution control cannot rely solely on ephemeral pub-sub. Use durable outbox/inbox or equivalent with recovery semantics. Separate high-priority safety lane from data/ML workload. X01 to settle exact schemas and transport before implementation.

### 8.3 Trade intent example (not executable broker request)

```json
{
  "intent_id": "intent-001",
  "decision_id": "decision-001",
  "action": "OPEN",
  "side": "BUY",
  "symbol": "XAUUSD",
  "requested_lot": "0.01",
  "strategy_id": "trend_following",
  "cycle_id": "cycle-001",
  "mode": "DEMO",
  "expected_value_usd": "0.20",
  "prediction_ids": ["prediction-001"],
  "valid_until": "2026-01-01T00:01:00Z"
}
```

All values are illustrative, not a trade recommendation or default settings. M06 checks latest account/symbol/cycle, policy, ownership, mode and freshness before any approval; intent cannot guarantee fill or permission. Execution command ID must be persisted before dispatch; unknown outcomes require broker reconciliation before new command. Do not carry a risk approval indefinitely.

### 8.4 API contract outline

| Method | Path | Owner / semantics |
|---|---|---|
| GET | `/api/status`, `/api/account`, `/api/positions` | M08 facade over M06 authoritative snapshots |
| GET | `/api/cycles`, `/api/reports` | cycle and realized history; pagination/date filters |
| GET | `/api/predictions`, `/api/decisions` | AI outputs with provenance and validity |
| GET | `/api/settings` | redacted current config/version |
| PATCH | `/api/settings` | auth, validation, version conflict, effective cycle/policy |
| POST | `/api/bot/start` | accepted/rejected with command_id; not promise execution |
| POST | `/api/bot/pause` | pause entry request |
| POST | `/api/bot/finish-cycle` | finish-current-cycle request |
| POST | `/api/bot/emergency-stop` | stop latch and close workflow |
| POST | `/api/bot/approve-recovery` | gate and approval check |
| WS | `/ws/live` | authenticated snapshots/events; sequence and resync |

**TBD:** explicit Live approval endpoint, command status lookup, auth protocol, API version and schema/error codes. M08/M06/X01 must specify them without giving UI a path to bypass gates. HTTP accepted means command queued/recorded, not a confirmed fill or completed close. UI refresh/retry must not generate duplicate execution. REST/WS returns as-of timestamps and data quality flags.

### 8.5 Configuration example [PROPOSED]

```yaml
# Example only; not recommended trading parameters.
# Secrets are injected from secure local secret storage / environment.
trading:
  symbol: XAUUSD
  initial_lot: null              # required explicit user input
  profit_target_usd: null       # required explicit user input
  max_drawdown_percent: null    # required explicit user input
  order_scope: BOT_ONLY
  drawdown_scope: ACCOUNT_EQUITY
  account_mode: DEMO
  risk_profile: LOW
  strategies_enabled: []       # configure validated strategies before run
llm:
  provider: null
  model: null
  api_key_ref: null
  news_handling: adaptive
system:
  deployment: local
  storage: local
  live_allowed: false
```

No ready-to-trade default. User config and protected policy are distinct: strategy toggles, LLM settings and target are configurable; model certification, risk-policy safety bounds and validation evidence are not arbitrary UI inputs. Default startup must not authorize live execution. A non-null key must be a secret reference, never plaintext in a checked-in config.

---

## 9. State, scheduling and non-functional requirements

### 9.1 State machine [PROPOSED baseline]

Keep account operating mode (SHADOW/DEMO/LIVE), control lifecycle, and cycle lifecycle distinct; do not use a single boolean `running` to represent all conditions.

| Control lifecycle | Meaning / allowed behavior |
|---|---|
| STOPPED | no entry; should have no unresolved owned exposure for a clean stop |
| STARTING / RECONCILING | read broker state, load config and locks; no new exposure |
| RUNNING | allow eligible intents only after risk checks |
| PAUSED | no new exposure; manage exits/protection |
| FINISHING | manage current cycle within risk limits; no new cycle |
| CLOSING | latch no entry, cancel pending, reconcile all exits |
| RISK_LOCKED | persistent DD breach; no entry; exit work may still be pending |
| SAFE_MODE | uncertain data/health/ownership; no new exposure; permitted safety handling |

Cycle states: `IDLE → ACTIVE → CLOSING → CLOSED`, with unresolved state tracked explicitly. A risk lock can coexist with a closing cycle; represent as separate persisted flags/state dimensions. Do not label stopped/closed if broker confirmation is absent. Recovery requires reconciliation; returning from SAFE_MODE must not clear risk lock or pending emergency command. X01/M06 own full transition table and test matrix.

### 9.2 Scheduling

| Task | Trigger / rule |
|---|---|
| Equity DD, emergency guard, profit target, trailing check | Tick + timer; independent from LLM/ML inference |
| M5/M15/H1 forecasts | closed candles; material events if defined by policy |
| News search | adaptive timer + economic events; cache, dedup and TTL |
| LLM reasoning | material change in verified data/signals |
| Adaptive decision | fresh forecast/news/control updates; avoid repeated intents on same event |
| Memory | decision/outcome/event recorded; no fabricated results |
| Retraining | drift/need + sufficient data + validation pipeline |
| Reports | daily scheduled period with explicit timezone/cutoff |

### 9.3 Non-functional requirements

- Safety: new exposure is fail-closed on stale/unknown state; valid exits remain prioritized; never block independent risk guard on LLM response.
- Reliability: restart/reconnect recovery, event-driven durable critical state, duplicate prevention, broker reconciliation and explicit unresolved outcomes.
- Performance: separate workloads; define measurable tick-to-guard, decision-to-intent, approved-to-dispatch, UI refresh, inference and restore budgets in PRDs. Numeric limits TBD from actual hardware/broker measurements; not hardcoded promises.
- Security: least privilege, local bind by default, authenticated remote access, encrypted transport and sensitive storage, secrets masked/rotatable, no credentials in logs, audit of control/approval/config changes.
- Observability: structured correlation IDs; component heartbeat, data freshness, model/policy versions, execution state and failure reasons; dashboard notifications only.
- Maintainability: modular code, typed schemas, deterministic tests, pinned dependencies, migrations, reproducible setup; no unnecessary microservices.
- Recovery/backup: critical order/risk/config/state recorded durably per event, high (predictions/decisions/history) proposed 1–5 min backups, medium (news/memory) 15–60 min, low (logs/reports) daily, model on change; incremental/versioned/encrypted backups with retention and restore verification. Frequency/RPO/RTO are proposals pending validation; local journal is not a substitute for tested backup.
- Deployment safety: do not hot-update risk/execution with open positions; prevent multiple active executors; safe rollback must not restore old state over actual broker reality.

---

## 10. Development plan and phase gates

PRD drafting can run in parallel. Implementation should follow approved contracts and dependency gates, not wait for all advanced research. All trading integration work defaults to fake broker/replay or MT5 Demo. No timeline or staffing estimate has been approved.

| Phase | Deliverable | Owner emphasis | Exit gate |
|---|---|---|---|
| P0 — PRD & contracts | 8 module PRDs + integration/QA plans, ADRs, shared schemas | all, X01/X02 | coverage of LOCKED rules; no conflicting contracts; safety TBD identified |
| P1 — Foundation | repo, config, API skeleton, DB migrations, event journal, UI shell, test harness | M08, M01, X01 | reproducible local start; typed contract tests; mock-only/no live |
| P2 — Trading & risk core | MT5 EA/bridge, ownership, DD, cycle/target, trailing guard, controls, recovery | M06, M08 | safety unit/integration/replay + demo smoke; independent risk path verified |
| P3 — Full AI MVP | MTF data, news/LLM, XGB/LGB, adaptive decision, recovery baseline, basic memory, full dashboard | M02–05, M07, M01 | end-to-end Demo-capable AI, no indicator-only substitute, evidence-linked reasons, bounded risk |
| P4 — Backtest/Shadow | point-in-time data, cost-aware historical replay, walk-forward, real market shadow | X02, M02–07 | no leakage; reports/baselines; safety scenarios tested |
| P5 — Live Demo/Forward | continuous demo on real market data; normal/volatile regimes, broker failure drills | M06, X02, all | stable operation and documented metrics; no real funds |
| P6 — Advanced AI | validated deeper models, pattern memory, adaptive ensemble, advanced deployment/reliability | M04/05/07/08 | each capability individually validated; unready features disabled |
| P7 — Live qualification | protected thresholds, risk review, broker/account suitability, manual approval | X02, M06, M07, Owner | only approved Live low risk if every relevant gate passes |
| P8 — Continuous improvement | shadow challenger, monitored performance, reduction/reversion/rollback | all | ongoing policy compliance; never raise user risk limit automatically |

**Full AI MVP includes** real AI forecast and news reasoning, adaptive decision/strategy selection, recovery proposals, risk/execution, fixed USD target, adaptive trailing, basic learning memory, web dashboard, testing and minimum security/recovery. Deep learning, full pattern matching, meta-learning and sophisticated infrastructure are phased later. Strategy implementations must be individually gated; MVP can enable only validated subset, and the complete requirement remains on roadmap.

### Parallel implementation dependencies

- First agree event/API schemas, money/time conventions, cycle/risk states, and broker adapter interface (X01 + M06/M08).
- M01 builds with mocked read APIs; M02 builds replay fixtures; M03 and M04 can work against agreed sample schemas. Separate news assessment from LLM reasoning to break dependency loop.
- M05 can implement decision pipeline using fixtures; real execution integration waits for M06 guard readiness.
- M07 begins experience schema/model registry early; full learning waits for validated datasets and outcome streams.
- X02 builds regression harness from P0; do not postpone QA until after AI is implemented.

---

## 11. Testing, acceptance and release policy

### 11.1 Evaluation stages

Unit + contract + integration → historical backtest → chronological walk-forward/held-out → Shadow on live market (no broker order) → Demo/forward on real market → readiness evaluation → explicit user approval → low-risk Live only if allowed. Stages may repeat. Demo execution is not a substitute for safety and statistical validation, and demo fills may differ from live fills.

- Historical replay must model bid/ask, spread, commissions, swap, slippage, contract/lot rules, latency, rejection/partial fill assumptions; record simplifications. MT5 Strategy Tester for EA behavior, Python replay for AI pipeline. Never call a live news feed to reconstruct what was known at a historical timestamp.
- Compare against simple benchmark and no-trade baseline; use chronological splits, avoid look-ahead and leakage, calibrate confidence on separate data, evaluate multiple market regimes; track selection bias/overfitting and uncertainty.
- Metrics: direction accuracy, Brier/calibration, MAE/RMSE as appropriate, net expectancy after costs, profit factor, max DD, trade/cycle win rates, target hit rate, recovery behavior, sample coverage, latency, reliability. Numeric qualification thresholds and required sample/duration must be set by X02/M04/M06/M07 before final evaluation, protected from user/AI edits, and cannot be optimized on final holdout.
- Live approval is bound to account/broker, model/policy versions and qualified configuration; changing material parameters invalidates approval as defined by policy. Start Low Risk; monitor in parallel shadow; reduce risk or revert Demo on degradation. Never auto-resume after DD breach.

### 11.2 Minimum cross-module acceptance matrix

| Test ID | Scenario / expected result | Requirement / owner |
|---|---|---|
| AT-01 | User inputs target $10, bot net estimate $10 → latch close; cancel owned pending; confirm all owned positions closed | PFT-01–02 / M06 |
| AT-02 | Final realized $9.80 after target-triggered close → win yes, target achieved no; no false reporting | PFT-05 / M06,M01 |
| AT-03 | Peak profit $9, active floor $7.50; floor cannot decrease; reach floor → close | PFT-03 / M05,M06 |
| AT-04 | Partial close/timeout/duplicate command → reconcile, no new cycle or duplicated order | TRD-04,PFT-04 / M06 |
| AT-05 | Finish-cycle vs normal restart; only normal authorized mode may start next cycle after confirmed close | TRD-05,PFT-04 / M05,M06 |
| AT-06 | Account DD threshold breached by manual trade; bot closes own positions only and persists lock | RSK-01–04,TRD-04 / M06 |
| AT-07 | Restart after DD breach or disconnect; no auto-resume; lifetime peak preserved | RSK-02,07,SYS-04 / M06,M08 |
| AT-08 | Insufficient margin/unsafe min lot/invalid symbol specs/ambiguous Netting ownership → reject entry | RSK-08,TRD-02–06 / M06 |
| AT-09 | Normal SL absent, broker-side Emergency SL verified; install failure follows safe policy | RSK-05 / M06 |
| AT-10 | Python/LLM unavailable; connected EA still performs risk/exit logic | RSK-05,SYS-02 / M03,M06,M08 |
| AT-11 | Market closed/gap/reject/disconnect during emergency close → unresolved state shown; no false guarantee | RSK-04–05 / M06,M01 |
| AT-12 | Pause blocks new/recovery entry and pending exposure while still protecting open positions | TRD-05 / M06 |
| AT-13 | Martingale or unapproved strategy/model in Live → deny | RSK-06,DEC-05,LRN-05 / M05,M06,M07 |
| AT-14 | Stale signal, invalid structured LLM response, injected news → no unauthorized intent or entry | LLM-04–05,DEC-02 / M03–06 |
| AT-15 | Replay features/news as of timestamp → no future information or leakage | DAT-04,PRE-05 / M02–04,M07 |
| AT-16 | Model artifact without approval/evidence → cannot activate for Live | LRN-05–06,VAL-04–06 / M04,M07,M06 |
| AT-17 | Chart-only vs news-enhanced forecasts have separate lineage; confidence score not mislabeled | PRE-03–04,DEC-01 / M04,M05 |
| AT-18 | Market regime changes during open cycle; strategy switch retains cycle target/DD | DEC-06,PFT-04 / M05,M06 |
| AT-19 | No positive opportunity or expected value → WAIT; no forced recovery trade | DEC-02,04 / M05,M06 |
| AT-20 | Browser close/refresh/WS reconnect; bot continues; stale state shown accurately | UI-01–05 / M01,M08 |
| AT-21 | Unauthorized config/control/approval request denied; credentials redacted; audit recorded | SYS-08 / M08,M01 |
| AT-22 | Backup restore + broker reconciliation; no duplicated order or loss of persistent risk lock | SYS-03–05 / M08,M06 |
| AT-23 | Cash deposits/withdrawals follow approved DD adjustment, not fake trading P&L | RSK-01–03 / M06 |
| AT-24 | Live disabled until passed gate + explicit approval; demo/live account mismatch blocked | VAL-01–06 / M06,M08,X02 |
| AT-25 | Strong trend, range, false breakout, reversal, CPI/NFP/FOMC/high spread simulated/demo; metrics recorded | VAL-02–04 / X02,M02–07 |
| AT-26 | Hybrid cloud outage and overlapping host activation; safe local behavior, single executor | SYS-02,04 / M08,M06 |
| AT-27 | Daily/individual/cycle history reconciles with realized deals and separates manual trade | PFT-05,UI-04 / M06,M01,M08 |

All sample money values in tests are fixtures, not recommended trading settings. Each PRD must add local tests and reference these shared IDs. Evidence: run ID, commit, test data/version, mode/account alias, broker specs where relevant, result/log/screenshot, and defects. Tests requiring MT5/market/provider access remain pending until environment is available; do not fabricate success.

### 11.3 Release Definition of Done

PRD reviewed + requirements traceable + approved contracts + implementation reviewed + unit/contract/integration tests + documented failure behavior + security review + reproducible install + operational runbook + known limitations. For live-related modules, verified Demo evidence and protected readiness gate are additional requirements. A checklist tick or completed code generation alone is not release evidence.

---

## 12. PRD template — copy into each module PRD

Each assignee should produce one self-contained PRD for their workstream. Do not copy the full master plan into every PRD; reference requirement IDs and describe module-specific behavior in enough detail for another developer to implement and test.

```markdown
# PRD-XX — <Module name>

- Module ID: MXX / X0X
- Owner: <assigned role/person>
- Reviewers: <upstream/downstream owners, X01, X02>
- Status: Draft / In review / Approved / Implemented / Validated
- Version: 0.1
- Parent: master_planV1.md (V1.0)
- Target phase: P0–P8
- Requirement IDs: <list from §3 / X01>

## 1. Summary & product goal
Why this module exists; user/system outcome; success metrics and evidence.

## 2. Scope
- In scope for Full AI MVP
- Advanced phase deliverables
- Explicit non-goals
- Assumptions / dependencies / prerequisites

## 3. Actors & use cases
Describe normal flow, edge cases, failures, recovery and authorization.

## 4. Functional requirements
| Local ID | Master ID | Requirement | Priority | Phase | Acceptance test |
|---|---|---|---|---|---|
| MXX-FR-001 | ... | SHALL ... | Must | MVP | ... |

## 5. Business rules & algorithms
Inputs, outputs, formulas/units, state transitions, priorities, invariants,
validation, threshold sources, examples, pseudocode; distinguish LOCKED,
PROPOSED and TBD. No invented performance or live guarantees.

## 6. Data model & interfaces
| Contract | Producer | Consumer | Schema/version | Validity/errors |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |
Describe API/events, IDs, time/currency/precision, persistence, ownership,
foreign keys, version compatibility, idempotency, error handling.

## 7. UX / control behavior (if applicable)
Views, user actions, status/empty/stale/error/closing states,
permissions, confirmation and audit. No direct trade bypass.

## 8. Non-functional requirements
Security, performance budgets, reliability, monitoring, data retention,
backup/restore, resource isolation and deployment compatibility.

## 9. Failure modes & safety
| Failure | Detection | Safe behavior | Recovery | Test ID |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |

## 10. Testing & acceptance criteria
Given / When / Then for each MUST; shared AT IDs; unit, contract,
integration, replay, demo and validation where relevant. State evidence
needed and conditions that block release. Numeric thresholds with rationale.

## 11. Implementation plan & deliverables
| Task ID | Output / path | Dependency | Acceptance | Owner |
|---|---|---|---|---|
| ... | ... | ... | ... | ... |
List order of work, fixtures, docs and runbook. No time estimates without basis.

## 12. Risks, open decisions & changes
| ID | Issue | Options | Impact | Decision owner | Status |
|---|---|---|---|---|---|
| ... | ... | ... | ... | ... | TBD |

## 13. Traceability & sign-off
| Master ID | Local FR | Test ID | Evidence | Status |
|---|---|---|---|---|
| ... | ... | ... | pending | Not tested |
Review approval, integration approval, QA evidence and release status.
```

### 12.1 Ready-to-use AI Agent assignment prompt

Replace `<MODULE_ID>` with the assigned ID, e.g. M06. Give the agent this master plan as input.

```text
You are the owner of <MODULE_ID> in the XAUUSD AI Auto Trading Bot project.
Read master_planV1.md completely. Your task is to CREATE ONLY the module PRD
at the file path listed in section 2, using the template in section 12.

1. Extract the module's master requirement IDs and cross-cutting safety rules.
2. Define scope, non-goals, MVP/advanced deliverables, interfaces, data and
   state behavior, dependencies, failure handling and testable acceptance.
3. Write Given/When/Then tests and a traceability matrix linking every MUST
   to master requirement IDs and acceptance evidence.
4. Mark unapproved design choices PROPOSED and unknown values TBD. Explain
   impact and decision owner. Do not invent thresholds, test results, API
   availability, broker compatibility or credentials.
5. Preserve shared contracts and locked rules. Do not modify other teams'
   interfaces without a change request. Do not add product features.
6. Work in demo/shadow scope only. No real trades, no live activation and no
   implementation unless explicitly requested in a separate task.
7. Output the complete Markdown PRD and a short list of dependencies/open
   decisions requiring review. Do not ask for information already in the plan.
```

### 12.2 Review workflow

Draft → peer review by upstream/downstream owners → X01 contract and safety review (with M06 where relevant) → X02 acceptance review → owner approval of scope changes if any → implementation-ready PRD. Keep ADR for technical decisions. Keep traceability matrix current when code/test status changes. Do not edit this master plan silently: propose versioned amendments with impacted IDs and PRDs.

---

## 13. Open decisions, risks and limitations

These items are not additional user feature questions; they are implementation/research work for the responsible teams. The owner may need to review material changes or approve actual live use. Do not resolve safety-critical issues by assuming values.

| ID | Open decision / limitation | Responsible | Blocks |
|---|---|---|---|
| OD-01 | MT5 EA–Python bridge transport, command acknowledgement, transaction ordering, platform compatibility | M06,X01 | execution integration |
| OD-02 | broker symbol specs, minimum lot, leverage, hedge/netting and USD/cent compatibility; starting capital may be insufficient | M06,X02 | broker qualification/live |
| OD-03 | exact cost ledger, account currency conversion, commission/swap handling, partial close and manual modifications | M06 | profit/risk acceptance |
| OD-04 | cash-flow-adjusted DD, epoch/lifetime ceilings, recovery remediation and protected risk bounds | M06,X02 | risk certification/live |
| OD-05 | emergency SL distance, exposure/stress bounds, adaptive trailing floor policy, fallback when AI fails | M06,M05,X02 | risk certification/live |
| OD-06 | news/calendar and cross-market providers, access, licenses, historical availability, quality and expiry | M02,M03 | data/AI validation |
| OD-07 | forecast horizons/labels, training windows, strategy entry/exit rules, model readiness metrics and thresholds | M04,M05,M07,X02 | model certification |
| OD-08 | live qualification duration/sample/regimes, protected promotion/rollback thresholds, approval scope | X02,M06,M07 | live authorization |
| OD-09 | library versions, exact schemas, migrations, performance budgets and resource needs | M08,X01,all | implementation/integration |
| OD-10 | remote access security, backup/restore RPO/RTO, cloud storage and local journal/sync policy | M08,X01 | deployment qualification |
| OD-11 | config change effect on running cycle, startup authorization, account switch, finish/stop conflict priority | M06,M08,M01,X01 | control acceptance |
| OD-12 | actual historical data, credentials to Demo environment, installed MT5, hardware/VPS and broker execution evidence not yet supplied/verified | Project Owner + assigned teams | real integration/testing |

Key financial limitation: recovery/hedging/grid/averaging can compound risk even without Martingale. Account DD cannot be guaranteed at exact threshold; broker-side stops may slip; manual trades outside bot control may increase loss after the bot stops. High cycle win rate does not prove profitability. Capital adequacy and robustness must be established empirically. Live may remain disabled indefinitely if conditions fail.

### 13.1 Change request template

```text
CR-ID / Title:
Affected master requirement IDs and PRDs:
Current behavior:
Proposed change and rationale:
Risk / security / financial impact:
Alternatives and evidence:
Migration / compatibility / rollback:
Required tests:
Owner / reviewers / approval status:
```

---

## 14. Handoff checklist & current project status

- [x] User requirements and conceptual design consolidated in Master Plan V1.0.
- [x] Eight module workstreams and two integration/QA roles proposed.
- [x] Requirement IDs, design decision mapping, common business rules, contract drafts, acceptance matrix and PRD template documented.
- [ ] Assign people/agents to modules and create individual PRDs in `docs/prd/`.
- [ ] Review and approve module PRDs and shared contracts; resolve relevant TBDs.
- [ ] Create repository and Phase P1 foundation.
- [ ] Implement MT5/Risk/Profit core; verify independent safety and bot-only execution.
- [ ] Build Full AI MVP and validate in replay/Shadow/Demo.
- [ ] Complete advanced scope and protected readiness evaluation as appropriate.
- [ ] Obtain explicit user approval for a qualified Live configuration; otherwise remain Demo/Shadow.

**Current status:** planning document only. No repository, source code, trained model, MT5 session, live order, backtest result, or readiness evidence has been created by this handoff. All PRD files listed above are target deliverable paths, not existing files.

**Next action:** assign each workstream owner and give them `master_planV1.md` plus the module ID. Each owner writes a draft PRD using §12; X01/X02 review contracts and acceptance before P1 implementation. No additional product feature questions are required at this stage.

---

End of Master Plan V1.0.
