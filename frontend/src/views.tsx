// Route views (M01 #72).
import type { Route } from "./app";
import { HistoryView } from "./history";
import { InsightsView } from "./insights";
import { SettingsView } from "./settings";
import { TradingView } from "./trading";

export const VIEWS: Record<Route, () => React.ReactElement> = {
  trading: TradingView,
  insights: InsightsView,
  history: HistoryView,
  settings: SettingsView,
};
