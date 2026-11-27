// Shared presentation rules (DESIGN.md): money is signed and worded, never colour alone;
// a failed read says so, never looks like "no data".
import { signed } from "./format";

export function Money({ value, empty = "—" }: { value: string | null; empty?: string }) {
  const s = value === null ? null : signed(value);
  if (s === null) return <span className="muted">{value === null ? empty : "—"}</span>;
  return (
    <span className={`money ${s.word}`}>
      <span>{s.text}</span> <span className="muted">{s.word}</span>
    </span>
  );
}

export function ReadError({ what = "this" }: { what?: string }) {
  return <p role="alert">Could not read {what} from the bot. Retrying…</p>;
}
