// History (#75; UI-04). Cycles with their final realized net and whether the Profit
// Target was triggered and achieved, reported separately: a target triggered but closed
// below it is "Triggered" and "Not achieved", never a win by implication (AT-02). A cycle
// that is not closed has no final net yet; a closed one without it is "Not yet known".
import { formatMoney } from "./format";
import { useSnapshot } from "./snapshot";
import type { Cycle } from "./types";
import { Money, ReadError } from "./ui";

function achieved(c: Cycle): string {
  if (c.target_achieved === null) return "Not yet known";
  return c.target_achieved ? "Achieved" : "Not achieved";
}

export function HistoryView() {
  const cycles = useSnapshot((api) => api.cycles(50));
  const items = cycles.data?.items ?? [];
  return (
    <>
      <h2>History</h2>
      {cycles.error !== null && <ReadError what="the history" />}
      {cycles.data && items.length === 0 && <p className="muted">No cycles yet.</p>}
      {items.length > 0 && (
        <table>
          <caption>Cycles, latest 50, newest first</caption>
          <thead>
            <tr>
              <th scope="col">Cycle</th>
              <th scope="col">State</th>
              <th scope="col">Profit Target</th>
              <th scope="col">Final realized net</th>
              <th scope="col">Target</th>
              <th scope="col">Result</th>
              <th scope="col">Closed by</th>
            </tr>
          </thead>
          <tbody>
            {items.map((c) => (
              <tr key={c.cycle_id ?? c.seq} aria-label={`Cycle ${c.cycle_id ?? c.seq}`}>
                <th scope="row">
                  <code>{c.cycle_id ?? "—"}</code>
                </th>
                <td>{c.state}</td>
                <td>{formatMoney(c.target_usd)}</td>
                <td>
                  <Money
                    value={c.final_net_usd}
                    empty={c.state === "CLOSED" ? "Not yet known" : "open"}
                  />
                </td>
                <td>{c.target_triggered ? "Triggered" : "Not triggered"}</td>
                <td>{achieved(c)}</td>
                <td>{c.close_reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
