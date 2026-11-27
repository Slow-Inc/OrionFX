// Money arrives as decimal strings (the backend never uses floats). Format it with
// integer cents so no float drift creeps in, and always pair a gain or loss with a sign
// and a word: colour is never the only signal (DESIGN.md).

function toCents(value: string): bigint {
  const negative = value.trim().startsWith("-");
  const [whole = "0", frac = ""] = value.trim().replace(/^[-+]/, "").split(".");
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

export function formatMoney(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const cents = toCents(value);
  return (cents < 0n ? "−" : "") + render(cents);
}

export interface Signed {
  text: string;
  word: "gain" | "loss" | "flat";
  sign: -1 | 0 | 1;
}

export function signed(value: string): Signed {
  const cents = toCents(value);
  if (cents > 0n) return { text: `+${render(cents)}`, word: "gain", sign: 1 };
  if (cents < 0n) return { text: `−${render(cents)}`, word: "loss", sign: -1 };
  return { text: render(cents), word: "flat", sign: 0 };
}
