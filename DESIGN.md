---
status: not-yet-designed
owner: M01 Web Dashboard PRD
stack_proposed: React, TypeScript, Tailwind CSS, Lightweight Charts   # master plan §5.2 [PROPOSED]
tokens: {}   # filled when the M01 PRD fixes the visual system
---

<!-- lang:en -->
# Design System

> The "how" behind `PRODUCT.md`'s "why". **No visual system has been decided yet** — tokens,
> typography and components are owned by the M01 Web Dashboard PRD (`docs/prd/PRD-01-web-dashboard.md`, not yet written).
> Until then, only the constraints below are binding.

## Binding constraints (from PRODUCT.md and the master plan)

- **Profit/loss never by colour alone** — pair colour with sign, icon or label.
- **Control commands are distinct and unambiguous** — **Emergency Stop** is visually separated from **Pause** and **Finish Cycle**, and never one mis-click away from **Start**.
- **Pending and unresolved states are first-class** — a command that has not been confirmed by MT5 shows as pending, never as done.
- **No celebratory motion** on wins; motion respects `prefers-reduced-motion`.
- **The dashboard is not safety-critical** — no safety behaviour may depend on the page being open.

## Do / Don't

- **Do** show every AI decision with its reason and evidence link.
- **Don't** show projected profit as if it were realized; label **Cycle Net** as an estimate.
<!-- lang:end -->

<!-- lang:th -->
# Design System

> ไฟล์นี้คือ "ทำอย่างไร" ของ "ทำไม" ใน `PRODUCT.md` **ตอนนี้ยังไม่ได้ตัดสินใจเรื่องหน้าตา** —
> token ตัวอักษร และ component เป็นของ PRD ของ M01 Web Dashboard (`docs/prd/PRD-01-web-dashboard.md` ยังไม่ได้เขียน)
> ระหว่างนี้ มีผลบังคับแค่ข้อจำกัดด้านล่าง

## ข้อจำกัดที่บังคับใช้ (จาก PRODUCT.md และ master plan)

- **ห้ามใช้สีอย่างเดียวบอกกำไร/ขาดทุน** — ใช้สีคู่กับเครื่องหมาย ไอคอน หรือข้อความ
- **คำสั่งควบคุมต้องแยกกันชัด ไม่กำกวม** — **Emergency Stop** ต้องแยกจาก **Pause** และ **Finish Cycle** ให้เห็นชัด และต้องไม่อยู่ห่างจาก **Start** แค่คลิกพลาดครั้งเดียว
- **สถานะค้างและยังไม่จบต้องแสดงเป็นหลัก** — คำสั่งที่ MT5 ยังไม่ยืนยันต้องแสดงว่ากำลังรอ ห้ามแสดงว่าเสร็จแล้ว
- **ไม่มีแอนิเมชันฉลอง** ตอนชนะ และการเคลื่อนไหวต้องเคารพ `prefers-reduced-motion`
- **dashboard ไม่ใช่ส่วน safety-critical** — พฤติกรรมด้านความปลอดภัยต้องไม่ขึ้นกับว่าหน้าเว็บเปิดอยู่หรือไม่

## ควร / ไม่ควร

- **ควร** แสดงทุกการตัดสินใจของ AI พร้อมเหตุผลและลิงก์ไปยังหลักฐาน
- **ไม่ควร** แสดงกำไรที่คาดการณ์เหมือนเป็นกำไรที่ได้จริงแล้ว ให้ระบุว่า **Cycle Net** เป็นค่าประมาณ
<!-- lang:end -->
