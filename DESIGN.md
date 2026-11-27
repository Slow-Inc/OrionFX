---
status: drafted (M01 PRD #72)
owner: M01 Web Dashboard PRD
stack: Bun, Vite, React, TypeScript, plain CSS variables   # PRD-01 #72 (Tailwind dropped: fewer deps)
tokens: frontend/src/styles.css   # :root variables, light + dark
---

<!-- lang:en -->
# Design System

> The "how" behind `PRODUCT.md`'s "why". The visual system is drafted by the M01 Web
> Dashboard PRD (#72); tokens live in `frontend/src/styles.css`.

## Tokens (drafted)

- **Palette:** neutral surfaces, one accent (`--accent`), `--ok`, `--warn`, `--danger`;
  light and dark follow `prefers-color-scheme`. `--danger` is reserved for Emergency Stop
  and the risk lock.
- **Type:** the system UI font, 16px base, tabular numbers for money.
- **Shape:** 8px radius, 12px spacing unit, pills for status.
- **States:** Live / Stale / Disconnected / Connecting on the status bar; every panel shows
  its `as of` time.

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

> ไฟล์นี้คือ "ทำอย่างไร" ของ "ทำไม" ใน `PRODUCT.md` ระบบหน้าตาร่างไว้แล้วใน PRD ของ M01 Web
> Dashboard (#72) และ token อยู่ที่ `frontend/src/styles.css`

## Token (ฉบับร่าง)

- **ชุดสี:** พื้นผิวโทนกลาง สีเน้นหนึ่งสี (`--accent`) และ `--ok`, `--warn`, `--danger`
  ใช้ทั้งแบบสว่างและมืดตาม `prefers-color-scheme` โดย `--danger` สงวนไว้สำหรับ Emergency Stop
  และ risk lock เท่านั้น
- **ตัวอักษร:** ใช้ฟอนต์ UI ของระบบ ขนาดพื้นฐาน 16px และใช้ตัวเลขความกว้างเท่ากันสำหรับตัวเลขเงิน
- **รูปทรง:** มุมโค้ง 8px หน่วยระยะห่าง 12px และใช้ pill แสดงสถานะ
- **สถานะ:** แถบสถานะแสดง Live / Stale / Disconnected / Connecting และทุก panel แสดงเวลา
  `as of` ของตัวเอง

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
