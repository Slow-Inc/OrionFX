// Money arrives as decimal strings (the backend never uses floats). Format it with
// integer cents so no float drift creeps in, and always pair a gain or loss with a sign
// and a word: colour is never the only signal (DESIGN.md).

/** Plain digits for a decimal string, expanding exponent notation (Python's str() of a
 * Decimal can give "0E-8" or "1.5E+3"). */
function plain(value: string): string {
  const m = /^([-+]?)(\d*)(?:\.(\d*))?(?:[eE]([-+]?\d+))?$/.exec(value.trim());
  if (!m) throw new Error(`not a decimal: ${value}`);
  const [, sign = "", int = "", frac = "", exp = "0"] = m;
  const digits = `${int}${frac}` || "0";
  const point = int.length + Number(exp); // where the decimal point lands
  if (point <= 0) return `${sign}0.${"0".repeat(-point)}${digits}`;
  if (point >= digits.length) return `${sign}${digits}${"0".repeat(point - digits.length)}`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

function toCents(raw: string): bigint {
  const value = plain(raw);
  const negative = value.startsWith("-");
  const [whole = "0", frac = ""] = value.replace(/^[-+]/, "").split(".");
  const three = `${frac}000`.slice(0, 3);
  let cents = BigInt(whole) * 100n + BigInt(three.slice(0, 2));
  if (Number(three[2]) >= 5) cents += 1n; // round half up on the magnitude
  return negative ? -cents : cents;
}

function render(cents: bigint): string {
  const abs = cents < 0n ? -cents : cents;
  const whole = (abs / 100n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${whole}.${(abs % 100n).toString().padStart(2, "0")}`;
}

/** A price as plain decimal digits (not money: no grouping or currency rules). */
export function formatPrice(value: string | null | undefined): string {
  return formatPercent(value);
}

/** A decimal percent string for display ("0E-8" -> "0"), or "—" when not a decimal. */
export function formatPercent(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  try {
    const p = plain(value);
    return p.includes(".") ? p.replace(/\.?0+$/, "") : p;
  } catch {
    return "—";
  }
}

export function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  try {
    const cents = toCents(value);
    return (cents < 0n ? "−" : "") + render(cents);
  } catch {
    return "—"; // never crash a view over one malformed value
  }
}

export interface Signed {
  text: string;
  word: "gain" | "loss" | "flat";
  sign: -1 | 0 | 1;
}

/** A signed amount, or null when the value is not a decimal (shown as "—", never thrown). */
export function signed(value: string): Signed | null {
  let cents: bigint;
  try {
    cents = toCents(value);
  } catch {
    return null;
  }
  if (cents > 0n) return { text: `+${render(cents)}`, word: "gain", sign: 1 };
  if (cents < 0n) return { text: `−${render(cents)}`, word: "loss", sign: -1 };
  return { text: render(cents), word: "flat", sign: 0 };
}
