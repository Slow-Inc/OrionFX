// Stale data must never look live (M01 acceptance). A missing time, or one in the
// future (clock skew), is STALE: only a recent, past observation is LIVE.
export type Freshness = "LIVE" | "STALE";

export function freshness(asOf: string | null, now: Date, maxAgeMs: number): Freshness {
  if (asOf === null) return "STALE";
  const age = now.getTime() - new Date(asOf).getTime();
  return age >= 0 && age <= maxAgeMs ? "LIVE" : "STALE";
}
