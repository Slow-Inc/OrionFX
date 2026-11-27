// Settings (#75; UI-05). Edits a copy of the stored config and saves it with the version
// it was read at, so a newer config is never overwritten silently (409 asks to reload).
// Validation errors appear next to their field. Secrets are never shown: the stored
// value arrives masked, the input starts empty, and the mask is sent back unchanged
// unless a new value is typed. Live is never a toggle: the backend config accepts only
// Shadow or Demo, and Live needs a passed qualification and a protected policy change.
import { useEffect, useState } from "react";
import { ApiError } from "./api";
import { useApp } from "./app";
import { MASK, type Settings, type TradingConfig, type UserConfig } from "./types";

const STRATEGIES = ["trend_following", "breakout", "pullback", "range", "scalping", "hybrid"];

type Errors = Record<string, string>;

function NumberField({
  label,
  path,
  value,
  onChange,
  errors,
}: {
  label: string;
  path: string;
  value: string | null;
  onChange: (v: string | null) => void;
  errors: Errors;
}) {
  const error = errors[path];
  return (
    <label>
      {label}
      <input
        inputMode="decimal"
        value={value ?? ""}
        aria-invalid={error ? true : undefined}
        onChange={(e) => onChange(e.target.value.trim() === "" ? null : e.target.value.trim())}
      />
      {error && <span className="field-error">{error}</span>}
    </label>
  );
}

export function SettingsView() {
  const { api, refresh } = useApp();
  const [loaded, setLoaded] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<UserConfig | null>(null);
  const [secret, setSecret] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = () => {
    setConflict(false);
    setProblem(null);
    api.settings().then(
      (s) => {
        setLoaded(s);
        setDraft(structuredClone(s.config));
        setSecret("");
        setErrors({});
      },
      () => setProblem("Could not read the settings."),
    );
  };
  // biome-ignore lint/correctness/useExhaustiveDependencies: read once; reload is explicit
  useEffect(load, []);

  if (!loaded || !draft) {
    return (
      <>
        <h2>Settings</h2>
        {problem ? <p role="alert">{problem}</p> : <p className="muted">Loading…</p>}
      </>
    );
  }

  const setTrading = (patch: Partial<TradingConfig>) =>
    setDraft({ ...draft, trading: { ...draft.trading, ...patch } });
  const t = draft.trading;

  const save = async () => {
    setSaving(true);
    setErrors({});
    setNotice(null);
    setProblem(null);
    const config: UserConfig = {
      ...draft,
      llm: { ...draft.llm, api_key_ref: secret !== "" ? secret : loaded.config.llm.api_key_ref },
    };
    try {
      const { version } = await api.saveSettings(loaded.version, config);
      setNotice(`Saved (version ${version})`);
      load(); // re-read what was stored: the form shows the saved truth
      refresh();
    } catch (err) {
      if (err instanceof ApiError && err.kind === "conflict") {
        setConflict(true);
        setProblem("The settings were changed elsewhere since you opened them.");
      } else if (err instanceof ApiError && err.kind === "validation") {
        setErrors(err.fields);
        setProblem("Some settings are not valid.");
      } else {
        setProblem("Could not save the settings.");
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
            path="trading.initial_lot"
            value={t.initial_lot}
            onChange={(v) => setTrading({ initial_lot: v })}
            errors={errors}
          />
          <NumberField
            label="Profit Target (USD)"
            path="trading.profit_target_usd"
            value={t.profit_target_usd}
            onChange={(v) => setTrading({ profit_target_usd: v })}
            errors={errors}
          />
          <NumberField
            label="Max drawdown (% of account equity)"
            path="trading.max_drawdown_percent"
            value={t.max_drawdown_percent}
            onChange={(v) => setTrading({ max_drawdown_percent: v })}
            errors={errors}
          />
          <label>
            Account mode
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
          </label>
          <p className="muted">
            Live needs a passed qualification and a reviewed protected-policy change; it is never
            switched on here.
          </p>
          <label>
            Risk profile
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
          </label>
          <fieldset>
            <legend>Strategies enabled</legend>
            {STRATEGIES.map((s) => (
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
              </label>
            ))}
          </fieldset>
        </fieldset>
        <fieldset>
          <legend>LLM</legend>
          <label>
            Provider
            <input
              value={draft.llm.provider ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, llm: { ...draft.llm, provider: e.target.value || null } })
              }
            />
          </label>
          <label>
            Model
            <input
              value={draft.llm.model ?? ""}
              onChange={(e) =>
                setDraft({ ...draft, llm: { ...draft.llm, model: e.target.value || null } })
              }
            />
          </label>
          <label>
            API key reference{" "}
            {loaded.config.llm.api_key_ref === MASK ? "(set; type to replace)" : "(not set)"}
            <input
              type="password"
              autoComplete="off"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
            />
            {errors["llm.api_key_ref"] && (
              <span className="field-error">{errors["llm.api_key_ref"]}</span>
            )}
          </label>
        </fieldset>
        <div className="row">
          <button type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save settings"}
          </button>
          {conflict && (
            <button type="button" onClick={load}>
              Reload settings
            </button>
          )}
        </div>
        {notice && <p role="status">{notice}</p>}
        {problem && <p role="alert">{problem}</p>}
      </form>
    </>
  );
}
