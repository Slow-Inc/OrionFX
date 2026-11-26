<!-- lang:en -->
# OrionFX System Context

## Language

> **Canonical glossary = `UBIQUITOUS_LANGUAGE.md`** (root). The terms below are a
> local quick-reference for this document; if they ever disagree, `UBIQUITOUS_LANGUAGE.md` wins.

**Trade Intent**:
A proposal from the Adaptive Decision Engine (M05) to open, add to, or close exposure. It is not an order and has no authority on its own; the Risk & Execution Engine (M06) re-checks policy and live execution state and is the only path to the broker.
_Avoid_: signal, order

**Risk Veto**:
M06's authority to reject any **Trade Intent**, independent of AI. It does not wait for the LLM.
_Avoid_: override

### Example dialogue

> **Dev A:** "When the model is confident enough, can M05 send the order straight to MT5?"
> **Dev B:** "No — M05 only emits a **Trade Intent**. M06 applies the **Risk Veto** and the **Execution EA** is the single trade writer."

---

## System Architecture — 2026-11-27 (planned; no code yet)

### Modules
Eight product modules plus two support roles (master plan §2): M01 Web Dashboard, M02 Market Data, M03 LLM & News, M04 Prediction, M05 Adaptive Decision, M06 Risk & Execution (critical path), M07 Learning & Memory, M08 System Core; X01 Architecture & Integration, X02 Validation & QA.

### Data and control flow
```
MT5 → M02 market data → M03 news/LLM, M04 prediction → M05 Trade Intent
M05 → M06 Risk Veto → MQL5 Execution EA → MT5
M06 outcomes → M07 memory → approved models back to M04/M05
all modules → M08 core → M01 dashboard (read models); M01 → M08 → M06 (control commands)
```

### Truth Hierarchy
```
MT5 terminal/broker   — source of truth: account, positions, orders, deals
Local durable ledger  — intent/state/history (SQLite, planned); reconcile before trusting
Dashboard (M01)       — display + control commands only; not safety-critical, need not stay open
```

### Safety independence
The MQL5 risk guard keeps emergency handling working if Python or the LLM fails, as long as the terminal and broker connection work. Broker-side **Emergency SL** is the fallback when the terminal does not (master plan §5.1).
<!-- lang:end -->

<!-- lang:th -->
# OrionFX System Context — ภาษาไทย

## ภาษา

> **คำศัพท์มาตรฐานอยู่ที่ `UBIQUITOUS_LANGUAGE.md`** (root) คำด้านล่างเป็นแค่ตัวช่วยอ่านเอกสารนี้ ถ้าขัดกันเมื่อไร ให้ยึด `UBIQUITOUS_LANGUAGE.md`

**Trade Intent**:
ข้อเสนอจาก Adaptive Decision Engine (M05) ให้เปิด เพิ่ม หรือปิดความเสี่ยง ไม่ใช่คำสั่งเทรด และไม่มีอำนาจในตัวเอง Risk & Execution Engine (M06) จะตรวจ policy และสถานะการส่งคำสั่งล่าสุดซ้ำ และเป็นทางเดียวที่ไปถึงโบรกเกอร์
_ห้ามใช้แทน_: signal, order

**Risk Veto**:
อำนาจของ M06 ที่จะปฏิเสธ **Trade Intent** ใดก็ได้ ทำงานแยกจาก AI และไม่รอ LLM
_ห้ามใช้แทน_: override

### ตัวอย่างบทสนทนา

> **Dev A:** "ถ้าโมเดลมั่นใจพอ ให้ M05 ส่งคำสั่งเข้า MT5 ตรง ๆ เลยได้ไหม?"
> **Dev B:** "ไม่ได้ — M05 ส่งได้แค่ **Trade Intent** M06 เป็นตัวใช้ **Risk Veto** และ **Execution EA** เป็นตัวเดียวที่เขียนคำสั่งเทรด"

---

## สถาปัตยกรรมระบบ — 2026-11-27 (ยังเป็นแผน ยังไม่มีโค้ด)

### โมดูล
โมดูลผลิตภัณฑ์แปดตัว กับบทบาทสนับสนุนสองตัว (master plan §2): M01 Web Dashboard, M02 Market Data, M03 LLM & News, M04 Prediction, M05 Adaptive Decision, M06 Risk & Execution (critical path), M07 Learning & Memory, M08 System Core · X01 Architecture & Integration, X02 Validation & QA

### การไหลของข้อมูลและคำสั่ง
```
MT5 → M02 market data → M03 news/LLM, M04 prediction → M05 Trade Intent
M05 → M06 Risk Veto → MQL5 Execution EA → MT5
M06 outcomes → M07 memory → approved models back to M04/M05
all modules → M08 core → M01 dashboard (read models); M01 → M08 → M06 (control commands)
```

### ลำดับความจริง
```
MT5 terminal/broker   — ความจริง: account, positions, orders, deals
Local durable ledger  — intent/state/history (SQLite ตามแผน) ต้อง reconcile ก่อนเชื่อ
Dashboard (M01)       — แสดงผลและส่ง control command เท่านั้น ไม่ใช่ส่วน safety-critical ไม่ต้องเปิดค้าง
```

### ความปลอดภัยที่ไม่พึ่งส่วนอื่น
risk guard ใน MQL5 ยังจัดการเหตุฉุกเฉินได้แม้ Python หรือ LLM ล่ม ตราบใดที่ terminal และการเชื่อมต่อโบรกเกอร์ยังใช้ได้ ถ้า terminal ใช้ไม่ได้ **Emergency SL** ฝั่งโบรกเกอร์คือชั้นสำรอง (master plan §5.1)
<!-- lang:end -->
