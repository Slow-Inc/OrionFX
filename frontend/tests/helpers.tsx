// Render a view inside the app context with a fake API (shared by the view tests).
import { render } from "@testing-library/react";
import { vi } from "vitest";
import type { Api } from "../src/api";
import { AppContext, type AppContextValue } from "../src/app";
import type { Status } from "../src/types";

export function renderWithApp(ui: React.ReactElement, api: Api, status: Status | null = null) {
  const value: AppContextValue = { api, status, feed: "LIVE", revision: 0, refresh: vi.fn() };
  return render(<AppContext.Provider value={value}>{ui}</AppContext.Provider>);
}
