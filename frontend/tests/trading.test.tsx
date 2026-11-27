// M01 slice B (#74): commands stay pending until M06 says otherwise; the Trading view
// keeps Account DD apart from the Cycle Net estimate, never shows stale as live, and
// confirms every control (Emergency Stop by typing).
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Api } from "../src/api";
import { ApiError } from "../src/api";
import { runCommand } from "../src/commands";
import { TradingView } from "../src/trading";
import type { Account, CommandResult, Cycle, Status } from "../src/types";
import { renderWithApp } from "./helpers";

const NOW = new Date().toISOString();

const STATUS: Status = {
  as_of: NOW,
  control_state: "RUNNING",
  control_state_seq: 4,
  risk_locked: false,
  risk_lock_seq: null,
  requires_reconcile: false,
  config_version: 2,
  account_mode: "DEMO",
  missing_for_start: [],
  live_allowed: false,
  health: {},
  latest_seq: 9,
  risk: {
    epoch_dd_percent: "4.25",
    lifetime_dd_percent: "6.10",
    epoch_ceiling_percent: "20",
    lifetime_ceiling_percent: "30",
    reason_codes: [],
    policy_version: "p",
    observed_at: NOW,
  },
};

const ACCOUNT: Account = {
  as_of: NOW,
  stale: false,
  snapshot: {
    currency: "USD",
    balance: "1000.00",
    equity: "985.50",
    margin: "40.00",
    free_margin: "945.50",
    connected: true,
    quote_status: "OK",
    observed_at: NOW,
  },
};

const CYCLE: Cycle = {
  cycle_id: "cyc-1",
  seq: 8,
  state: "ACTIVE",
  target_usd: "10",
  cycle_net_estimated_usd: "-3.20",
  peak_net_usd: "1.10",
  trailing_floor_usd: null,
  trailing_active: false,
  final_net_usd: null,
  close_reason: null,
  target_triggered: false,
  target_achieved: null,
  unresolved: [],
};

function fakeApi(over: Partial<Api> = {}): Api {
  return {
    account: vi.fn(async () => ACCOUNT),
    cycles: vi.fn(async () => ({ as_of: NOW, items: [CYCLE] })),
    positions: vi.fn(async () => ({
      as_of: NOW,
      observed_at: NOW,
      stale: false,
      items: [
        {
          position_id: "p-1",
          symbol: "XAUUSD",
          side: "BUY" as const,
          volume: "0.01",
          open_price: "2000.00",
          profit: "-1.25",
          commission: "-0.04",
          swap: "0",
          cycle_id: "cyc-1",
        },
      ],
    })),
    command: vi.fn(async () => ({ command_id: "cmd-1", status: "ACCEPTED" as const })),
    commandStatus: vi.fn(async () => ({ command_id: "cmd-1", status: "ACCEPTED", unresolved: [] })),
    ...over,
  } as unknown as Api;
}

const renderTrading = (api: Api, status: Status | null = STATUS) =>
  renderWithApp(<TradingView />, api, status);

// --- runCommand ---------------------------------------------------------------------------------

describe("runCommand", () => {
  const sleep = async () => {};

  it("reports PENDING until M06 completes the command, then its outcome", async () => {
    const replies: CommandResult[] = [
      { command_id: "c", status: "ACCEPTED", unresolved: [] },
      { command_id: "c", status: "ACCEPTED", unresolved: [] },
      { command_id: "c", status: "COMPLETED", unresolved: [], resulting_state: "PAUSED" },
    ];
    const api = fakeApi({ commandStatus: vi.fn(async () => replies.shift() as CommandResult) });
    const seen: string[] = [];
    const final = await runCommand(api, "pause", { onUpdate: (s) => seen.push(s.phase), sleep });
    expect(seen).toEqual(["SENDING", "PENDING", "COMPLETED"]);
    expect(final.result?.resulting_state).toBe("PAUSED");
  });

  it("an emergency stop with unresolved positions is never shown as done", async () => {
    const api = fakeApi({
      commandStatus: vi.fn(async () => ({
        command_id: "c",
        status: "UNRESOLVED" as const,
        unresolved: ["pos-7"],
      })),
    });
    const final = await runCommand(api, "emergency-stop", { onUpdate: () => {}, sleep });
    expect(final.phase).toBe("UNRESOLVED");
    expect(final.result?.unresolved).toEqual(["pos-7"]);
  });

  it("a timeout reads as still pending, never as done", async () => {
    const final = await runCommand(fakeApi(), "start", {
      onUpdate: () => {},
      sleep,
      timeoutPolls: 3,
    });
    expect(final.phase).toBe("STILL_PENDING");
  });

  it("a refused send (missing settings) is reported with what is missing", async () => {
    const api = fakeApi({
      command: vi.fn(async () => {
        throw new ApiError("conflict", 409, "HTTP 409", {}, { missing: ["trading.initial_lot"] });
      }),
    });
    const final = await runCommand(api, "start", { onUpdate: () => {}, sleep });
    expect(final.phase).toBe("REFUSED");
    expect(final.message).toContain("trading.initial_lot");
  });
});

// --- Trading view --------------------------------------------------------------------------------

describe("Trading view", () => {
  it("shows Account DD apart from the Cycle Net, which is labelled an estimate", async () => {
    renderTrading(fakeApi());
    const account = await screen.findByRole("region", { name: "Account" });
    expect(within(account).getByText("985.50")).toBeInTheDocument();
    expect(within(account).getByText("4.25% of 20% limit")).toBeInTheDocument();
    const cycle = screen.getByRole("region", { name: "Current cycle" });
    expect(within(cycle).getByText(/Cycle Net \(estimate\)/)).toBeInTheDocument();
    expect(within(cycle).getByText("−3.20")).toBeInTheDocument();
    expect(within(cycle).getByText("loss")).toBeInTheDocument(); // a word, not colour alone
  });

  it("marks a stale account snapshot as stale", async () => {
    renderTrading(fakeApi({ account: vi.fn(async () => ({ ...ACCOUNT, stale: true })) }));
    const account = await screen.findByRole("region", { name: "Account" });
    expect(await within(account).findByText("Stale")).toBeInTheDocument();
  });

  it("shows unknown DD as unknown, never as 0", async () => {
    renderTrading(fakeApi(), { ...STATUS, risk: null });
    const account = await screen.findByRole("region", { name: "Account" });
    expect(within(account).getByText("Unknown")).toBeInTheDocument();
  });

  it("lists the bot's positions with signed floating profit and freshness", async () => {
    renderTrading(fakeApi());
    const row = await screen.findByRole("row", { name: "Position p-1" });
    expect(within(row).getByText("BUY")).toBeInTheDocument();
    expect(within(row).getByText("−1.25")).toBeInTheDocument();
    expect(within(row).getByText("loss")).toBeInTheDocument();
  });

  it("before any report, positions are 'not yet known', never 'none'", async () => {
    renderTrading(
      fakeApi({
        positions: vi.fn(async () => ({ as_of: NOW, observed_at: null, stale: true, items: [] })),
      }),
    );
    const card = await screen.findByRole("region", { name: "Bot positions" });
    expect(await within(card).findByText(/not yet known/)).toBeInTheDocument();
    expect(within(card).queryByText("No open bot positions.")).not.toBeInTheDocument();
  });

  it("says when the bot holds no positions, and marks a stale list", async () => {
    renderTrading(
      fakeApi({
        positions: vi.fn(async () => ({
          as_of: NOW,
          observed_at: "2020-01-01T00:00:00Z",
          stale: true,
          items: [],
        })),
      }),
    );
    const card = await screen.findByRole("region", { name: "Bot positions" });
    expect(await within(card).findByText("No open bot positions.")).toBeInTheDocument();
    expect(within(card).getByText("Stale")).toBeInTheDocument();
  });

  it("a failed read says so instead of looking like no data", async () => {
    renderTrading(
      fakeApi({
        cycles: vi.fn(async () => {
          throw new ApiError("unavailable", 503, "storage unavailable");
        }),
      }),
    );
    const cycle = await screen.findByRole("region", { name: "Current cycle" });
    expect(await within(cycle).findByRole("alert")).toHaveTextContent(/Could not read/);
    expect(within(cycle).queryByText("No cycle yet.")).not.toBeInTheDocument();
  });

  it("shows how fresh the Account DD is, and formats exponent strings", async () => {
    const old = {
      ...STATUS,
      risk: {
        ...(STATUS.risk as NonNullable<Status["risk"]>),
        epoch_dd_percent: "0E-8",
        observed_at: "2020-01-01T00:00:00Z",
      },
    };
    renderTrading(fakeApi(), old);
    const account = await screen.findByRole("region", { name: "Account" });
    expect(within(account).getByText("0% of 20% limit")).toBeInTheDocument();
    expect(within(account).getAllByText("Stale").length).toBeGreaterThan(0);
  });

  it("turns every control off while the bot state is unknown", async () => {
    renderTrading(fakeApi(), null);
    for (const name of ["Start", "Pause", "Finish Cycle"]) {
      expect(await screen.findByRole("button", { name })).toBeDisabled();
    }
  });

  it("only one command at a time: an open confirm is disabled while one is pending", async () => {
    const api = fakeApi();
    renderTrading(api, { ...STATUS, control_state: "PAUSED" });
    await userEvent.click(await screen.findByRole("button", { name: "Start" }));
    await userEvent.click(screen.getByRole("button", { name: "Finish Cycle" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm Start" }));
    expect(await screen.findByText(/Pending/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm Finish Cycle" })).toBeDisabled();
  });

  it("cancelling an Emergency Stop clears the typed word", async () => {
    renderTrading(fakeApi());
    const zone = await screen.findByRole("region", { name: "Emergency" });
    await userEvent.click(within(zone).getByRole("button", { name: "Emergency Stop" }));
    await userEvent.type(within(zone).getByLabelText(/Type STOP/), "STOP");
    await userEvent.click(within(zone).getByRole("button", { name: "Cancel" }));
    await userEvent.click(within(zone).getByRole("button", { name: "Emergency Stop" }));
    expect(within(zone).getByRole("button", { name: "Confirm Emergency Stop" })).toBeDisabled();
  });

  it("asks before a regular command and only sends after confirming", async () => {
    const api = fakeApi();
    renderTrading(api);
    await userEvent.click(await screen.findByRole("button", { name: "Pause" }));
    expect(api.command).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Confirm Pause" }));
    expect(api.command).toHaveBeenCalledWith("pause");
    expect(await screen.findByText(/Pending/)).toBeInTheDocument();
  });

  it("Emergency Stop is separate and needs the word STOP typed", async () => {
    const api = fakeApi();
    renderTrading(api);
    const zone = await screen.findByRole("region", { name: "Emergency" });
    await userEvent.click(within(zone).getByRole("button", { name: "Emergency Stop" }));
    const confirm = within(zone).getByRole("button", { name: "Confirm Emergency Stop" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(zone).getByLabelText(/Type STOP/), "STOP");
    await userEvent.click(confirm);
    expect(api.command).toHaveBeenCalledWith("emergency-stop");
  });

  it("disables Start and says why when settings are missing", async () => {
    renderTrading(fakeApi(), {
      ...STATUS,
      control_state: "STOPPED",
      missing_for_start: ["trading.initial_lot"],
    });
    expect(await screen.findByRole("button", { name: "Start" })).toBeDisabled();
    expect(screen.getByText(/trading.initial_lot/)).toBeInTheDocument();
  });

  it("offers recovery approval only while risk-locked", async () => {
    renderTrading(fakeApi(), { ...STATUS, risk_locked: true, control_state: "RISK_LOCKED" });
    expect(await screen.findByRole("button", { name: "Approve Recovery" })).toBeInTheDocument();
  });
});

describe("runCommand stops", () => {
  it("when the view goes away or the session ends", async () => {
    const controller = new AbortController();
    const api = fakeApi();
    let polls = 0;
    const final = await runCommand(api, "pause", {
      onUpdate: () => {},
      signal: controller.signal,
      sleep: async () => {
        polls += 1;
        if (polls === 2) controller.abort();
      },
    });
    expect(polls).toBe(2);
    expect(final.phase).toBe("STILL_PENDING");
    const signedOut = fakeApi({
      commandStatus: vi.fn(async () => {
        throw new ApiError("unauthorized", 401, "auth");
      }),
    });
    let n = 0;
    await runCommand(signedOut, "pause", {
      onUpdate: () => {},
      sleep: async () => {
        n += 1;
      },
    });
    expect(n).toBe(1);
  });
});
