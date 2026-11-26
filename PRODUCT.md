# Product

<!-- lang:en -->
> This is the product brief (users, purpose, brand). The **visual design system** —
> color tokens, typography, components, do/don'ts — is canonical in **`DESIGN.md`**;
> the brand/principles below are the "why", DESIGN.md is the "how".
> Requirements of record live in `docs/plans/master_planV1.md`; this file summarizes them and never overrides them.

## Users
A single owner-operator running the bot for personal use (master plan §1.1). Multi-user SaaS, subscriptions, copy/social trading and a native mobile app are out of scope [LOCKED, §1.4].

## Product Purpose
An AI auto-trading bot for XAUUSD on MT5. AI analyses news and charts, forecasts, selects and switches strategies, opens and manages positions, protects profit, and learns from outcomes — all under a Risk Engine independent of the AI. The user sets Initial Lot, a fixed USD **Profit Target** per **Cycle**, a maximum drawdown %, and controls the bot from a browser.

It succeeds when: it trades a Demo account automatically and controllably from the browser; profit and DD are computed correctly; it closes and recovers state across restarts; it never touches **Manual Positions**; a blocked trade stays blocked and a DD lock survives restart (§1.2). It does not promise profit.

## Brand Personality
**Sober.** The product handles money; it never sounds excited about a trade or hints at guaranteed returns.

**Accountable.** Every decision shows its reason and the data behind it; important events can be audited later.

**Calm under failure.** When something breaks, it says what is unresolved and what it is doing about it, instead of hiding the state.

## Anti-references
**Trading "signal" apps that promise win rates** — the master plan forbids guaranteed-profit or exact-stop claims (§4.4).
**Gamified dashboards (confetti, streaks)** — they reward risk-taking, the opposite of a risk-first product.

## Design Principles
1. **Risk before profit** — safety and refusal-when-uncertain outrank profit and availability (§0.1).
2. **Simple control, advanced insight** — a few clear commands up front, depth on demand (D31).
3. **Show reasons, not just numbers** — decisions link to the evidence behind them (§3.5).
4. **Honest state** — show pending/unresolved instead of pretending an action succeeded.

## Accessibility & Inclusion
Target WCAG 2.1 AA for the dashboard; profit/loss is never signalled by colour alone; visible focus indicators; respect reduced-motion. UI language and i18n stance are decided in the M01 PRD.
<!-- lang:end -->

<!-- lang:th -->
> ไฟล์นี้คือ product brief (ผู้ใช้ วัตถุประสงค์ แบรนด์) ส่วน **ระบบดีไซน์** —
> สี ตัวอักษร component และข้อควร/ไม่ควร — อยู่ที่ **`DESIGN.md`**
> แบรนด์และหลักการด้านล่างคือ "ทำไม" ส่วน DESIGN.md คือ "ทำอย่างไร"
> ข้อกำหนดที่เป็นทางการอยู่ใน `docs/plans/master_planV1.md` ไฟล์นี้แค่สรุป และไม่มีอำนาจเหนือมัน

## ผู้ใช้
เจ้าของคนเดียวที่รันบอทเพื่อใช้งานส่วนตัว (master plan §1.1) multi-user SaaS, ระบบสมาชิก, copy/social trading และแอปมือถือ native อยู่นอก scope [LOCKED, §1.4]

## วัตถุประสงค์ของผลิตภัณฑ์
บอทเทรดอัตโนมัติด้วย AI สำหรับ XAUUSD บน MT5 AI วิเคราะห์ข่าวและกราฟ คาดการณ์ เลือกและสลับกลยุทธ์ เปิดและบริหารไม้ รักษากำไร และเรียนรู้จากผลลัพธ์ ทั้งหมดอยู่ใต้ Risk Engine ที่แยกจาก AI ผู้ใช้ตั้ง Initial Lot, **Profit Target** เป็น USD คงที่ต่อ **Cycle**, drawdown สูงสุดเป็น % และควบคุมบอทผ่านเบราว์เซอร์

ถือว่าสำเร็จเมื่อ: เทรดบัญชี Demo อัตโนมัติและควบคุมผ่านเบราว์เซอร์ได้ คำนวณกำไรและ DD ถูกต้อง ปิดและกู้สถานะคืนได้หลัง restart ไม่แตะ **Manual Position** คำสั่งที่ถูกบล็อกต้องถูกบล็อกจริง และ DD lock ยังอยู่หลัง restart (§1.2) ไม่สัญญาว่าจะได้กำไร

## บุคลิกของแบรนด์
**สุขุม** ผลิตภัณฑ์นี้จัดการเงิน จึงไม่ตื่นเต้นกับการเทรด และไม่พูดเป็นนัยว่ารับประกันผลตอบแทน

**ตรวจสอบได้** ทุกการตัดสินใจแสดงเหตุผลและข้อมูลที่ใช้ เหตุการณ์สำคัญย้อนดูได้ภายหลัง

**นิ่งเมื่อมีปัญหา** เมื่อมีอะไรพัง ระบบบอกว่าอะไรยังค้างอยู่และกำลังทำอะไรกับมัน ไม่ซ่อนสถานะ

## สิ่งที่ไม่อยากเป็น
**แอป "สัญญาณเทรด" ที่โฆษณา win rate** — master plan ห้ามอ้างว่ารับประกันกำไรหรือ stop ได้ตรงเป๊ะ (§4.4)
**dashboard แบบเกม (พลุ, streak)** — มันให้รางวัลกับการเสี่ยง ตรงข้ามกับผลิตภัณฑ์ที่เอาความเสี่ยงมาก่อน

## หลักการออกแบบ
1. **ความเสี่ยงมาก่อนกำไร** — ความปลอดภัยและการปฏิเสธเมื่อไม่แน่ใจ สำคัญกว่ากำไรและความพร้อมใช้งาน (§0.1)
2. **ควบคุมง่าย เจาะลึกได้** — คำสั่งไม่กี่ตัวที่ชัดเจนอยู่หน้าแรก รายละเอียดเปิดดูเมื่อต้องการ (D31)
3. **แสดงเหตุผล ไม่ใช่แค่ตัวเลข** — การตัดสินใจลิงก์ไปยังหลักฐานที่ใช้ (§3.5)
4. **บอกสถานะตามจริง** — แสดงว่ายังค้าง/ยังไม่จบ แทนที่จะทำเหมือนสำเร็จแล้ว

## การเข้าถึงและความครอบคลุม
ตั้งเป้า WCAG 2.1 AA สำหรับ dashboard ไม่ใช้สีอย่างเดียวบอกกำไร/ขาดทุน มี focus indicator ที่มองเห็นได้ และเคารพ reduced-motion ภาษา UI และแนวทาง i18n ตัดสินใน PRD ของ M01
<!-- lang:end -->
