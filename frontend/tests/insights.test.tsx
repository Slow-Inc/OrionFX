// M01 slice C (#75): AI Insights and History report what happened honestly; Settings
// validates, keeps secrets masked, handles conflicts, and never offers Live by a toggle.
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Api } from "../src/api";
import { ApiError } from "../src/api";
import { AppContext, type AppContextValue } from "../src/app";
import { HistoryView } from "../src/history";
import { InsightsView } from "../src/insights";
import { SettingsView } from "../src/settings";
import type { Cycle, Decision, Prediction, Settings } from "../src/types";

const NOW = new Date().toISOString();
const LATER = new Date(Date.now() + 3_600_000).toISOString();

const DECISION: Decision = {
  seq: 5,
  decision_id: "dec-1",
  action: "WAIT",
  strategy_id: "trend_following",
  side: null,
  expected_value_usd: null,
  confidence: null,
  reason_codes: ["EV_UNKNOWN"],
  evidence_refs: ["m02.features.v1@2026-01-05T12:00:00+00:00", "pred-1"],
  policy_version: "p1",
};

const PREDICTION: Prediction = {
  seq: 4,
  prediction_id: "pred-1",
  model_id: "baseline",
  model_version: "1",
  lineage: "CHART_ONLY",
  horizon: "H1",
  horizon_bars: 1,
  direction: "UP",
  probability: { value: "0.5", semantics: "uncalibrated baseline score", calibrated: false },
  data_quality: "OK",
  generated_at: NOW,
  valid_until: LATER,
};

const CLOSED: Cycle = {
  cycle_id: "cyc-1",
  seq: 9,
  state: "CLOSED",
  target_usd: "10",
  cycle_net_estimated_usd: null,
  peak_net_usd: "10.2",
  trailing_floor_usd: null,
  trailing_active: false,
  final_net_usd: "9.80",
  close_reason: "TARGET_TRIGGERED",
  target_triggered: true,
  target_achieved: false,
  unresolved: [],
};

const SETTINGS: Settings = {
  version: 3,
  config: {
    trading: {
      symbol: "XAUUSD",
      initial_lot: "0.01",
      profit_target_usd: "10",
      max_drawdown_percent: "20",
      order_scope: "BOT_ONLY",
      drawdown_scope: "ACCOUNT_EQUITY",
      account_mode: "DEMO",
      risk_profile: "LOW",
      strategies_enabled: ["trend_following"],
    },
    llm: { provider: "anthropic", model: "m", api_key_ref: "***", news_handling: "adaptive" },
    system: {
      deployment: "local",
      storage: "local",
      bind_host: "127.0.0.1",
      dashboard_origin: "http://127.0.0.1:5173",
    },
  },
  protected: { policy_version: "policy-0.1", live_allowed: false },
  missing_for_start: [],
};

function fakeApi(over: Partial<Api> = {}): Api {
  return {
    decisions: vi.fn(async () => ({ as_of: NOW, items: [DECISION] })),
    predictions: vi.fn(async () => ({ as_of: NOW, items: [PREDICTION] })),
    cycles: vi.fn(async () => ({ as_of: NOW, items: [CLOSED] })),
    settings: vi.fn(async () => SETTINGS),
    saveSettings: vi.fn(async () => ({ version: 4 })),
    ...over,
  } as unknown as Api;
}

function renderIn(ui: React.ReactElement, api: Api) {
  const value: AppContextValue = { api, status: null, feed: "LIVE", revision: 0, refresh: vi.fn() };
  return render(<AppContext.Provider value={value}>{ui}</AppContext.Provider>);
}

// --- Insights --------------------------------------------------------------------------------

describe("AI Insights", () => {
  it("shows every decision with its action, strategy, reasons and evidence", async () => {
    renderIn(<InsightsView />, fakeApi());
    const decisions = await screen.findByRole("region", { name: "Decisions" });
    expect(await within(decisions).findByText("WAIT")).toBeInTheDocument();
    expect(within(decisions).getByText("trend_following")).toBeInTheDocument();
    expect(within(decisions).getByText("EV_UNKNOWN")).toBeInTheDocument();
    expect(within(decisions).getByText("pred-1")).toBeInTheDocument();
  });

  it("states what a forecast score means and marks it uncalibrated", async () => {
    renderIn(<InsightsView />, fakeApi());
    const forecasts = await screen.findByRole("region", { name: "Forecasts" });
    expect(await within(forecasts).findByText(/uncalibrated baseline score/)).toBeInTheDocument();
    expect(within(forecasts).getByText("Uncalibrated")).toBeInTheDocument();
  });

  it("marks an expired forecast as expired", async () => {
    const old = { ...PREDICTION, valid_until: "2020-01-01T00:00:00Z" };
    renderIn(
      <InsightsView />,
      fakeApi({ predictions: vi.fn(async () => ({ as_of: NOW, items: [old] })) }),
    );
    const forecasts = await screen.findByRole("region", { name: "Forecasts" });
    expect(await within(forecasts).findByText("Expired")).toBeInTheDocument();
  });
});

// --- History ---------------------------------------------------------------------------------

describe("History", () => {
  it("reports a triggered-but-missed target honestly", async () => {
    renderIn(<HistoryView />, fakeApi());
    const row = await screen.findByRole("row", { name: /cyc-1/ });
    expect(within(row).getByText("+9.80")).toBeInTheDocument();
    expect(within(row).getByText("Triggered")).toBeInTheDocument();
    expect(within(row).getByText("Not achieved")).toBeInTheDocument();
  });

  it("says when there is no history yet", async () => {
    renderIn(<HistoryView />, fakeApi({ cycles: vi.fn(async () => ({ as_of: NOW, items: [] })) }));
    expect(await screen.findByText(/No cycles yet/)).toBeInTheDocument();
  });
});

// --- Settings --------------------------------------------------------------------------------

describe("Settings", () => {
  it("saves edits with the version it read and echoes masked secrets unchanged", async () => {
    const api = fakeApi();
    renderIn(<SettingsView />, api);
    const lot = await screen.findByLabelText("Initial Lot");
    await userEvent.clear(lot);
    await userEvent.type(lot, "0.02");
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    expect(api.saveSettings).toHaveBeenCalledTimes(1);
    const [version, config] = (api.saveSettings as ReturnType<typeof vi.fn>).mock.calls[0] as [
      number,
      Settings["config"],
    ];
    expect(version).toBe(3);
    expect(config.trading.initial_lot).toBe("0.02");
    expect(config.llm.api_key_ref).toBe("***"); // untouched secret: still the mask
    expect(await screen.findByRole("status")).toHaveTextContent("Saved (version 4)");
  });

  it("never shows a secret and replaces it only when typed", async () => {
    const api = fakeApi();
    renderIn(<SettingsView />, api);
    const key = await screen.findByLabelText(/API key reference/);
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("type", "password");
    await userEvent.type(key, "env:ANTHROPIC_API_KEY");
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    const config = (api.saveSettings as ReturnType<typeof vi.fn>).mock
      .calls[0]?.[1] as Settings["config"];
    expect(config.llm.api_key_ref).toBe("env:ANTHROPIC_API_KEY");
  });

  it("offers Live only as a disabled, explained option", async () => {
    renderIn(<SettingsView />, fakeApi());
    const mode = await screen.findByLabelText("Account mode");
    const live = within(mode).getByRole("option", { name: /Live/ });
    expect(live).toBeDisabled();
    expect(screen.getByText(/Live needs a passed qualification/)).toBeInTheDocument();
  });

  it("shows validation errors next to their field", async () => {
    const api = fakeApi({
      saveSettings: vi.fn(async () => {
        throw new ApiError("validation", 422, "HTTP 422", {
          "trading.initial_lot": "Input should be greater than 0",
        });
      }),
    });
    renderIn(<SettingsView />, api);
    await userEvent.click(await screen.findByRole("button", { name: "Save settings" }));
    expect(await screen.findByText("Input should be greater than 0")).toBeInTheDocument();
  });

  it("asks to reload when someone else changed the settings first", async () => {
    const api = fakeApi({
      saveSettings: vi.fn(async () => {
        throw new ApiError("conflict", 409, "stale version");
      }),
    });
    renderIn(<SettingsView />, api);
    await userEvent.click(await screen.findByRole("button", { name: "Save settings" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/changed elsewhere/);
    expect(screen.getByRole("button", { name: "Reload settings" })).toBeInTheDocument();
  });

  it("sends an emptied number as unset, not as zero", async () => {
    const api = fakeApi();
    renderIn(<SettingsView />, api);
    await userEvent.clear(await screen.findByLabelText("Profit Target (USD)"));
    await userEvent.click(screen.getByRole("button", { name: "Save settings" }));
    const config = (api.saveSettings as ReturnType<typeof vi.fn>).mock
      .calls[0]?.[1] as Settings["config"];
    expect(config.trading.profit_target_usd).toBeNull();
  });
});
