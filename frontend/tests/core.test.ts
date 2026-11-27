// M01 slice A (#73): freshness, money formatting, the typed API client and the live feed.
import { describe, expect, it, vi } from "vitest";
import { ApiError, createApi } from "../src/api";
import { formatMoney, signed } from "../src/format";
import { freshness } from "../src/freshness";
import { ACCEPT_GRACE_MS, LiveFeed, type SocketLike } from "../src/live";

describe("freshness", () => {
  const now = new Date("2026-01-05T12:00:30Z");
  it("is LIVE within the max age and STALE after it", () => {
    expect(freshness("2026-01-05T12:00:00Z", now, 60_000)).toBe("LIVE");
    expect(freshness("2026-01-05T11:58:00Z", now, 60_000)).toBe("STALE");
  });
  it("treats a missing or far-future timestamp as STALE, never LIVE", () => {
    expect(freshness(null, now, 60_000)).toBe("STALE");
    expect(freshness("2026-01-05T12:05:00Z", now, 60_000)).toBe("STALE");
  });
  it("tolerates a few seconds of server clock running ahead", () => {
    expect(freshness("2026-01-05T12:00:33Z", now, 60_000)).toBe("LIVE");
  });
});

describe("money", () => {
  it("shows a sign and a word, never colour alone", () => {
    expect(signed("12.40")).toEqual({ text: "+12.40", word: "gain", sign: 1 });
    expect(signed("-3.5")).toEqual({ text: "−3.50", word: "loss", sign: -1 });
    expect(signed("0")).toEqual({ text: "0.00", word: "flat", sign: 0 });
  });
  it("formats decimal strings without float drift", () => {
    expect(formatMoney("1000.1")).toBe("1,000.10");
    expect(formatMoney("1.005")).toBe("1.01"); // half up, on the magnitude
    expect(formatMoney("-1.005")).toBe("−1.01");
    expect(formatMoney(null)).toBe("—");
    expect(formatMoney("0E-8")).toBe("0.00"); // Python's str() of a tiny Decimal
    expect(formatMoney("1.5E+3")).toBe("1,500.00");
    expect(formatMoney("not money")).toBe("—"); // never crashes a view
  });
});

function fakeFetch(status: number, body: unknown) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

describe("api client", () => {
  it("sends the bearer token and parses JSON", async () => {
    const fetch = fakeFetch(200, { ok: true });
    const api = createApi({ fetch, token: () => "tok", onUnauthorized: vi.fn() });
    await api.status();
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/status");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer tok");
  });
  it("a 401 ends the session and raises an unauthorized error", async () => {
    const onUnauthorized = vi.fn();
    const api = createApi({ fetch: fakeFetch(401, {}), token: () => "t", onUnauthorized });
    await expect(api.status()).rejects.toMatchObject({ kind: "unauthorized" });
    expect(onUnauthorized).toHaveBeenCalled();
  });
  it("maps conflict, validation and unavailable responses to typed errors", async () => {
    const mk = (s: number, b: unknown) =>
      createApi({ fetch: fakeFetch(s, b), token: () => "t", onUnauthorized: vi.fn() });
    await expect(mk(409, { detail: "x" }).status()).rejects.toMatchObject({ kind: "conflict" });
    const err = await mk(422, { detail: [{ loc: ["trading", "initial_lot"], msg: "bad" }] })
      .status()
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).kind).toBe("validation");
    expect((err as ApiError).fields).toEqual({ "trading.initial_lot": "bad" });
    await expect(mk(503, "down").status()).rejects.toMatchObject({ kind: "unavailable" });
    await expect(mk(429, { detail: "wait" }).status()).rejects.toMatchObject({
      kind: "rate_limited",
    });
    const start = await mk(409, { detail: { missing: ["trading.initial_lot"] } })
      .status()
      .catch((e: unknown) => e);
    expect((start as ApiError).detail).toEqual({ missing: ["trading.initial_lot"] });
  });
  it("logs in without a token, and a failed sign-in is not a lost session", async () => {
    const fetch = fakeFetch(200, { token: "new", expires_at: "2026-01-06T00:00:00Z" });
    const api = createApi({ fetch, token: () => null, onUnauthorized: vi.fn() });
    expect((await api.login("pw")).token).toBe("new");
    const onUnauthorized = vi.fn();
    const bad = createApi({ fetch: fakeFetch(401, {}), token: () => null, onUnauthorized });
    await expect(bad.login("nope")).rejects.toMatchObject({ kind: "unauthorized" });
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

class FakeSocket implements SocketLike {
  static all: FakeSocket[] = [];
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.onclose?.({ code: 1000 });
  }
}

describe("live feed", () => {
  it("says hello with the token and the last seq, and tracks seq", () => {
    FakeSocket.all = [];
    const events: number[] = [];
    const feed = new LiveFeed({
      url: "ws://x/ws/live",
      token: "tok",
      socket: (u) => new FakeSocket(u),
      onEvent: (seq) => events.push(seq),
      onState: vi.fn(),
      onUnauthorized: vi.fn(),
      since: 5,
    });
    feed.start();
    const s = FakeSocket.all[0] as FakeSocket;
    s.onopen?.();
    expect(JSON.parse(s.sent[0] as string)).toEqual({ token: "tok", since: 5 });
    s.onmessage?.({ data: JSON.stringify({ seq: 6, event: { event_type: "x" } }) });
    s.onmessage?.({ data: JSON.stringify({ seq: 7, event: { event_type: "y" } }) });
    expect(events).toEqual([6, 7]);
    expect(feed.lastSeq).toBe(7);
    feed.stop();
  });

  it("reconnects after a drop and resumes from the last seq", () => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    const states: string[] = [];
    const feed = new LiveFeed({
      url: "ws://x/ws/live",
      token: "tok",
      socket: (u) => new FakeSocket(u),
      onEvent: vi.fn(),
      onState: (s) => states.push(s),
      onUnauthorized: vi.fn(),
    });
    feed.start();
    const first = FakeSocket.all[0] as FakeSocket;
    first.onopen?.();
    first.onmessage?.({ data: JSON.stringify({ seq: 9, event: {} }) });
    first.onclose?.({ code: 1006 });
    expect(states).toContain("DISCONNECTED");
    vi.advanceTimersByTime(5_000);
    const second = FakeSocket.all[1] as FakeSocket;
    second.onopen?.();
    expect(JSON.parse(second.sent[0] as string).since).toBe(9);
    expect(states.at(-1)).toBe("CONNECTING"); // open is not accepted: no "Live" yet
    vi.advanceTimersByTime(ACCEPT_GRACE_MS);
    expect(states.at(-1)).toBe("LIVE");
    feed.stop();
    vi.useRealTimers();
  });

  it("stops and reports when the session is refused (4401)", () => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    const onUnauthorized = vi.fn();
    const feed = new LiveFeed({
      url: "ws://x/ws/live",
      token: "tok",
      socket: (u) => new FakeSocket(u),
      onEvent: vi.fn(),
      onState: vi.fn(),
      onUnauthorized,
    });
    feed.start();
    (FakeSocket.all[0] as FakeSocket).onclose?.({ code: 4401 });
    expect(onUnauthorized).toHaveBeenCalled();
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.all).toHaveLength(1); // no reconnect after an auth refusal
    vi.useRealTimers();
  });

  it("a rejected hello (4400) is final and never shows Live", () => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    const states: string[] = [];
    const feed = new LiveFeed({
      url: "ws://x/ws/live",
      token: "tok",
      socket: (u) => new FakeSocket(u),
      onEvent: vi.fn(),
      onState: (s) => states.push(s),
      onUnauthorized: vi.fn(),
    });
    feed.start();
    const s = FakeSocket.all[0] as FakeSocket;
    s.onopen?.();
    s.onclose?.({ code: 4400 });
    vi.advanceTimersByTime(60_000);
    expect(states).not.toContain("LIVE");
    expect(FakeSocket.all).toHaveLength(1);
    vi.useRealTimers();
  });

  it("ignores malformed or seq-less frames without losing its position", () => {
    FakeSocket.all = [];
    const events: number[] = [];
    const feed = new LiveFeed({
      url: "ws://x/ws/live",
      token: "tok",
      socket: (u) => new FakeSocket(u),
      onEvent: (seq) => events.push(seq),
      onState: vi.fn(),
      onUnauthorized: vi.fn(),
      since: 3,
    });
    feed.start();
    const s = FakeSocket.all[0] as FakeSocket;
    s.onopen?.();
    s.onmessage?.({ data: "not json" });
    s.onmessage?.({ data: JSON.stringify({ event: {} }) });
    s.onmessage?.({ data: JSON.stringify({ seq: 4, event: {} }) });
    expect(events).toEqual([4]);
    expect(feed.lastSeq).toBe(4);
    feed.stop();
  });
});
