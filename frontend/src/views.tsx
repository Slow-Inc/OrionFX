// Route views. Trading (#74), Insights, History and Settings (#75) replace these.
import type { Route } from "./app";

function NotYet({ name }: { name: string }) {
  return (
    <section aria-labelledby="h">
      <h2 id="h">{name}</h2>
      <p className="muted">This view is not built yet.</p>
    </section>
  );
}

export const VIEWS: Record<Route, () => React.ReactElement> = {
  trading: () => <NotYet name="Trading" />,
  insights: () => <NotYet name="AI Insights" />,
  history: () => <NotYet name="History" />,
  settings: () => <NotYet name="Settings" />,
};
