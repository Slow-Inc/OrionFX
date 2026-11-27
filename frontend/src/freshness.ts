// Stale data must never look live (M01 acceptance). A missing time is STALE, and so is
// one further in the future than a small clock skew allows: only a recent observation
// is LIVE.
export type Freshness = "LIVE" | "STALE";

export const CLOCK_SKEW_MS = 5_000;

export function freshness(asOf: string | null, now: Date, maxAgeMs: number): Freshness {
  if (asOf === null) return "STALE";
  const age = now.getTime() - new Date(asOf).getTime();
  return age >= -CLOCK_SKEW_MS && age <= maxAgeMs ? "LIVE" : "STALE";
}
