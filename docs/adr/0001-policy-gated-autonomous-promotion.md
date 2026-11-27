# 0001 — Policy-gated autonomous model promotion to Shadow and Demo

- **Status:** Accepted (owner decision, 2026-11-28); implementation pending in PRD #120
- **Area:** M07 model lifecycle, X02 readiness, M06 eligibility
- **Supersedes:** nothing. This narrows the owner-only rule in `ModelLifecycle.promote` (`backend/app/learning/lifecycle.py`, "Only the owner may approve, by allow-list (OWNER_ACTOR)").

## Context

The owner wants OrionFX to run 100% autonomously. That means trading by itself, and also learning by itself: detect drift, retrain, evaluate, promote and roll back without a human in each step.

**How the code works today.** Every promotion needs the owner. `promote` accepts only `OWNER_ACTOR`, and nothing promotes itself.

**What the master plan requires.** A human is mandatory only for real-money Live:
- VAL-05: "Live ต้อง manual approval"
- AT-24: Live disabled until gate plus explicit approval
- §Validation: explicit user approval before Live

For Shadow and Demo it asks that adaptation stay "within previously validated policy … no uncontrolled online optimization" (§15.5 item 5). LRN-05 requires new models to pass Shadow, Demo and Validation before Live. Policy-gated promotion inside Shadow and Demo therefore fits the [LOCKED] requirements and needs no Change Request.

**What a model must clear first.** The owner set the bar: a model may run on the real market, on a Demo account, only after it has passed the pre-registered test targets. Without passing them, running it on Demo is pointless.

## Decision

1. **Who promotes.** A system actor may promote and roll back models, for **Shadow** and **Demo** modes only, and only under a **certified** policy.
   - The owner certifies the policy once.
   - The policy is the PRD #120 protocol: targets, iteration budget, fold plan, holdout and regime rule, plus the anti-overfitting rules and a certified `ReadinessPolicy`.
   - Certification is per policy version. Any change to the policy is a new version and needs the owner again.
2. **Gate to Demo.** A model reaches Demo only when, under that policy version, **every** tier passes:
   - model metrics per fold;
   - the system backtest;
   - the single-use holdout, with its power check;
   - the readiness verdict, judged on the one subject being promoted.
   A model that fails stays a candidate. At most it runs as a recorded, never-trading Shadow predictor.
3. **Automatic rollback.** On Demo, forward metrics are checked against the policy's floors.
   - If drift or degradation breaches the floors, the system rolls back automatically to the previous champion, or to no model, in which case M05 waits.
   - It never rolls *forward* to an unqualified model.
4. **Real-money Live is unchanged.**
   - It stays unselectable: `TradingConfig.account_mode` is SHADOW or DEMO only.
   - LIVE promotion stays owner-only and needs a verified live qualification.
   - Enabling real money remains a separate owner decision under the master plan.
5. **Audit and independence.**
   - Every automatic promotion and rollback is audited with its evidence refs, policy version and actor.
   - M06 checks registry eligibility independently, per #121 / BLK-04. M06 therefore refuses a model or strategy whose eligibility is missing, expired or revoked, whoever promoted it.

## Alternatives considered

- **Owner approves every promotion (today).** Rejected: the loop cannot run unattended, which defeats the goal.
- **Fully automatic, including real-money Live.** Rejected: it contradicts VAL-05 and AT-24 and would need a Change Request. The owner chose Demo only.
- **Auto-promote on walk-forward scores alone.** Rejected: walk-forward scores used for selection are development data (PRD #120 anti-overfitting rule 1). The holdout and the system tier must also pass.

## Consequences

**Lifecycle.**
- `ModelLifecycle` gains a policy-bound system actor that is valid for SHADOW and DEMO.
- `promote` and `rollback` check the mode against the actor.
- Tests must show four refusals:
  - the system actor cannot promote to LIVE;
  - it cannot promote under an uncertified or changed policy;
  - it cannot promote without the full evidence set;
  - it cannot roll forward.

**Readiness.** The readiness verdict must be per subject: BLK-05 in PRD #120 Slice 0 is a precondition.

**What the owner still owns:**
- certifying each policy version: targets, thresholds, holdout;
- any real-money decision;
- the external resources: MT5 and the Demo account (#17), and the news licence (#41).

**Honesty about Demo results.** Demo results are forward evidence on real prices with demo fills. They are not proof of profit, and demo fills may differ from live fills (master plan §Validation).

---

## ภาษาไทย

**บริบท:** owner ต้องการให้ OrionFX ทำงานเอง 100% ทั้งการเทรดและการเรียนรู้ คือตรวจ drift, เทรนใหม่, ประเมิน, promote และ rollback ได้เองโดยไม่ต้องมีคนกดทุกขั้น
- **โค้ดตอนนี้:** การ promote ต้องเป็น owner เท่านั้น
- **master plan:** บังคับให้มีคนอนุมัติเฉพาะตอนขึ้น Live เงินจริง (VAL-05, AT-24, §Validation) ส่วน Shadow/Demo กำหนดแค่ว่าต้องปรับภายใน policy ที่ validate แล้ว (§15.5 ข้อ 5) การ promote ที่ผูกกับ policy ใน Shadow/Demo จึงไม่ขัดกับข้อ [LOCKED] และไม่ต้องทำ CR
- **เงื่อนไขของ owner:** model จะขึ้นตลาดจริงในบัญชี Demo ได้ก็ต่อเมื่อผ่านเป้าการทดสอบที่ลงทะเบียนไว้ ถ้าไม่ผ่าน การขึ้น Demo ก็ไม่มีความหมาย

**การตัดสินใจ:**
1. **ใครเป็นคน promote:** system actor promote และ rollback ได้เฉพาะโหมด **Shadow** และ **Demo** และต้องอยู่ภายใต้ policy ที่ owner **รับรอง** แล้วเท่านั้น
   - owner รับรอง policy ครั้งเดียว
   - policy คือ protocol ของ PRD #120 ได้แก่ เป้า, จำนวนรอบ, แผน fold, holdout, กฎ regime รวมถึงกติกากัน overfitting และ `ReadinessPolicy` ที่รับรองแล้ว
   - การรับรองผูกกับ policy version ถ้าแก้ policy จะกลายเป็น version ใหม่ และต้องให้ owner รับรองอีกครั้ง
2. **เงื่อนไขการขึ้น Demo:** ต้องผ่าน**ทุกชั้น**ภายใต้ policy version นั้น
   - metric ของ model ราย fold
   - system backtest
   - holdout ที่ใช้ได้ครั้งเดียว พร้อมผ่านการตรวจ power
   - readiness ที่ตัดสินเฉพาะ model ตัวที่จะ promote
   ถ้าไม่ผ่านจะยังเป็น candidate และอย่างมากได้แค่ทำนายแบบเงาใน Shadow โดยไม่เทรด
3. **Rollback อัตโนมัติ:** บน Demo ระบบตรวจ metric แบบ forward เทียบกับขั้นต่ำของ policy
   - ถ้า drift หรือผลแย่ลงต่ำกว่าเกณฑ์ ระบบจะ rollback เองไปที่ champion ตัวก่อน หรือไม่ใช้ model เลย (M05 จะ WAIT)
   - ระบบไม่มีวันเลื่อนไปใช้ model ที่ยังไม่ผ่านเกณฑ์
4. **Live เงินจริงไม่เปลี่ยน:**
   - ยังเลือกไม่ได้ เพราะ `account_mode` มีแค่ SHADOW และ DEMO
   - การ promote ขึ้น LIVE ยังต้องเป็น owner และต้องมี live qualification ที่ตรวจแล้ว
   - การเปิดใช้เงินจริงยังเป็นการตัดสินใจแยกของ owner ตาม master plan
5. **Audit และการตรวจอิสระ:**
   - ทุกการ promote และ rollback อัตโนมัติถูกบันทึก audit พร้อม ref ของหลักฐาน, policy version และ actor
   - M06 ตรวจ eligibility จาก registry เองโดยอิสระ (#121, BLK-04) จึงปฏิเสธ model หรือ strategy ที่ eligibility ขาด หมดอายุ หรือถูกเพิกถอน ไม่ว่าใครเป็นคน promote

**ทางเลือกที่ไม่เลือก:**
- **owner อนุมัติทุกครั้ง (แบบปัจจุบัน):** loop ทำงานเองไม่ได้ ซึ่งขัดกับเป้าหมาย
- **อัตโนมัติทั้งหมดรวมถึง Live เงินจริง:** ขัดกับ VAL-05 และ AT-24 และต้องทำ CR อีกทั้ง owner เลือก Demo เท่านั้น
- **promote จากคะแนน walk-forward อย่างเดียว:** คะแนนที่ใช้เลือก model ถือเป็นข้อมูล development ตามกติกาข้อ 1 ของ PRD #120 จึงต้องผ่าน holdout และชั้น system ด้วย

**ผลที่ตามมา:**
- **lifecycle:** `ModelLifecycle` เพิ่ม system actor ที่ผูกกับ policy และใช้ได้กับ SHADOW/DEMO และ `promote` กับ `rollback` ตรวจว่าโหมดตรงกับ actor ต้องมี test ยืนยันว่า system actor
  - promote ขึ้น LIVE ไม่ได้
  - promote ภายใต้ policy ที่ยังไม่รับรองหรือถูกแก้ไม่ได้
  - promote โดยไม่มีหลักฐานครบชุดไม่ได้
  - เลื่อนไปใช้ model ที่ยังไม่ผ่านเกณฑ์ไม่ได้
- **readiness:** ต้องตัดสินทีละ subject ซึ่งคือ BLK-05 ใน Slice 0 ของ PRD #120 และต้องแก้ก่อน
- **สิ่งที่ยังเป็นของ owner:**
  - การรับรองแต่ละ policy version (เป้า, เกณฑ์, holdout)
  - ทุกการตัดสินใจที่เกี่ยวกับเงินจริง
  - ทรัพยากรภายนอก คือ MT5 กับบัญชี Demo (#17) และ license ข่าว (#41)
- **ความตรงไปตรงมาเรื่องผลบน Demo:** ผลบน Demo เป็นหลักฐาน forward บนราคาจริงแต่ fill เป็นของบัญชี demo ไม่ใช่หลักฐานว่าทำกำไรได้ และ fill บน demo อาจต่างจาก live (§Validation)
