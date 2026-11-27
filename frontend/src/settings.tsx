// Settings (#75; UI-05). Edits a copy of the stored config and saves it with the version
// it was read at, so a newer config is never overwritten silently (409 asks to reload).
// Validation errors appear next to their field; any without a field on screen are listed
// in the alert. Secrets are never shown: the stored value arrives masked, the input
// starts empty (and is never offered for password autofill), and the mask is sent back
// unchanged unless a new value is typed or the key is removed. The strategy list comes
// from the backend. Live is never a toggle: the backend config accepts only Shadow or
// Demo, and Live needs a passed qualification and a protected policy change.
import { type ReactNode, useEffect, useState } from "react";
import { ApiError } from "./api";
import { useApp } from "./app";
import { type LlmConfig, MASK, type Settings, type TradingConfig, type UserConfig } from "./types";

type Errors = Record<string, string>;
type Outcome =
  | { kind: "saved"; version: number }
  | { kind: "conflict" }
  | { kind: "invalid"; unplaced: string[] }
  | { kind: "error"; message: string }
  | null;

/** Error paths that have a field on screen; others are listed in the alert. */
const PLACED = new Set([
  "trading.initial_lot",
  "trading.profit_target_usd",
  "trading.max_drawdown_percent",
  "trading.account_mode",
  "trading.risk_profile",
  "trading.strategies_enabled",
  "llm.provider",
  "llm.model",
  "llm.base_url",
  "llm.reasoning_effort",
  "llm.api_key_ref",
]);

function Field({
  label,
  error,
  children,
}: {
  label: ReactNode;
  error?: string | undefined;
  children: ReactNode;
}) {
  return (
    // biome-ignore lint/a11y/noLabelWithoutControl: the control is passed in as `children`
    <label>
      {label}
      {children}
      {error && <span className="field-error">{error}</span>}
    </label>
  );
}

function NumberField({
  label,
  value,
  error,
  onChange,
}: {
  label: string;
  value: string | null;
  error: string | undefined;
  onChange: (v: string | null) => void;
}) {
  return (
    <Field label={label} error={error}>
      <input
        inputMode="decimal"
        value={value ?? ""}
        aria-invalid={error ? true : undefined}
        onChange={(e) => onChange(e.target.value.trim() === "" ? null : e.target.value.trim())}
      />
    </Field>
  );
}

function unplacedErrors(errors: Errors): string[] {
  return Object.entries(errors)
    .filter(([path]) => !PLACED.has(path.replace(/\.\d+$/, "")))
    .map(([path, msg]) => (path ? `${path}: ${msg}` : msg));
}

export function SettingsView() {
  const { api, refresh } = useApp();
  const [loaded, setLoaded] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<UserConfig | null>(null);
  const [secret, setSecret] = useState("");
  const [removeKey, setRemoveKey] = useState(false);
  const [errors, setErrors] = useState<Errors>({});
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [saving, setSaving] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);

  const load = async () => {
    try {
      const s = await api.settings();
      setLoaded(s);
      setDraft(structuredClone(s.config));
      setSecret("");
      setRemoveKey(false);
      setErrors({});
      setLoadFailed(false);
    } catch {
      setLoadFailed(true);
    }
  };
  // biome-ignore lint/correctness/useExhaustiveDependencies: read once; reload is explicit
  useEffect(() => {
    void load();
  }, []);

  if (!loaded || !draft) {
    return (
      <>
        <h2>Settings</h2>
        {loadFailed ? (
          <p role="alert">Could not read the settings.</p>
        ) : (
          <p className="muted">Loading…</p>
        )}
      </>
    );
  }

  const t = draft.trading;
  const setTrading = (patch: Partial<TradingConfig>) =>
    setDraft({ ...draft, trading: { ...draft.trading, ...patch } });
  const setLlm = (patch: Partial<LlmConfig>) =>
    setDraft({ ...draft, llm: { ...draft.llm, ...patch } });
  const keySet = loaded.config.llm.api_key_ref === MASK;
  // Stored ids the backend no longer knows stay visible so they can be removed.
  const strategies = [...new Set([...loaded.available_strategies, ...t.strategies_enabled])];

  const save = async () => {
    setSaving(true);
    setErrors({});
    setOutcome(null);
    const apiKey = removeKey ? null : secret !== "" ? secret : loaded.config.llm.api_key_ref;
    const config: UserConfig = { ...draft, llm: { ...draft.llm, api_key_ref: apiKey } };
    try {
      const { version } = await api.saveSettings(loaded.version, config);
      setOutcome({ kind: "saved", version });
      await load(); // re-read what was stored, at its new version, before Save is enabled
      refresh();
    } catch (err) {
      if (err instanceof ApiError && err.kind === "conflict") {
        setOutcome({ kind: "conflict" });
      } else if (err instanceof ApiError && err.kind === "validation") {
        setErrors(err.fields);
        setOutcome({ kind: "invalid", unplaced: unplacedErrors(err.fields) });
      } else {
        setOutcome({ kind: "error", message: "Could not save the settings." });
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <h2>Settings</h2>
      <form
        className="settings"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <fieldset>
          <legend>Trading</legend>
          <NumberField
            label="Initial Lot"
            value={t.initial_lot}
            error={errors["trading.initial_lot"]}
            onChange={(v) => setTrading({ initial_lot: v })}
          />
          <NumberField
            label="Profit Target (USD)"
            value={t.profit_target_usd}
            error={errors["trading.profit_target_usd"]}
            onChange={(v) => setTrading({ profit_target_usd: v })}
          />
          <NumberField
            label="Max drawdown (% of account equity)"
            value={t.max_drawdown_percent}
            error={errors["trading.max_drawdown_percent"]}
            onChange={(v) => setTrading({ max_drawdown_percent: v })}
          />
          <Field label="Account mode" error={errors["trading.account_mode"]}>
            <select
              value={t.account_mode}
              onChange={(e) => setTrading({ account_mode: e.target.value as "SHADOW" | "DEMO" })}
            >
              <option value="SHADOW">Shadow (no orders)</option>
              <option value="DEMO">Demo</option>
              <option value="LIVE" disabled>
                Live (not available)
              </option>
            </select>
          </Field>
          <p className="muted">
            Live needs a passed qualification and a reviewed protected-policy change; it is never
            switched on here.
          </p>
          <Field label="Risk profile" error={errors["trading.risk_profile"]}>
            <select
              value={t.risk_profile}
              onChange={(e) =>
                setTrading({ risk_profile: e.target.value as TradingConfig["risk_profile"] })
              }
            >
              <option value="LOW">Low</option>
              <option value="NORMAL">Normal</option>
              <option value="HIGH">High</option>
            </select>
          </Field>
          <fieldset>
            <legend>Strategies enabled</legend>
            {strategies.map((s) => (
              <label key={s} className="check">
                <input
                  type="checkbox"
                  checked={t.strategies_enabled.includes(s)}
                  onChange={(e) =>
                    setTrading({
                      strategies_enabled: e.target.checked
                        ? [...t.strategies_enabled, s]
                        : t.strategies_enabled.filter((x) => x !== s),
                    })
                  }
                />
                {s}
                {!loaded.available_strategies.includes(s) && (
                  <span className="pill warn">unknown</span>
                )}
              </label>
            ))}
            {errors["trading.strategies_enabled"] && (
              <span className="field-error">{errors["trading.strategies_enabled"]}</span>
            )}
          </fieldset>
        </fieldset>
        <fieldset>
          <legend>LLM</legend>
          <Field label="Provider" error={errors["llm.provider"]}>
            <input
              value={draft.llm.provider ?? ""}
              onChange={(e) => setLlm({ provider: e.target.value || null })}
            />
          </Field>
          <Field label="Model" error={errors["llm.model"]}>
            <input
              value={draft.llm.model ?? ""}
              onChange={(e) => setLlm({ model: e.target.value || null })}
            />
          </Field>
          <Field label="Base URL" error={errors["llm.base_url"]}>
            <input
              type="url"
              inputMode="url"
              placeholder="https://…/openai/v1"
              value={draft.llm.base_url ?? ""}
              onChange={(e) => setLlm({ base_url: e.target.value.trim() || null })}
            />
          </Field>
          <Field label="Reasoning effort" error={errors["llm.reasoning_effort"]}>
            <input
              placeholder="e.g. low, high, max (depends on the model)"
              value={draft.llm.reasoning_effort ?? ""}
              onChange={(e) => setLlm({ reasoning_effort: e.target.value.trim() || null })}
            />
          </Field>
          <Field
            label={`API key reference ${keySet ? "(set; type to replace)" : "(not set)"}`}
            error={errors["llm.api_key_ref"]}
          >
            <input
              type="password"
              autoComplete="new-password"
              value={secret}
              disabled={removeKey}
              onChange={(e) => setSecret(e.target.value)}
            />
          </Field>
          {keySet && (
            <label className="check">
              <input
                type="checkbox"
                checked={removeKey}
                onChange={(e) => setRemoveKey(e.target.checked)}
              />
              Remove the stored key on save
            </label>
          )}
        </fieldset>
        <div className="row">
          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save settings"}
          </button>
          {outcome?.kind === "conflict" && (
            <button type="button" onClick={() => void load().then(() => setOutcome(null))}>
              Reload settings
            </button>
          )}
        </div>
        {outcome?.kind === "saved" && <p role="status">Saved (version {outcome.version})</p>}
        {outcome?.kind === "conflict" && (
          <p role="alert">The settings were changed elsewhere since you opened them.</p>
        )}
        {outcome?.kind === "invalid" && (
          <div role="alert">
            <p>Some settings are not valid.</p>
            {outcome.unplaced.length > 0 && (
              <ul>
                {outcome.unplaced.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            )}
          </div>
        )}
        {outcome?.kind === "error" && <p role="alert">{outcome.message}</p>}
      </form>
    </>
  );
}
