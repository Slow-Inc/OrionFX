<!-- lang:en -->
# Ubiquitous Language

Canonical term glossary for OrionFX. When a term appears in **bold**, use it exactly
as written — in code identifiers, PR descriptions, issue titles, and team conversations.
Source of meaning: `docs/plans/master_planV1.md` (section refs in brackets).

## Trading cycle & profit

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **Cycle** | One trading round from its first **Trade Intent** until every **Bot Position** is closed and reconciled. Has a permanent `cycle_id` assigned before the first intent [§4.1]. | round, session, batch |
| **Cycle Net** | `cycle_net_estimated_usd` = realized net + floating net − estimated remaining close cost, for one **Cycle**, after costs, with no double counting [§4.1]. | profit, P&L (unqualified) |
| **Profit Target** | Fixed USD amount per **Cycle** set by the user; never a percentage; AI may not raise it [PFT-01]. | TP, take profit, goal % |
| **Target Triggered** vs **Target Achieved** | Triggered = **Cycle Net** reached the **Profit Target** and closing began; Achieved = the final realized net actually ≥ target. Stored separately [§4.1]. | hit (unqualified) |
| **Basket Trailing** | Protecting a **Cycle**'s peak net profit with a **Trailing Floor** that only moves up once activated [PFT-03, §4.2]. | trailing stop (per-position) |
| **Trailing Floor** | `trailing_floor_usd`; when **Cycle Net** ≤ floor the cycle latches `CLOSING` [§4.2]. | stop level |
| **Cycle Restart** | Starting a new **Cycle** only after the previous one is `CLOSED` and the market re-evaluated; WAIT is allowed [PFT-04]. | auto-reopen |

## Risk & safety

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **Account DD** | Drawdown % of the whole MT5 account equity (user + bot), from a cash-flow-adjusted peak [RSK-01, §4.3]. | daily loss, bot DD |
| **Risk Epoch** | The period tracked by `epoch_peak`; a new epoch needs validation and user approval after a breach [RSK-02]. | session |
| **Lifetime Peak** | `lifetime_peak`; never resets with cycles, restarts, or new epochs [RSK-02]. | all-time high (informal) |
| **Protected Limit** | System-set risk ceiling inside the user's max loss; AI cannot raise it [RSK-03]. | soft limit |
| **Risk Lock** | Latched state after a DD breach; never auto-resumes on equity recovery or restart [§4.3]. | pause |
| **Risk Veto** | M06's authority to reject any **Trade Intent**; overrides AI [RSK-04]. | override |
| **Emergency SL** | Broker-side stop attached and verified per position as the last safety layer; there is no normal per-position SL [§4.4]. | SL, stop loss (unqualified) |

## Ownership & execution

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **Bot Position** | An order/position/deal proven to belong to the bot via magic number + account/server + symbol + persisted id mapping [TRD-04, §4.4]. | our trade |
| **Manual Position** | Anything not proven to be a **Bot Position**; the bot never closes or adopts it [TRD-04]. | user trade (fine in prose, not in code) |
| **Trade Intent** | M05's proposal to trade; not an order. Only M06 turns an approved intent into execution via the EA [§2.1, §4.4]. | signal, order |
| **Execution EA** | The MQL5 Expert Advisor — the single trade writer to MT5 [§5.1]. | bot (ambiguous) |
| **Reconcile** | Comparing local durable state with MT5 (source of truth) before trusting or retrying [§4.4]. | sync |

## Control commands

| Term | Definition | Aliases to avoid |
|------|-----------|-----------------|
| **Start** / **Pause** / **Finish Cycle** / **Emergency Stop** / **Approve Recovery** | The five control commands and their non-negotiable behaviours [§4.5]. | stop (unqualified), kill |

## Relationships

- A **Cycle** contains one or more **Bot Positions**; **Cycle Net** is computed per **Cycle**.
- **Basket Trailing** and the **Profit Target** both end a **Cycle** by latching `CLOSING`.
- **Account DD** spans **Bot Positions** and **Manual Positions**, but a breach closes only **Bot Positions** and sets a **Risk Lock**.
- A **Trade Intent** passes the **Risk Veto** before the **Execution EA** sends anything.

## Flagged ambiguities

- **"stop"**: always say which — **Pause**, **Finish Cycle**, **Emergency Stop**, or **Emergency SL**.
- **"profit"**: say **Cycle Net** (estimated, live) or final realized net (after close).
- **"bot"**: the whole product, never the **Execution EA** — name the EA explicitly.
<!-- lang:end -->

<!-- lang:th -->
# Ubiquitous Language — คำศัพท์มาตรฐาน

คำศัพท์มาตรฐานของ OrionFX คำที่เป็น **ตัวหนา** ให้ใช้ตามที่เขียนไว้ทุกตัวอักษร ทั้งในชื่อ identifier ในโค้ด คำอธิบาย PR ชื่อ issue และการคุยในทีม
ความหมายอ้างอิงจาก `docs/plans/master_planV1.md` (เลขหัวข้ออยู่ในวงเล็บ)

## รอบการเทรดและกำไร

| Term | ความหมาย | คำที่ห้ามใช้แทน |
|------|-----------|-----------------|
| **Cycle** | การเทรดหนึ่งรอบ ตั้งแต่ **Trade Intent** แรกจนปิดและ reconcile **Bot Position** ครบทุกไม้ มี `cycle_id` ถาวรที่กำหนดก่อนส่ง intent แรก [§4.1] | round, session, batch |
| **Cycle Net** | `cycle_net_estimated_usd` = กำไรสุทธิที่ปิดแล้ว + กำไรสุทธิลอยตัว − ต้นทุนปิดที่คาดว่าจะเกิด ของ **Cycle** เดียว หลังหักต้นทุน และห้ามนับซ้ำ [§4.1] | profit, P&L (แบบไม่ระบุ) |
| **Profit Target** | จำนวน USD คงที่ต่อ **Cycle** ที่ผู้ใช้ตั้ง ไม่ใช่เปอร์เซ็นต์ และ AI เพิ่มเองไม่ได้ [PFT-01] | TP, take profit, goal % |
| **Target Triggered** กับ **Target Achieved** | Triggered = **Cycle Net** ถึง **Profit Target** แล้วเริ่มปิด · Achieved = กำไรสุทธิสุดท้ายที่ปิดจริง ≥ เป้า เก็บแยกกัน [§4.1] | hit (แบบไม่ระบุ) |
| **Basket Trailing** | การรักษากำไรสุทธิสูงสุดของ **Cycle** ด้วย **Trailing Floor** ที่ขยับขึ้นอย่างเดียวหลัง activate [PFT-03, §4.2] | trailing stop (รายไม้) |
| **Trailing Floor** | `trailing_floor_usd` เมื่อ **Cycle Net** ≤ floor รอบนั้นจะ latch `CLOSING` [§4.2] | stop level |
| **Cycle Restart** | เริ่ม **Cycle** ใหม่ได้หลังรอบเดิม `CLOSED` และประเมินตลาดใหม่แล้วเท่านั้น เลือก WAIT ได้ [PFT-04] | auto-reopen |

## ความเสี่ยงและความปลอดภัย

| Term | ความหมาย | คำที่ห้ามใช้แทน |
|------|-----------|-----------------|
| **Account DD** | เปอร์เซ็นต์ drawdown ของ equity ทั้งบัญชี MT5 (ของผู้ใช้ + ของบอท) เทียบกับ peak ที่ปรับ cash flow แล้ว [RSK-01, §4.3] | daily loss, bot DD |
| **Risk Epoch** | ช่วงที่ติดตามด้วย `epoch_peak` หลัง breach ต้องผ่าน validation และผู้ใช้อนุมัติก่อนเริ่ม epoch ใหม่ [RSK-02] | session |
| **Lifetime Peak** | `lifetime_peak` ไม่ reset ตาม cycle, restart หรือ epoch ใหม่ [RSK-02] | all-time high (ภาษาพูด) |
| **Protected Limit** | เพดานความเสี่ยงที่ระบบตั้งภายในกรอบ max loss ของผู้ใช้ AI เพิ่มเองไม่ได้ [RSK-03] | soft limit |
| **Risk Lock** | สถานะที่ latch ไว้หลัง DD breach ไม่กลับมาทำงานเองเมื่อ equity ฟื้นหรือ restart [§4.3] | pause |
| **Risk Veto** | อำนาจของ M06 ในการปฏิเสธ **Trade Intent** ใดก็ได้ อยู่เหนือ AI [RSK-04] | override |
| **Emergency SL** | stop ฝั่งโบรกเกอร์ที่ attach และ verify ต่อไม้ เป็นชั้นป้องกันสุดท้าย ไม่มี SL ปกติรายไม้ [§4.4] | SL, stop loss (แบบไม่ระบุ) |

## ความเป็นเจ้าของและการส่งคำสั่ง

| Term | ความหมาย | คำที่ห้ามใช้แทน |
|------|-----------|-----------------|
| **Bot Position** | order/position/deal ที่พิสูจน์ได้ว่าเป็นของบอท ผ่าน magic number + account/server + symbol + mapping ของ id ที่เก็บถาวร [TRD-04, §4.4] | our trade |
| **Manual Position** | อะไรก็ตามที่พิสูจน์ไม่ได้ว่าเป็น **Bot Position** บอทไม่ปิดและไม่ adopt [TRD-04] | user trade (ใช้ในประโยคได้ แต่ไม่ใช้ในโค้ด) |
| **Trade Intent** | ข้อเสนอเทรดจาก M05 ไม่ใช่คำสั่ง มีแค่ M06 ที่เปลี่ยน intent ที่อนุมัติแล้วเป็นการส่งคำสั่งจริงผ่าน EA [§2.1, §4.4] | signal, order |
| **Execution EA** | Expert Advisor ภาษา MQL5 ตัวเดียวที่เขียนคำสั่งเทรดลง MT5 [§5.1] | bot (กำกวม) |
| **Reconcile** | เทียบ state ที่เก็บในเครื่องกับ MT5 (ซึ่งเป็นความจริง) ก่อนเชื่อหรือก่อน retry [§4.4] | sync |

## คำสั่งควบคุม

| Term | ความหมาย | คำที่ห้ามใช้แทน |
|------|-----------|-----------------|
| **Start** / **Pause** / **Finish Cycle** / **Emergency Stop** / **Approve Recovery** | คำสั่งควบคุมห้าตัวและพฤติกรรมที่ต่อรองไม่ได้ของแต่ละตัว [§4.5] | stop (แบบไม่ระบุ), kill |

## ความสัมพันธ์

- **Cycle** หนึ่งมี **Bot Position** ตั้งแต่หนึ่งไม้ขึ้นไป และ **Cycle Net** คิดแยกต่อ **Cycle**
- ทั้ง **Basket Trailing** และ **Profit Target** จบ **Cycle** ด้วยการ latch `CLOSING`
- **Account DD** นับรวมทั้ง **Bot Position** และ **Manual Position** แต่เมื่อ breach จะปิดเฉพาะ **Bot Position** แล้วตั้ง **Risk Lock**
- **Trade Intent** ต้องผ่าน **Risk Veto** ก่อนที่ **Execution EA** จะส่งอะไรออกไป

## คำที่กำกวม

- **"stop"**: ต้องบอกเสมอว่าหมายถึง **Pause**, **Finish Cycle**, **Emergency Stop** หรือ **Emergency SL**
- **"profit"**: ให้บอกว่าเป็น **Cycle Net** (ค่าประมาณระหว่างรอบ) หรือกำไรสุทธิที่ปิดจริง (หลังปิดรอบ)
- **"bot"**: หมายถึงทั้งระบบ ไม่ใช่ **Execution EA** ถ้าหมายถึง EA ให้เรียกชื่อ EA ตรง ๆ
<!-- lang:end -->
