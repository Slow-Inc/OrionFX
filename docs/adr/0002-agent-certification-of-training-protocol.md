# 0002 — Agent certification of the M04 training protocol (Shadow/Demo only)

- **Status:** Accepted (owner delegation, 2026-11-28). The first protocol under it is v1, a DRAFT (`docs/research/2026-11-28-m04-protocol-v1.md`, #132).
- **Area:** M04 training loop, X02 readiness, M07 model lifecycle
- **Amends:** ADR 0001, on **who certifies** a policy version for Shadow/Demo. ADR 0001 says the owner certifies each policy version. Under this ADR the agent certifies it, by delegation. Everything else in ADR 0001 stands. ADR 0001 is not edited.

## Context

- **ADR 0001.** It lets a system actor promote and roll back models in Shadow and Demo, but only under a policy the **owner** has certified. Any change to the policy is a new version that "needs the owner again".
- **The owner's delegation.** On **2026-11-28** the owner delegated that certification to the agent, in these words:

  > "ไม่ต้องถามผม ให้ทำการพิจารณาอย่างเข้มข้นแล้วเริ่ม loop training ได้เลย"
  >
  > In English: "Don't ask me. Deliberate rigorously, then start the training loop."

- **What the master plan allows.** Master plan OD-07 leaves "forecast horizons/labels, training windows, strategy entry/exit rules, model readiness metrics and thresholds" to **model certification** (`docs/plans/master_planV1.1.md:955`). So these values are certifiable, not [LOCKED].
- **What the code mints.**
  - A READY verdict from `EvidenceStore.evaluate` issues an `x02:live-qualification:<hash>` ref (`backend/app/validation/evidence.py:20`, `:45`, `:230`).
  - `ModelLifecycle` needs such a ref for a LIVE promotion (`backend/app/learning/lifecycle.py:14`).
  - Promotion is owner-only today (`OWNER_ACTOR`, `lifecycle.py:60`).
  - `DEFAULT_POLICY` (`evidence.py:107`) is uncertified.

## Decision

1. **Scope of the delegation.** The agent certifies the M04 protocol values used for **Shadow/Demo** qualification:
   - targets and gates;
   - the holdout rule;
   - the iteration budget;
   - the regime rule;
   - labels and horizons within OD-07;
   - the readiness thresholds used for Shadow/Demo promotion under ADR 0001.
2. **Narrower reading: Live stays with the owner.** Anything that mints live qualification stays owner-only:
   - an `x02:live-qualification:` ref;
   - a certified `ReadinessPolicy` for Live;
   - Live thresholds and stage durations (OD-08);
   - any real-money decision.

   A protocol certified under this ADR is therefore never saved as a certified `ReadinessPolicy`.
3. **Rules the agent must follow:**
   - every certified value rests on research **plus an advisor review**;
   - every certified value carries the line "certified by agent under owner delegation, 2026-11-28" and a rationale (its source or derivation);
   - **a target is never lowered after its results are seen**;
   - a change after the hash is a **new protocol version with a stated reason**;
   - a version after a failure is judged only on data nobody read under the failed version.
4. **Revocation.** The owner may revoke or narrow the delegation at any time. Until then, ADR 0001's promotion mechanics apply with the agent as the certifier for Shadow/Demo.

## Alternatives considered

- **The owner certifies every version (ADR 0001 as written).** Rejected by the owner: it blocks the unattended loop the owner asked for.
- **Delegate everything, including Live qualification.** Rejected:
  - it contradicts VAL-05 and AT-24 (Live needs manual approval);
  - the code would mint `x02:live-qualification:` refs from agent-certified thresholds.
- **Delegate without rules.** Rejected: without "never lower a target after results" and versioning, delegated certification could move goalposts silently.

## Consequences

- **Docs.** `CLAUDE.md` ("Training-loop research autonomy") and `docs/agents/training-loop.md` (§6, §8, §9) state the delegation and its limits. The protocol v1 draft carries the certification line and a rationale for each value.
- **Code.** No code change is needed for the delegation itself.
  - The ADR 0001 system actor and its four refusal tests still apply.
  - "Certified policy" in that work means certified under this ADR for Shadow/Demo.
  - `DEFAULT_POLICY` stays uncertified for Live.
- **Audit.** Every certified protocol version records its certifier ("agent under owner delegation, 2026-11-28"), its hash, and its research and advisor evidence.
- **Risk.** The agent both runs the research and certifies the goalposts. The rules in Decision 3 are the guard: pre-registration before data, no lowering after results, a counted budget, and a single-use holdout. They make that dual role auditable, but not independent. That residual risk is why Live stays owner-only.

---

## ภาษาไทย

**บริบท:**
- ADR 0001 ให้ system actor promote และ rollback model ใน Shadow/Demo ได้ แต่ต้องอยู่ภายใต้ policy ที่ **owner** รับรอง และถ้ามี version ใหม่ต้องให้ owner รับรองอีกครั้ง
- วันที่ **2026-11-28** owner มอบอำนาจการรับรองนี้ให้ agent ด้วยคำว่า "ไม่ต้องถามผม ให้ทำการพิจารณาอย่างเข้มข้นแล้วเริ่ม loop training ได้เลย"
- master plan OD-07 กำหนดให้ horizon, label, ช่วงข้อมูลเทรน และเกณฑ์ readiness ของ model เป็นเรื่องของ model certification จึงไม่ใช่ค่า [LOCKED]
- ในโค้ด ผล READY จาก `EvidenceStore.evaluate` จะออก ref `x02:live-qualification:` และ ref นี้ใช้ promote ขึ้น LIVE

**การตัดสินใจ:**
1. **ขอบเขต:** agent รับรองค่าของ protocol M04 ที่ใช้คัดเลือกสำหรับ **Shadow/Demo** ได้แก่
   - เป้าและ gate
   - กฎ holdout
   - budget
   - กฎ regime
   - label และ horizon ภายใต้ OD-07
   - เกณฑ์ readiness ที่ใช้ promote ขึ้น Shadow/Demo
2. **อ่านแบบแคบ:** ทุกอย่างที่ออก live qualification ยังเป็นของ owner ได้แก่
   - ref `x02:live-qualification:`
   - `ReadinessPolicy` ที่รับรองสำหรับ Live
   - เกณฑ์ Live (OD-08)
   - การตัดสินใจเรื่องเงินจริงทั้งหมด

   protocol ที่ agent รับรองจึงไม่ถูกบันทึกเป็น `ReadinessPolicy` ที่รับรองแล้ว
3. **กติกา:**
   - ทุกค่าต้องมาจาก research และผ่านการทบทวนโดย advisor
   - ทุกค่าต้องมีบรรทัด "certified by agent under owner delegation, 2026-11-28" พร้อมเหตุผล (ที่มาหรือวิธีคำนวณ)
   - ห้ามลดเป้าหลังเห็นผล
   - ถ้าแก้หลัง hash ต้องออก version ใหม่และระบุเหตุผล
   - version ที่ออกหลังจากล้มเหลวต้องตัดสินบนข้อมูลที่ยังไม่มีใครอ่านภายใต้ version ก่อนหน้า
4. owner ถอนหรือจำกัดการมอบอำนาจได้ทุกเมื่อ

**ทางเลือกที่ไม่เลือก:**
- **owner รับรองทุก version:** ทำให้ loop ทำงานเองไม่ได้
- **มอบรวมถึง Live:** ขัดกับ VAL-05/AT-24 และโค้ดจะออก live qualification จากเกณฑ์ที่ agent รับรองเอง
- **มอบโดยไม่มีกติกา:** เป้าอาจถูกเลื่อนได้โดยไม่มีใครเห็น

**ผลที่ตามมา:**
- ไม่ต้องแก้โค้ดเพื่อการมอบอำนาจนี้
- `DEFAULT_POLICY` ยังไม่ถูกรับรองสำหรับ Live
- ทุก protocol version บันทึกผู้รับรอง, hash และหลักฐาน
- **ความเสี่ยงที่เหลือ:** agent เป็นทั้งผู้วิจัยและผู้กำหนดเป้า กติกาในข้อ 3 ทำให้ตรวจสอบย้อนหลังได้ แต่ไม่ได้ทำให้เป็นอิสระต่อกัน Live จึงยังเป็นของ owner
