import type { ReactNode } from "react";
import type { ConfigCatalogItem } from "../configTypes";
import { MAIN_PROVIDERS } from "../configTypes";
import type { SettingsCopy } from "../settingsCopy";

export type ModelDraft = {
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
};

export type VlmDraft = {
  enabled: boolean;
  endpoint: string;
  model: string;
  api_key: string;
  protocol: string;
  timeout_s: string;
};

export type ScholarDraft = { api_key: string };

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="settings-field">
      <span className="settings-label">
        {label}
        {hint ? <span className="muted">{hint}</span> : null}
      </span>
      {children}
    </label>
  );
}

export function secretPlaceholder(set: boolean, copy: SettingsCopy): string {
  return set ? copy.keepSecret : copy.unsetSecret;
}

export function ModelFields({
  draft,
  onChange,
  catalog,
  apiKeySet,
  copy,
}: {
  draft: ModelDraft;
  onChange: (next: ModelDraft) => void;
  catalog: ConfigCatalogItem[];
  apiKeySet: boolean;
  copy: SettingsCopy;
}) {
  const applyProvider = (provider: string) => {
    const preset = catalog.find((item) => item.key === provider);
    const next = { ...draft, provider };
    if (preset && (!draft.base_url || draft.provider === "mock")) {
      next.base_url = preset.default_endpoint;
    }
    if (preset && (!draft.model || draft.provider === "mock" || draft.model === "omni-mock")) {
      next.model = preset.default_model;
    }
    if (provider === "mock") {
      next.base_url = "";
      next.model = "omni-mock";
    }
    onChange(next);
  };
  return (
    <div className="settings-grid">
      <Field label={copy.provider}>
        <select
          className="settings-input"
          value={draft.provider}
          onChange={(event) => applyProvider(event.target.value)}
        >
          {MAIN_PROVIDERS.map((item) => (
            <option key={item} value={item}>
              {catalog.find((row) => row.key === item)?.label || item}
            </option>
          ))}
        </select>
      </Field>
      <Field label={copy.baseUrl} hint={draft.provider === "mock" ? copy.optional : copy.required}>
        <input
          className="settings-input"
          value={draft.base_url}
          placeholder="https://api.deepseek.com/v1"
          disabled={draft.provider === "mock"}
          onChange={(event) => onChange({ ...draft, base_url: event.target.value })}
        />
      </Field>
      <Field label={copy.modelName}>
        <input
          className="settings-input"
          value={draft.model}
          onChange={(event) => onChange({ ...draft, model: event.target.value })}
        />
      </Field>
      <Field label={copy.apiKey} hint={copy.optional}>
        <input
          className="settings-input"
          type="password"
          autoComplete="off"
          value={draft.api_key}
          placeholder={secretPlaceholder(apiKeySet, copy)}
          onChange={(event) => onChange({ ...draft, api_key: event.target.value })}
        />
      </Field>
    </div>
  );
}

export function VlmFields({
  draft,
  onChange,
  apiKeySet,
  copy,
}: {
  draft: VlmDraft;
  onChange: (next: VlmDraft) => void;
  apiKeySet: boolean;
  copy: SettingsCopy;
}) {
  return (
    <div className="settings-grid">
      <label className="settings-check">
        <input
          type="checkbox"
          checked={draft.enabled}
          onChange={(event) => onChange({ ...draft, enabled: event.target.checked })}
        />
        {copy.enable}
      </label>
      <Field label={copy.baseUrl} hint={copy.vlmEndpointHint}>
        <input
          className="settings-input"
          value={draft.endpoint}
          placeholder="https://host  or  https://host/v1"
          onChange={(event) => onChange({ ...draft, endpoint: event.target.value })}
        />
      </Field>
      <Field label={copy.modelName}>
        <input
          className="settings-input"
          value={draft.model}
          onChange={(event) => onChange({ ...draft, model: event.target.value })}
        />
      </Field>
      <Field label={copy.apiKey}>
        <input
          className="settings-input"
          type="password"
          autoComplete="off"
          value={draft.api_key}
          placeholder={secretPlaceholder(apiKeySet, copy)}
          onChange={(event) => onChange({ ...draft, api_key: event.target.value })}
        />
      </Field>
      <Field label="protocol">
        <input
          className="settings-input"
          value={draft.protocol}
          onChange={(event) => onChange({ ...draft, protocol: event.target.value })}
        />
      </Field>
      <Field label="timeout_s">
        <input
          className="settings-input"
          type="number"
          min={1}
          step="1"
          value={draft.timeout_s}
          onChange={(event) => onChange({ ...draft, timeout_s: event.target.value })}
        />
      </Field>
    </div>
  );
}

export function ScholarFields({
  draft,
  onChange,
  apiKeySet,
  copy,
}: {
  draft: ScholarDraft;
  onChange: (next: ScholarDraft) => void;
  apiKeySet: boolean;
  copy: SettingsCopy;
}) {
  return (
    <Field label={copy.apiKey} hint={copy.optional}>
      <input
        className="settings-input"
        type="password"
        autoComplete="off"
        value={draft.api_key}
        placeholder={secretPlaceholder(apiKeySet, copy)}
        onChange={(event) => onChange({ ...draft, api_key: event.target.value })}
      />
    </Field>
  );
}

export function vlmHasValues(draft: VlmDraft): boolean {
  return Boolean(
    draft.endpoint.trim() ||
      draft.model.trim() ||
      draft.api_key.trim() ||
      (draft.protocol && draft.protocol !== "openai_compatible_chat") ||
      draft.timeout_s,
  );
}

export function mainModelReady(draft: ModelDraft): boolean {
  if (draft.provider === "mock") return true;
  return Boolean(draft.base_url.trim() && draft.model.trim());
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

export function parseAdvancedValue(raw: string): string | number | boolean | unknown {
  const text = raw.trim();
  if (text === "true" || text === "false" || text.startsWith("[") || text.startsWith("{")) {
    return text;
  }
  return raw;
}
