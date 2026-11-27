// M01 slice A (#73): the shell. Login, status bar, session end, stale never shown as live.
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App, StatusBar } from "../src/app";
import type { SocketLike } from "../src/live";
import type { Status } from "../src/types";

const STATUS: Status = {
  as_of: new Date().toISOString(),
  control_state: "STOPPED",
  control_state_seq: null,
  risk_locked: true,
  risk_lock_seq: 3,
  requires_reconcile: false,
  config_version: 1,
  account_mode: "DEMO",
  missing_for_start: [],
  live_allowed: false,
  health: {},
  latest_seq: 3,
};

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status });
}

class QuietSocket implements SocketLike {
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  send() {}
  close() {}
}

describe("app shell", () => {
  it("signs in, then shows the status bar with mode, bot state and the risk lock", async () => {
    const fetch = vi.fn(async (url: string) =>
      url.endsWith("/api/auth/login")
        ? json(200, { token: "t", expires_at: "x" })
        : json(200, STATUS),
    ) as unknown as typeof globalThis.fetch;
    render(<App fetch={fetch} socket={() => new QuietSocket()} wsUrl="ws://x" />);
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Mode: DEMO")).toBeInTheDocument();
    expect(screen.getByText("Risk locked")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Trading" })).toHaveAttribute("aria-current", "page");
  });

  it("returns to sign-in when the session ends (401)", async () => {
    let signedIn = false;
    const fetch = vi.fn(async (url: string) => {
      if (url.endsWith("/api/auth/login")) {
        signedIn = true;
        return json(200, { token: "t", expires_at: "x" });
      }
      return signedIn ? json(401, { detail: "authentication required" }) : json(200, STATUS);
    }) as unknown as typeof globalThis.fetch;
    render(<App fetch={fetch} socket={() => new QuietSocket()} wsUrl="ws://x" />);
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await act(async () => {
      await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    });
    expect(await screen.findByText(/session ended/)).toBeInTheDocument();
  });

  it("shows a failed sign-in without leaking why", async () => {
    const fetch = vi.fn(async () =>
      json(401, { detail: "invalid password" }),
    ) as unknown as typeof globalThis.fetch;
    render(<App fetch={fetch} socket={() => new QuietSocket()} wsUrl="ws://x" />);
    await userEvent.type(screen.getByLabelText("Password"), "bad");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-in failed.");
    expect(screen.queryByText(/session ended/)).not.toBeInTheDocument();
  });
});

describe("sign out", () => {
  it("says you signed out, not that the session ended", async () => {
    const fetch = vi.fn(async (url: string) =>
      url.endsWith("/api/auth/login")
        ? json(200, { token: "t", expires_at: "x" })
        : json(200, STATUS),
    ) as unknown as typeof globalThis.fetch;
    render(<App fetch={fetch} socket={() => new QuietSocket()} wsUrl="ws://x" />);
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByText("You signed out.")).toBeInTheDocument();
    expect(screen.queryByText(/session ended/)).not.toBeInTheDocument();
  });
});

describe("status bar", () => {
  it("never says Live for an old status or a dropped feed", () => {
    const old = { ...STATUS, as_of: "2020-01-01T00:00:00Z" };
    const { rerender } = render(<StatusBar status={old} feed="LIVE" />);
    expect(screen.getByRole("status")).toHaveTextContent("Stale");
    rerender(<StatusBar status={STATUS} feed="DISCONNECTED" />);
    expect(screen.getByRole("status")).toHaveTextContent("Disconnected");
    rerender(<StatusBar status={STATUS} feed="LIVE" />);
    expect(screen.getByRole("status")).toHaveTextContent("Live");
  });
});
