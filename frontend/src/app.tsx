// The app shell (#73): login, hash routes, the always-visible status bar, and the live
// feed. The session token lives in memory only: closing the tab logs out, and closing
// the browser never affects the bot (the dashboard is not safety-critical).
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { type Api, ApiError, createApi } from "./api";
import { freshness } from "./freshness";
import { type FeedState, LiveFeed, type SocketLike } from "./live";
import type { Status } from "./types";

export const ROUTES = ["trading", "insights", "history", "settings"] as const;
export type Route = (typeof ROUTES)[number];
const STATUS_MAX_AGE_MS = 30_000;

export interface AppContextValue {
  api: Api;
  status: Status | null;
  feed: FeedState;
  /** Bumped on every live event and reconnect: views refetch their snapshots. */
  tick: number;
  refresh: () => void;
}

const AppContext = createContext<AppContextValue | null>(null);

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp outside the app");
  return ctx;
}

function routeFromHash(hash: string): Route {
  const r = hash.replace(/^#\/?/, "");
  return (ROUTES as readonly string[]).includes(r) ? (r as Route) : "trading";
}

export interface AppProps {
  fetch?: typeof fetch;
  wsUrl?: string;
  socket?: (url: string) => SocketLike;
  views: Record<Route, () => React.ReactElement>;
}

export function App({ fetch, wsUrl, socket, views }: AppProps) {
  const [token, setToken] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const logout = useCallback(() => {
    setToken(null);
    setExpired(true);
  }, []);
  const api = useMemo(
    () => createApi({ ...(fetch ? { fetch } : {}), token: () => token, onUnauthorized: logout }),
    [fetch, token, logout],
  );
  if (!token) {
    return (
      <Login
        api={api}
        expired={expired}
        onToken={(t) => {
          setExpired(false);
          setToken(t);
        }}
      />
    );
  }
  return (
    <Shell api={api} token={token} wsUrl={wsUrl} socket={socket} views={views} onLogout={logout} />
  );
}

function Login({
  api,
  expired,
  onToken,
}: {
  api: Api;
  expired: boolean;
  onToken: (t: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <main className="login">
      <h1>OrionFX</h1>
      {expired && <p role="status">Your session ended. Please sign in again.</p>}
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            onToken((await api.login(password)).token);
          } catch (err) {
            setError(
              err instanceof ApiError && err.status === 429
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
  const { protocol, host } = window.location;
  return `${protocol === "https:" ? "wss" : "ws"}://${host}/ws/live`;
}

function Shell({
  api,
  token,
  wsUrl,
  socket,
  views,
  onLogout,
}: {
  api: Api;
  token: string;
  wsUrl: string | undefined;
  socket: ((url: string) => SocketLike) | undefined;
  views: Record<Route, () => React.ReactElement>;
  onLogout: () => void;
}) {
  const [route, setRoute] = useState<Route>(() => routeFromHash(window.location.hash));
  const [status, setStatus] = useState<Status | null>(null);
  const [feed, setFeed] = useState<FeedState>("CONNECTING");
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    const onHash = () => setRoute(routeFromHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    const live = new LiveFeed({
      url: wsUrl ?? defaultWsUrl(),
      token: () => token,
      ...(socket ? { socket } : {}),
      onEvent: () => refresh(),
      onState: (s) => {
        setFeed(s);
        if (s === "LIVE") refresh(); // reconnected: re-read the snapshots
      },
      onUnauthorized: onLogout,
    });
    live.start();
    return () => live.stop();
  }, [token, wsUrl, socket, refresh, onLogout]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: `tick` is the refetch signal
  useEffect(() => {
    let alive = true;
    api
      .status()
      .then((s) => alive && setStatus(s))
      .catch(() => alive && setStatus(null));
    return () => {
      alive = false;
    };
  }, [api, tick]);

  const View = views[route];
  return (
    <AppContext.Provider value={{ api, status, feed, tick, refresh }}>
      <StatusBar status={status} feed={feed} />
      <nav aria-label="Sections">
        {ROUTES.map((r) => (
          <a key={r} href={`#/${r}`} aria-current={r === route ? "page" : undefined}>
            {r[0]?.toUpperCase() + r.slice(1)}
          </a>
        ))}
        <button type="button" className="quiet" onClick={() => api.logout().finally(onLogout)}>
          Sign out
        </button>
      </nav>
      <main>
        <View />
      </main>
    </AppContext.Provider>
  );
}

export function StatusBar({ status, feed }: { status: Status | null; feed: FeedState }) {
  const fresh = freshness(status?.as_of ?? null, new Date(), STATUS_MAX_AGE_MS);
  const live = feed === "LIVE" && fresh === "LIVE";
  return (
    <header className="statusbar">
      <span className={`pill ${live ? "ok" : "warn"}`} role="status">
        {feed === "LIVE"
          ? fresh === "LIVE"
            ? "Live"
            : "Stale"
          : feed === "CONNECTING"
            ? "Connecting…"
            : "Disconnected"}
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
