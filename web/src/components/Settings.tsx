import { useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, api } from "../api";
import type { ConfigDescribe } from "../configTypes";
import { IconClose } from "../icons";
import { trapFocus } from "../focus";
import { settingsCopy, type SettingsCopy } from "../settingsCopy";
import type { ChannelDescribeResponse } from "../channelTypes";
import type {
  PersonaAction,
  PersonaSnapshot,
  PersonaStartRequest,
} from "../personaTypes";
import {
  applyLocale,
  applyTheme,
  type LocalePreference,
  type SettingsSection,
  type ThemePreference,
  type UiPrefs,
} from "../uiPrefs";
import {
  Field,
  ModelFields,
  ScholarFields,
  VlmFields,
  formatValue,
  parseAdvancedValue,
  vlmHasValues,
  type ModelDraft,
  type ScholarDraft,
  type VlmDraft,
} from "./settingsFields";
import { ChannelSettings } from "./ChannelSettings";
import { SkillSettings } from "./SkillSettings";
import { PersonaSettings } from "./PersonaSettings";

type SettingsProps = {
  prefs: UiPrefs;
  onPrefs: (next: UiPrefs) => void;
  onClose: () => void;
  onChannelsChanged?: (data: ChannelDescribeResponse) => void;
  persona: PersonaSnapshot | null;
  personaLoading: boolean;
  personaBusy: boolean;
  personaWorkspaceKey?: string;
  personaFolderPath?: string;
  personaError?: string;
  personaNotice?: string;
  pendingPersonaId?: string;
  pendingPersonaAction?: PersonaAction | "";
  onPersonaReload: () => Promise<void>;
  onPersonaStart: (request: PersonaStartRequest) => Promise<void>;
};

const SECTIONS: SettingsSection[] = [
  "general",
  "models",
  "capability",
  "personas",
  "channels",
  "runtime",
  "skills",
  "advanced",
  "interface",
];

export function Settings({
  prefs,
  onPrefs,
  onClose,
  onChannelsChanged,
  persona,
  personaLoading,
  personaBusy,
  personaWorkspaceKey,
  personaFolderPath,
  personaError,
  personaNotice,
  pendingPersonaId,
  pendingPersonaAction,
  onPersonaReload,
  onPersonaStart,
}: SettingsProps) {
  const copy = settingsCopy(prefs.locale);
  const dialogRef = useRef<HTMLDivElement>(null);
  const [describe, setDescribe] = useState<ConfigDescribe | null>(null);
  const [section, setSection] = useState<SettingsSection>(prefs.lastSection);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const reload = async () => {
    const data = await api.describeConfig();
    setDescribe(data);
    return data;
  };

  useEffect(() => {
    void reload().catch((err: unknown) => {
      setError(err instanceof Error ? err.message : String(err));
    });
    window.requestAnimationFrame(() => dialogRef.current?.focus());
  }, []);

  const run = async (task: () => Promise<string | undefined>) => {
    setBusy(true);
    setError("");
    try {
      const message = await task();
      await reload();
      setNotice(message || copy.saved);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : copy.failed);
    } finally {
      setBusy(false);
    }
  };

  const chooseSection = (next: SettingsSection) => {
    setSection(next);
    onPrefs({ ...prefs, lastSection: next });
  };

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        ref={dialogRef}
        className="modal settings-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        tabIndex={-1}
        onMouseDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            onClose();
            return;
          }
          trapFocus(event);
        }}
      >
        <header>
          <div>
            <h2 id="settings-title">{copy.settings}</h2>
            <p className="muted">{copy.help}</p>
          </div>
          <button type="button" className="icon-btn square" aria-label={copy.closeSettings} onClick={onClose}>
            <IconClose size={17} />
          </button>
        </header>
        <div className="settings-shell">
          <nav className="settings-nav" aria-label={copy.settings}>
            {SECTIONS.map((id) => (
              <button
                key={id}
                type="button"
                className={section === id ? "active" : ""}
                aria-current={section === id ? "page" : undefined}
                onClick={() => chooseSection(id)}
              >
                {copy[id]}
              </button>
            ))}
          </nav>
          <div className="settings-body">
            {error ? (
              <p className="banner error" role="alert">
                {error}
              </p>
            ) : null}
            {notice || describe?.notice ? (
              <p className="banner" role="status">
                {notice || describe?.notice}
              </p>
            ) : null}
            {!describe ? (
              <p className="muted">{copy.saving}</p>
            ) : (
              <Section
                section={section}
                describe={describe}
                copy={copy}
                prefs={prefs}
                busy={busy}
                onPrefs={onPrefs}
                run={run}
                onChannelsChanged={onChannelsChanged}
                persona={persona}
                personaLoading={personaLoading}
                personaBusy={personaBusy}
                personaWorkspaceKey={personaWorkspaceKey}
                personaFolderPath={personaFolderPath}
                personaError={personaError}
                personaNotice={personaNotice}
                pendingPersonaId={pendingPersonaId}
                pendingPersonaAction={pendingPersonaAction}
                onPersonaReload={onPersonaReload}
                onPersonaStart={onPersonaStart}
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function Section({
  section,
  describe,
  copy,
  prefs,
  busy,
  onPrefs,
  run,
  onChannelsChanged,
  persona,
  personaLoading,
  personaBusy,
  personaWorkspaceKey,
  personaFolderPath,
  personaError,
  personaNotice,
  pendingPersonaId,
  pendingPersonaAction,
  onPersonaReload,
  onPersonaStart,
}: {
  section: SettingsSection;
  describe: ConfigDescribe;
  copy: SettingsCopy;
  prefs: UiPrefs;
  busy: boolean;
  onPrefs: (next: UiPrefs) => void;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
  onChannelsChanged?: (data: ChannelDescribeResponse) => void;
  persona: PersonaSnapshot | null;
  personaLoading: boolean;
  personaBusy: boolean;
  personaWorkspaceKey?: string;
  personaFolderPath?: string;
  personaError?: string;
  personaNotice?: string;
  pendingPersonaId?: string;
  pendingPersonaAction?: PersonaAction | "";
  onPersonaReload: () => Promise<void>;
  onPersonaStart: (request: PersonaStartRequest) => Promise<void>;
}) {
  if (section === "general") return <GeneralPanel describe={describe} copy={copy} busy={busy} run={run} />;
  if (section === "models") return <ModelsPanel describe={describe} copy={copy} busy={busy} run={run} />;
  if (section === "capability") return <CapabilityPanel describe={describe} copy={copy} busy={busy} run={run} />;
  if (section === "personas") {
    return (
      <PersonaSettings
        key={personaWorkspaceKey || "persona-settings"}
        locale={prefs.locale}
        snapshot={persona}
        loading={personaLoading}
        busy={personaBusy}
        error={personaError}
        notice={personaNotice}
        folderPath={personaFolderPath}
        pendingScientistId={pendingPersonaId}
        pendingAction={pendingPersonaAction}
        onReload={onPersonaReload}
        onStart={onPersonaStart}
      />
    );
  }
  if (section === "channels") {
    return <ChannelSettings locale={prefs.locale} onChanged={onChannelsChanged} />;
  }
  if (section === "runtime") return <RuntimePanel describe={describe} copy={copy} busy={busy} run={run} />;
  if (section === "skills") {
    return <SkillSettings describe={describe} copy={copy} busy={busy} run={run} />;
  }
  if (section === "advanced") return <AdvancedPanel describe={describe} copy={copy} busy={busy} run={run} />;
  return <InterfacePanel prefs={prefs} copy={copy} onPrefs={onPrefs} />;
}

function Actions({
  copy,
  busy,
  onSave,
  extra,
}: {
  copy: SettingsCopy;
  busy: boolean;
  onSave: () => void;
  extra?: ReactNode;
}) {
  return (
    <div className="settings-actions">
      {extra}
      <button type="button" className="btn" disabled={busy} onClick={onSave}>
        {busy ? copy.saving : copy.save}
      </button>
    </div>
  );
}

function GeneralPanel({
  describe,
  copy,
  busy,
  run,
}: {
  describe: ConfigDescribe;
  copy: SettingsCopy;
  busy: boolean;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
}) {
  const [home, setHome] = useState(describe.home.active);
  return (
    <div className="settings-stack">
      <section className="settings-block">
        <h3>{copy.dataDir}</h3>
        <p className="muted">
          {describe.home.source} · {copy.homeWarn}
        </p>
        <Field label={copy.dataDir}>
          <input className="settings-input" value={home} onChange={(event) => setHome(event.target.value)} />
        </Field>
        <Actions
          copy={copy}
          busy={busy}
          onSave={() => void run(async () => (await api.configHome({ path: home })).notice)}
          extra={
            <button
              type="button"
              className="btn ghost"
              disabled={busy}
              onClick={() => void run(async () => (await api.configHome({ reset: true })).notice)}
            >
              {copy.resetHome}
            </button>
          }
        />
      </section>
      <section className="settings-block">
        <h3>{copy.paths}</h3>
        <dl className="settings-paths">
          {Object.entries(describe.paths).map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>
                <code>{value}</code>
              </dd>
            </div>
          ))}
        </dl>
      </section>
    </div>
  );
}

function ModelsPanel({
  describe,
  copy,
  busy,
  run,
}: {
  describe: ConfigDescribe;
  copy: SettingsCopy;
  busy: boolean;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
}) {
  const [model, setModel] = useState<ModelDraft>({
    provider: describe.blocks.model.provider,
    base_url: describe.blocks.model.base_url,
    model: describe.blocks.model.model,
    api_key: "",
  });
  const [vlm, setVlm] = useState<VlmDraft>({
    enabled: describe.blocks.vlm.enabled,
    endpoint: describe.blocks.vlm.endpoint,
    model: describe.blocks.vlm.model,
    api_key: "",
    protocol: describe.blocks.vlm.protocol,
    timeout_s: String(describe.blocks.vlm.timeout_s || ""),
  });
  const [scholar, setScholar] = useState<ScholarDraft>({ api_key: "" });
  return (
    <div className="settings-stack">
      <section className="settings-block">
        <h3>{copy.mainModel}</h3>
        <p className="muted">
          {describe.blocks.model.health}
          {describe.blocks.model.health_detail ? ` · ${describe.blocks.model.health_detail}` : ""}
        </p>
        <ModelFields
          draft={model}
          onChange={setModel}
          catalog={describe.catalog}
          apiKeySet={describe.blocks.model.api_key_set}
          copy={copy}
        />
        <Actions
          copy={copy}
          busy={busy}
          onSave={() =>
            void run(async () => {
              const result = await api.applyModel({
                provider: model.provider,
                base_url: model.base_url.trim(),
                model: model.model.trim(),
                api_key: model.api_key.trim(),
              });
              return result.notice;
            })
          }
          extra={
            <>
              <button
                type="button"
                className="btn ghost"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    const result = await api.testConfig("model");
                    return result.detail;
                  })
                }
              >
                {copy.test}
              </button>
              {describe.blocks.model.api_key_set ? (
                <button
                  type="button"
                  className="btn ghost"
                  disabled={busy}
                  onClick={() => void run(async () => (await api.unsetConfig("model.api_key")).notice)}
                >
                  {copy.clearSecret}
                </button>
              ) : null}
            </>
          }
        />
      </section>
      <section className="settings-block">
        <h3>{copy.visionModel}</h3>
        <VlmFields draft={vlm} onChange={setVlm} apiKeySet={describe.blocks.vlm.api_key_set} copy={copy} />
        <Actions
          copy={copy}
          busy={busy}
          onSave={() =>
            void run(async () => {
              if (!vlmHasValues(vlm) && vlm.enabled === describe.blocks.vlm.enabled) {
                return copy.skipOptional;
              }
              const timeout = vlm.timeout_s.trim() ? Number(vlm.timeout_s) : undefined;
              const result = await api.applyVlm({
                enabled: vlm.enabled,
                endpoint: vlm.endpoint.trim(),
                model: vlm.model.trim(),
                api_key: vlm.api_key.trim(),
                protocol: vlm.protocol.trim(),
                timeout_s: Number.isFinite(timeout) ? timeout : undefined,
              });
              return result.notice;
            })
          }
          extra={
            <button
              type="button"
              className="btn ghost"
              disabled={busy}
              onClick={() =>
                void run(async () => {
                  const result = await api.testConfig("vlm");
                  return result.detail;
                })
              }
            >
              {copy.test}
            </button>
          }
        />
      </section>
      <section className="settings-block">
        <h3>{copy.semanticScholar}</h3>
        <ScholarFields
          draft={scholar}
          onChange={setScholar}
          apiKeySet={describe.blocks.semantic_scholar.api_key_set}
          copy={copy}
        />
        <Actions
          copy={copy}
          busy={busy}
          onSave={() =>
            void run(async () => {
              if (!scholar.api_key.trim()) return copy.skipOptional;
              return (await api.applySemanticScholar({ api_key: scholar.api_key.trim() })).notice;
            })
          }
          extra={
            <>
              <button
                type="button"
                className="btn ghost"
                disabled={busy}
                onClick={() =>
                  void run(async () => (await api.testConfig("semantic_scholar")).detail)
                }
              >
                {copy.test}
              </button>
              {describe.blocks.semantic_scholar.api_key_set ? (
                <button
                  type="button"
                  className="btn ghost"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => (await api.unsetConfig("research.semantic_scholar_api_key")).notice)
                  }
                >
                  {copy.clearSecret}
                </button>
              ) : null}
            </>
          }
        />
      </section>
    </div>
  );
}

function CapabilityPanel({
  describe,
  copy,
  busy,
  run,
}: {
  describe: ConfigDescribe;
  copy: SettingsCopy;
  busy: boolean;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
}) {
  const emb = describe.blocks.embeddings;
  const [enabled, setEnabled] = useState(emb.enabled);
  const [provider, setProvider] = useState(emb.provider || "openai_compatible");
  const [baseUrl, setBaseUrl] = useState(emb.base_url);
  const [model, setModel] = useState(emb.model);
  const [apiKey, setApiKey] = useState("");
  const [python, setPython] = useState(emb.specter2_python);
  const [baseModel, setBaseModel] = useState(emb.specter2_base_model);
  const [adapter, setAdapter] = useState(emb.specter2_adapter);
  const [device, setDevice] = useState(emb.specter2_device || "cpu");
  const [memory, setMemory] = useState(describe.blocks.memory.enabled);
  return (
    <div className="settings-stack">
      <section className="settings-block">
        <h3>{copy.embeddings}</h3>
        <label className="settings-check">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          {enabled ? copy.enable : copy.disable}
        </label>
        {enabled ? (
          <>
            <Field label={copy.provider}>
              <select className="settings-input" value={provider} onChange={(event) => setProvider(event.target.value)}>
                <option value="openai_compatible">{copy.remote}</option>
                <option value="specter2">{copy.specter2}</option>
              </select>
            </Field>
            {provider === "specter2" ? (
              <>
                <Field label={copy.python}>
                  <input className="settings-input" value={python} onChange={(event) => setPython(event.target.value)} />
                </Field>
                <Field label={copy.baseModel}>
                  <input className="settings-input" value={baseModel} onChange={(event) => setBaseModel(event.target.value)} />
                </Field>
                <Field label={copy.adapter}>
                  <input className="settings-input" value={adapter} onChange={(event) => setAdapter(event.target.value)} />
                </Field>
                <Field label={copy.device}>
                  <input className="settings-input" value={device} onChange={(event) => setDevice(event.target.value)} />
                </Field>
                <Field label={copy.modelName}>
                  <input className="settings-input" value={model} onChange={(event) => setModel(event.target.value)} />
                </Field>
              </>
            ) : (
              <>
                <Field label={copy.baseUrl}>
                  <input className="settings-input" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} />
                </Field>
                <Field label={copy.modelName}>
                  <input className="settings-input" value={model} onChange={(event) => setModel(event.target.value)} />
                </Field>
                <Field label={copy.apiKey}>
                  <input
                    className="settings-input"
                    type="password"
                    autoComplete="off"
                    value={apiKey}
                    placeholder={emb.api_key_set ? copy.keepSecret : copy.unsetSecret}
                    onChange={(event) => setApiKey(event.target.value)}
                  />
                </Field>
              </>
            )}
          </>
        ) : null}
        <Actions
          copy={copy}
          busy={busy}
          onSave={() =>
            void run(async () => {
              const result = await api.applyEmbeddings(
                enabled
                  ? provider === "specter2"
                    ? {
                        enabled: true,
                        provider: "specter2",
                        python,
                        base_model: baseModel,
                        adapter,
                        device,
                        model,
                      }
                    : { enabled: true, provider: "openai_compatible", base_url: baseUrl, model, api_key: apiKey }
                  : { enabled: false },
              );
              return result.message || result.notice;
            })
          }
        />
      </section>
      <section className="settings-block">
        <h3>{copy.memoryEnabled}</h3>
        <label className="settings-check">
          <input type="checkbox" checked={memory} onChange={(event) => setMemory(event.target.checked)} />
          memory.enabled
        </label>
        <Actions
          copy={copy}
          busy={busy}
          onSave={() => void run(async () => (await api.setConfig("memory.enabled", memory)).notice)}
        />
      </section>
    </div>
  );
}

function RuntimePanel({
  describe,
  copy,
  busy,
  run,
}: {
  describe: ConfigDescribe;
  copy: SettingsCopy;
  busy: boolean;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
}) {
  const [draft, setDraft] = useState<Record<string, string | number | boolean>>({
    ...describe.blocks.react,
    ...describe.blocks.cost,
    ...describe.blocks.tasks,
    schedules_enabled: describe.blocks.schedules.enabled,
    bash_sandbox: describe.blocks.security.bash_sandbox,
    require_approval: describe.blocks.security.require_approval,
    approval_policy: describe.blocks.security.approval_policy,
    allowlist: describe.blocks.security.approval_allowlist.join(", "),
    ui_mode: describe.blocks.display.ui_mode,
    verbosity: describe.blocks.display.verbosity,
  });
  const set = (key: string, value: string | number | boolean) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const saveKeys: Array<[string, unknown]> = [
    ["react.max_iterations", Number(draft.max_iterations)],
    ["react.max_tool_calls", Number(draft.max_tool_calls)],
    ["react.max_seconds", Number(draft.max_seconds)],
    ["react.stall_timeout_s", Number(draft.stall_timeout_s)],
    ["react.stream_max_retries", Number(draft.stream_max_retries)],
    ["react.finalization_timeout_s", Number(draft.finalization_timeout_s)],
    ["react.self_review", Boolean(draft.self_review)],
    ["cost.enabled", Boolean(draft.enabled)],
    ["cost.max_total_tokens", Number(draft.max_total_tokens)],
    ["cost.max_cost_usd", Number(draft.max_cost_usd)],
    ["cost.warn_total_tokens", Number(draft.warn_total_tokens)],
    ["cost.warn_cost_usd", Number(draft.warn_cost_usd)],
    ["tasks.auto_retry", Boolean(draft.auto_retry)],
    ["tasks.workflow_max_steps", Number(draft.workflow_max_steps)],
    ["tasks.workflow_max_tool_calls", Number(draft.workflow_max_tool_calls)],
    ["tasks.workflow_max_seconds", Number(draft.workflow_max_seconds)],
    ["schedules.enabled", Boolean(draft.schedules_enabled)],
    ["security.bash_sandbox", String(draft.bash_sandbox)],
    ["security.require_approval", Boolean(draft.require_approval)],
    ["security.approval_policy", String(draft.approval_policy)],
    [
      "security.approval_allowlist",
      String(draft.allowlist)
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
    ],
    ["display.ui_mode", String(draft.ui_mode)],
    ["display.verbosity", String(draft.verbosity)],
  ];
  return (
    <div className="settings-stack">
      <section className="settings-block">
        <h3>{copy.runtime}</h3>
        <div className="settings-grid">
          {([
            ["react.max_iterations", "max_iterations"],
            ["react.max_tool_calls", "max_tool_calls"],
            ["react.max_seconds", "max_seconds"],
            ["react.stall_timeout_s", "stall_timeout_s"],
            ["cost.max_total_tokens", "max_total_tokens"],
            ["cost.max_cost_usd", "max_cost_usd"],
            ["tasks.workflow_max_steps", "workflow_max_steps"],
          ] as const).map(([label, key]) => (
            <Field key={label} label={label}>
              <input
                className="settings-input"
                value={String(draft[key] ?? "")}
                onChange={(event) => set(key, event.target.value)}
              />
            </Field>
          ))}
          <Field label="security.bash_sandbox">
            <select
              className="settings-input"
              value={String(draft.bash_sandbox)}
              onChange={(event) => set("bash_sandbox", event.target.value)}
            >
              <option value="readonly">readonly</option>
              <option value="workspace-write">workspace-write</option>
              <option value="full">full</option>
            </select>
          </Field>
          <Field label="security.approval_policy">
            <input
              className="settings-input"
              value={String(draft.approval_policy)}
              onChange={(event) => set("approval_policy", event.target.value)}
            />
          </Field>
          <Field label="security.approval_allowlist">
            <input
              className="settings-input"
              value={String(draft.allowlist)}
              onChange={(event) => set("allowlist", event.target.value)}
            />
          </Field>
        </div>
        <label className="settings-check">
          <input
            type="checkbox"
            checked={Boolean(draft.require_approval)}
            onChange={(event) => set("require_approval", event.target.checked)}
          />
          security.require_approval
        </label>
        <label className="settings-check">
          <input
            type="checkbox"
            checked={Boolean(draft.schedules_enabled)}
            onChange={(event) => set("schedules_enabled", event.target.checked)}
          />
          schedules.enabled
        </label>
      </section>
      <section className="settings-block">
        <h3>{copy.cliDisplay}</h3>
        <div className="settings-grid">
          <Field label={copy.uiMode}>
            <select
              className="settings-input"
              value={String(draft.ui_mode)}
              onChange={(event) => set("ui_mode", event.target.value)}
            >
              <option value="auto">auto</option>
              <option value="tui">tui</option>
              <option value="classic">classic</option>
            </select>
          </Field>
          <Field label={copy.verbosity}>
            <select
              className="settings-input"
              value={String(draft.verbosity)}
              onChange={(event) => set("verbosity", event.target.value)}
            >
              <option value="quiet">quiet</option>
              <option value="normal">normal</option>
              <option value="verbose">verbose</option>
            </select>
          </Field>
        </div>
        <Actions
          copy={copy}
          busy={busy}
          onSave={() =>
            void run(async () => {
              for (const [key, value] of saveKeys) {
                await api.setConfig(key, value);
              }
              return copy.saved;
            })
          }
        />
      </section>
    </div>
  );
}


function AdvancedPanel({
  describe,
  copy,
  busy,
  run,
}: {
  describe: ConfigDescribe;
  copy: SettingsCopy;
  busy: boolean;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
}) {
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [got, setGot] = useState("");
  return (
    <div className="settings-stack">
      <section className="settings-block">
        <h3>{copy.advanced}</h3>
        <p className="muted">{copy.advancedHint}</p>
        <Field label={copy.key}>
          <input className="settings-input" value={key} onChange={(event) => setKey(event.target.value)} />
        </Field>
        <Field label={copy.value}>
          <input className="settings-input" value={value} onChange={(event) => setValue(event.target.value)} />
        </Field>
        {got ? <pre className="settings-pre">{got}</pre> : null}
        <div className="settings-actions">
          <button
            type="button"
            className="btn ghost"
            disabled={busy || !key.trim()}
            onClick={() =>
              void run(async () => {
                const result = await api.getConfig(key.trim());
                setGot(`${result.key} = ${formatValue(result.value)}`);
                return undefined;
              })
            }
          >
            {copy.get}
          </button>
          <button
            type="button"
            className="btn ghost"
            disabled={busy || !key.trim()}
            onClick={() =>
              void run(async () => {
                const result = await api.setConfig(key.trim(), parseAdvancedValue(value));
                return result.notice;
              })
            }
          >
            {copy.set}
          </button>
          <button
            type="button"
            className="btn ghost"
            disabled={busy || !key.trim()}
            onClick={() => void run(async () => (await api.unsetConfig(key.trim())).notice)}
          >
            {copy.unset}
          </button>
        </div>
      </section>
      <section className="settings-block">
        <h3>{copy.effective}</h3>
        <table className="settings-table">
          <tbody>
            {describe.rows.map((row) => (
              <tr key={row.key}>
                <th>{row.key}</th>
                <td>{row.secret ? (row.set ? "***set***" : "—") : formatValue(row.value) || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

function InterfacePanel({
  prefs,
  copy,
  onPrefs,
}: {
  prefs: UiPrefs;
  copy: SettingsCopy;
  onPrefs: (next: UiPrefs) => void;
}) {
  const update = (patch: Partial<UiPrefs>) => {
    const next = { ...prefs, ...patch };
    onPrefs(next);
    applyTheme(next.theme);
    applyLocale(next.locale);
  };
  return (
    <section className="settings-block">
      <h3>{copy.interface}</h3>
      <Field label={copy.theme}>
        <select
          className="settings-input"
          value={prefs.theme}
          onChange={(event) => update({ theme: event.target.value as ThemePreference })}
        >
          <option value="system">{copy.themeSystem}</option>
          <option value="light">{copy.themeLight}</option>
          <option value="dark">{copy.themeDark}</option>
        </select>
      </Field>
      <Field label={copy.language}>
        <select
          className="settings-input"
          value={prefs.locale}
          onChange={(event) => update({ locale: event.target.value as LocalePreference })}
        >
          <option value="zh">{copy.langZh}</option>
          <option value="en">{copy.langEn}</option>
        </select>
      </Field>
    </section>
  );
}
