// The app shell (#73): sign-in, hash routes, the always-visible status bar, and the live
// feed. The session token lives in memory only: closing the tab signs out, and closing
// the browser never affects the bot (the dashboard is not safety-critical).
//
// Freshness: the status is re-read on a coalesced refresh after live events, and polled
// every STATUS_POLL_MS when the market is quiet; the clock ticks every second so an
// ageing status turns STALE on screen without waiting for an event.
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { API_BASE, type Api, ApiError, createApi } from "./api";
import { freshness } from "./freshness";
import { type FeedState, LiveFeed, type SocketLike } from "./live";
import type { Status } from "./types";
import { VIEWS } from "./views";

export const ROUTES = ["trading", "insights", "history", "settings"] as const;
export type Route = (typeof ROUTES)[number];
export const STATUS_MAX_AGE_MS = 30_000;
const STATUS_POLL_MS = 15_000;
const REFRESH_COALESCE_MS = 250;

export interface AppContextValue {
  api: Api;
  status: Status | null;
  feed: FeedState;
  /** Revision bumped (coalesced) by live events and reconnects: views refetch on it. */
  revision: number;
  refresh: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp outside the app");
  return ctx;
}

/** The current time, re-read every `everyMs`: time-based labels stay honest. */
export function useNow(everyMs = 1_000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), everyMs);
    return () => clearInterval(id);
  }, [everyMs]);
  return now;
}

function routeFromHash(hash: string): Route {
  const r = hash.replace(/^#\/?/, "");
  return (ROUTES as readonly string[]).includes(r) ? (r as Route) : "trading";
}

export interface AppProps {
  fetch?: typeof fetch;
  wsUrl?: string;
  socket?: (url: string) => SocketLike;
}

type SignedOut = "fresh" | "expired" | "signed-out";

export function App({ fetch, wsUrl, socket }: AppProps) {
  const [token, setToken] = useState<string | null>(null);
  const [signedOut, setSignedOut] = useState<SignedOut>("fresh");
  const end = useCallback((why: SignedOut) => {
    setToken(null);
    setSignedOut(why);
  }, []);
  const api = useMemo(
    () =>
      createApi({
        ...(fetch ? { fetch } : {}),
        token: () => token,
        onUnauthorized: () => end("expired"),
      }),
    [fetch, token, end],
  );
  if (!token) return <Login api={api} why={signedOut} onToken={setToken} />;
  return (
    <Shell
      api={api}
      token={token}
      wsUrl={wsUrl ?? defaultWsUrl()}
      socket={socket}
      onSignOut={() => end("signed-out")}
      onExpired={() => end("expired")}
    />
  );
}

function Login({ api, why, onToken }: { api: Api; why: SignedOut; onToken: (t: string) => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <main className="login">
      <h1>OrionFX</h1>
      {why === "expired" && <p role="status">Your session ended. Please sign in again.</p>}
      {why === "signed-out" && <p role="status">You signed out.</p>}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            onToken((await api.login(password)).token);
          } catch (err) {
            setError(
              err instanceof ApiError && err.kind === "rate_limited"
                ? "Too many failed attempts. Wait a minute."
                : "Sign-in failed.",
            );
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          Password
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        {error && <p role="alert">{error}</p>}
      </form>
    </main>
  );
}

function defaultWsUrl(): string {
  const origin = API_BASE || window.location.origin;
  return `${origin.replace(/^http/, "ws")}/ws/live`;
}

function Shell({
  api,
  token,
  wsUrl,
  socket,
  onSignOut,
  onExpired,
}: {
  api: Api;
  token: string;
  wsUrl: string;
  socket: ((url: string) => SocketLike) | undefined;
  onSignOut: () => void;
  onExpired: () => void;
}) {
  const [route, setRoute] = useState<Route>(() => routeFromHash(window.location.hash));
  const [status, setStatus] = useState<Status | null>(null);
  const [feed, setFeed] = useState<FeedState>("CONNECTING");
  const [revision, setRevision] = useState(0);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Many events in a burst (a replay, a busy tick) become one refresh.
  const refresh = useCallback(() => {
    if (pending.current) return;
    pending.current = setTimeout(() => {
      pending.current = null;
      setRevision((r) => r + 1);
    }, REFRESH_COALESCE_MS);
  }, []);
  useEffect(
    () => () => {
      if (pending.current) clearTimeout(pending.current);
    },
    [],
  );

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `revision` is the refetch signal
  useEffect(() => {
    let alive = true;
    api.status().then(
      (s) => alive && setStatus(s),
      () => alive && setStatus(null),
    );
    return () => {
      alive = false;
    };
  }, [api, revision]);

  useEffect(() => {
    const id = setInterval(refresh, STATUS_POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  // The feed starts once, from the journal position of the first status: no replay of
  // history the snapshots already reflect.
  const since = status?.latest_seq;
  const started = since !== undefined;
  // biome-ignore lint/correctness/useExhaustiveDependencies: start once, from the first `since`
  useEffect(() => {
    if (!started) return;
    const live = new LiveFeed({
      url: wsUrl,
      token,
      since,
      ...(socket ? { socket } : {}),
      onEvent: refresh,
      onState: (s) => {
        setFeed(s);
        if (s === "LIVE") refresh(); // (re)connected: re-read the snapshots
      },
      onUnauthorized: onExpired,
    });
    live.start();
    return () => live.stop();
  }, [started, token, wsUrl, socket, refresh, onExpired]);

  const value = useMemo(
    () => ({ api, status, feed, revision, refresh }),
    [api, status, feed, revision, refresh],
  );
  const View = VIEWS[route];
  return (
    <AppContext.Provider value={value}>
      <StatusBar status={status} feed={feed} />
      <nav aria-label="Sections">
        {ROUTES.map((r) => (
          <a key={r} href={`#/${r}`} aria-current={r === route ? "page" : undefined}>
            {r[0]?.toUpperCase() + r.slice(1)}
          </a>
        ))}
        <button
          type="button"
          className="quiet"
          onClick={() =>
            api
              .logout()
              .catch(() => undefined)
              .finally(onSignOut)
          }
        >
          Sign out
        </button>
      </nav>
      <main>
        <View />
      </main>
    </AppContext.Provider>
  );
}

const FEED_LABEL: Record<FeedState, string> = {
  LIVE: "Live",
  CONNECTING: "Connecting…",
  DISCONNECTED: "Disconnected",
};

export function StatusBar({ status, feed }: { status: Status | null; feed: FeedState }) {
  const now = useNow();
  const stale = freshness(status?.as_of ?? null, now, STATUS_MAX_AGE_MS) === "STALE";
  const label = feed === "LIVE" && stale ? "Stale" : FEED_LABEL[feed];
  return (
    <header className="statusbar">
      <span className={`pill ${label === "Live" ? "ok" : "warn"}`} role="status">
        {label}
      </span>
      {status && (
        <>
          <span className="pill">Mode: {status.account_mode}</span>
          <span className="pill">Bot: {status.control_state}</span>
          {status.risk_locked && <span className="pill danger">Risk locked</span>}
          {status.requires_reconcile && <span className="pill warn">Reconcile required</span>}
          <span className="muted">as of {new Date(status.as_of).toLocaleTimeString()}</span>
        </>
      )}
    </header>
  );
}
