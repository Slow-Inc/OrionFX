// Typed client for the M08 API. Bearer-token auth; a 401 ends the session. Errors are
// typed so the UI can say what happened: unauthorized, conflict (someone else changed
// it first), validation (with per-field messages), unavailable, or other.
import type {
  Account,
  CommandResult,
  ControlAction,
  Cycle,
  Decision,
  Items,
  Prediction,
  Settings,
  Status,
  Unavailable,
  UserConfig,
} from "./types";

export type ApiErrorKind = "unauthorized" | "conflict" | "validation" | "unavailable" | "other";

export class ApiError extends Error {
  constructor(
    readonly kind: ApiErrorKind,
    readonly status: number,
    message: string,
    readonly fields: Record<string, string> = {},
  ) {
    super(message);
  }
}

export interface ApiOptions {
  fetch?: typeof fetch;
  base?: string;
  token: () => string | null;
  onUnauthorized: () => void;
}

interface ValidationDetail {
  loc?: (string | number)[];
  msg?: string;
}

function fieldErrors(detail: unknown): Record<string, string> {
  if (!Array.isArray(detail)) return {};
  const out: Record<string, string> = {};
  for (const d of detail as ValidationDetail[]) {
    if (d.loc && d.msg) out[d.loc.join(".")] = d.msg;
  }
  return out;
}

export function createApi(opts: ApiOptions) {
  const doFetch = opts.fetch ?? fetch.bind(globalThis);
  const base = opts.base ?? "";

  async function call<T>(path: string, init: RequestInit = {}, auth = true): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body !== undefined) headers.set("Content-Type", "application/json");
    const token = opts.token();
    if (auth && token) headers.set("Authorization", `Bearer ${token}`);
    const res = await doFetch(base + path, { ...init, headers });
    if (res.ok) return (res.status === 204 ? undefined : await res.json()) as T;
    const body: unknown = await res.json().catch(() => null);
    const detail = (body as { detail?: unknown } | null)?.detail;
    const message = typeof detail === "string" ? detail : `HTTP ${res.status}`;
    if (res.status === 401) {
      opts.onUnauthorized();
      throw new ApiError("unauthorized", 401, message);
    }
    if (res.status === 409) throw new ApiError("conflict", 409, message);
    if (res.status === 422) throw new ApiError("validation", 422, message, fieldErrors(detail));
    if (res.status === 503) throw new ApiError("unavailable", 503, "storage unavailable");
    throw new ApiError("other", res.status, message);
  }

  return {
    login: (password: string) =>
      call<{ token: string; expires_at: string }>(
        "/api/auth/login",
        { method: "POST", body: JSON.stringify({ password }) },
        false,
      ),
    logout: () => call<void>("/api/auth/logout", { method: "POST" }),
    status: () => call<Status>("/api/status"),
    account: () => call<Account>("/api/account"),
    positions: () => call<Unavailable>("/api/positions"),
    cycles: (limit = 50) => call<Items<Cycle>>(`/api/cycles?limit=${limit}`),
    predictions: (limit = 50) => call<Items<Prediction>>(`/api/predictions?limit=${limit}`),
    decisions: (limit = 50) => call<Items<Decision>>(`/api/decisions?limit=${limit}`),
    settings: () => call<Settings>("/api/settings"),
    saveSettings: (expected_version: number, config: UserConfig) =>
      call<{ version: number }>("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ expected_version, config }),
      }),
    command: (action: ControlAction) =>
      call<{ command_id: string; status: "ACCEPTED" }>(`/api/bot/${action}`, { method: "POST" }),
    commandStatus: (id: string) => call<CommandResult>(`/api/commands/${encodeURIComponent(id)}`),
  };
}

export type Api = ReturnType<typeof createApi>;
