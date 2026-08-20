import { useEffect, useMemo, useState } from "react";
import { IconCheck, IconPersona, IconRefresh, IconSearch, IconSteer } from "../icons";
import type {
  PersonaAction,
  PersonaSnapshot,
  PersonaStartRequest,
  ScientistPersona,
} from "../personaTypes";
import type { LocalePreference } from "../uiPrefs";

type PersonaControlProps = {
  locale: LocalePreference;
  snapshot: PersonaSnapshot | null;
  loading: boolean;
  busy: boolean;
  error?: string;
  notice?: string;
  folderPath?: string;
  pendingScientistId?: string;
  pendingAction?: PersonaAction | "";
  onStart: (request: PersonaStartRequest) => Promise<void>;
};

const COPY = {
  zh: {
    title: "学术人格",
    lead: "让 Omni 以一位科学家的研究品味来判断问题，同时保留原有工具、安全与引用规则。",
    scope: "当前文件夹",
    standard: "标准 Omni",
    standardHint: "未启用学术人格",
    active: "当前文件夹已启用",
    available: "可用人格",
    manage: "管理全部",
    folderLabel: "已打开的文件夹",
    activateHint: "人格是当前文件夹的默认设置。研究任务请启用后在下方输入框发送。",
    activate: "启用人格",
    switch: "切换人格",
    alreadyEnabled: "已启用",
    restore: "恢复标准 Omni",
    empty: "还没有可用的科学家人格。可在 CLI 运行 /soul create <scientist> 创建。",
    loading: "正在读取学术人格…",
    configuring: "SoulAgent 正在为当前文件夹配置人格…",
    unloading: "正在恢复标准 Omni…",
    search: "搜索科学家",
    aliases: "别名",
    sourceProject: "当前项目人格库",
    sourceHome: "Omni 人格库",
    safety: "仅影响当前文件夹后续进入 ReAct 的判断与表达，不改变工具、安全和引用规则。子文件夹不会继承。",
    invalid: "存在无法读取的人格目录",
    retry: "重新读取",
    select: "选择一位科学家",
    noMatches: "没有匹配的科学家人格",
    readOnly: "当前文件夹不可写；请先在侧栏信任该文件夹。",
    mutationBusy: "正在写入人格；写入完成前不能再次调整，但不影响发送研究消息。",
  },
  en: {
    title: "Scientist persona",
    lead: "Use a scientist's research taste while keeping Omni's tools, safety, and citation rules.",
    scope: "Current folder",
    standard: "Standard Omni",
    standardHint: "No scientist persona is active",
    active: "Active in this folder",
    available: "Available personas",
    manage: "Manage all",
    folderLabel: "Opened folder",
    activateHint: "This becomes the folder default. Send the research task in the composer after it is enabled.",
    activate: "Activate persona",
    switch: "Switch persona",
    alreadyEnabled: "Enabled",
    restore: "Restore standard Omni",
    empty: "No scientist personas are available. Run /soul create <scientist> in the CLI.",
    loading: "Loading scientist personas…",
    configuring: "Configuring this folder with SoulAgent…",
    unloading: "Restoring standard Omni…",
    search: "Search scientists",
    aliases: "Aliases",
    sourceProject: "Project persona library",
    sourceHome: "Omni persona library",
    safety: "Only the next ReAct judgment and expression in this folder change. Tools, safety, and citation rules do not. Subfolders do not inherit.",
    invalid: "Some persona directories could not be read",
    retry: "Reload",
    select: "Select a scientist",
    noMatches: "No scientist personas match this search",
    readOnly: "This folder is read-only. Trust it from the sidebar before making changes.",
    mutationBusy: "Persona settings stay locked while SoulAgent writes. You can still send research messages.",
  },
} as const;

function initials(persona: ScientistPersona): string {
  const parts = persona.scientist_name.trim().split(/\s+/).filter(Boolean);
  if (parts.length > 1) return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
  return (parts[0] || persona.scientist_id).slice(0, 2).toUpperCase();
}

function actionFor(snapshot: PersonaSnapshot, scientistId: string): PersonaAction {
  if (!snapshot.active) return "activate";
  return snapshot.scientist_id === scientistId ? "activate" : "switch";
}

function alreadyEnabled(snapshot: PersonaSnapshot | null, scientistId: string): boolean {
  return Boolean(snapshot?.active && snapshot.scientist_id === scientistId);
}

function actionLabel(
  action: PersonaAction,
  copy: (typeof COPY)[LocalePreference],
): string {
  if (action === "switch") return copy.switch;
  return copy.activate;
}

function FolderPath({
  path,
  label,
}: {
  path?: string;
  label: string;
}) {
  if (!path) return null;
  return (
    <p className="persona-folder-path">
      <span>{label}</span>
      <code title={path}>{path}</code>
    </p>
  );
}

function PersonaAvatar({ persona, active = false }: { persona: ScientistPersona; active?: boolean }) {
  return (
    <span className={`persona-avatar${active ? " active" : ""}`} aria-hidden="true">
      {initials(persona)}
    </span>
  );
}

function PendingState({
  locale,
  action,
}: {
  locale: LocalePreference;
  action?: PersonaAction | "";
}) {
  const copy = COPY[locale];
  return (
    <span className="persona-pending" role="status">
      <IconRefresh size={14} />
      {action === "unload" ? copy.unloading : copy.configuring}
    </span>
  );
}

export function PersonaQuickStart({
  locale,
  snapshot,
  loading,
  busy,
  error = "",
  notice = "",
  folderPath = "",
  pendingAction = "",
  onStart,
  onManage,
}: PersonaControlProps & { onManage: () => void }) {
  const copy = COPY[locale];
  const [selectedId, setSelectedId] = useState(snapshot?.scientist_id || snapshot?.available[0]?.scientist_id || "");

  useEffect(() => {
    if (!snapshot) return;
    if (!snapshot.available.some((item) => item.scientist_id === selectedId)) {
      setSelectedId(snapshot.scientist_id || snapshot.available[0]?.scientist_id || "");
    }
  }, [selectedId, snapshot]);

  if (loading && !snapshot) {
    return (
      <section className="persona-quick persona-loading" aria-busy="true">
        <IconPersona size={18} />
        <span>{copy.loading}</span>
      </section>
    );
  }
  if (!snapshot && error) {
    return (
      <section className="persona-quick" aria-labelledby="persona-quick-title">
        <div className="persona-quick-heading">
          <div className="persona-heading-icon" aria-hidden="true"><IconPersona size={17} /></div>
          <div><strong id="persona-quick-title">{copy.title}</strong><span>{copy.scope}</span></div>
          <button type="button" className="persona-manage" onClick={onManage}>{copy.manage}</button>
        </div>
        <p className="persona-feedback error" role="alert">{error}</p>
      </section>
    );
  }

  const inventory = snapshot?.available || [];
  const selectedPriority = inventory.find((item) => item.scientist_id === selectedId);
  const activePriority = snapshot?.active
    ? inventory.find((item) => item.scientist_id === snapshot.scientist_id)
    : undefined;
  const priorityIds = new Set(
    [selectedPriority?.scientist_id, activePriority?.scientist_id].filter(Boolean),
  );
  const available = [
    ...(selectedPriority ? [selectedPriority] : []),
    ...(activePriority && activePriority.scientist_id !== selectedPriority?.scientist_id
      ? [activePriority]
      : []),
    ...inventory.filter((item) => !priorityIds.has(item.scientist_id)),
  ].slice(0, 4);
  const selected = snapshot?.available.find((item) => item.scientist_id === selectedId) || null;
  const action = snapshot && selected ? actionFor(snapshot, selected.scientist_id) : "activate";
  const enabled = selected ? alreadyEnabled(snapshot, selected.scientist_id) : false;
  const submit = async () => {
    if (!selected || enabled) return;
    await onStart({
      action,
      scientist_id: selected.scientist_id,
    });
  };

  return (
    <section className="persona-quick" aria-labelledby="persona-quick-title">
      <div className="persona-quick-heading">
        <div className="persona-heading-icon" aria-hidden="true">
          <IconPersona size={17} />
        </div>
        <div>
          <strong id="persona-quick-title">{copy.title}</strong>
          <span>{snapshot?.active ? `${snapshot.scientist_name} · ${copy.scope}` : copy.standardHint}</span>
        </div>
        <div className="persona-quick-tools">
          {snapshot?.active ? (
            <button
              type="button"
              className="persona-manage"
              disabled={busy || !snapshot.writable}
              onClick={() => void onStart({ action: "unload" })}
            >
              {copy.restore}
            </button>
          ) : null}
          <button type="button" className="persona-manage" onClick={onManage}>
            {copy.manage}
          </button>
        </div>
      </div>
      {available.length ? (
        <div className="persona-quick-list" role="group" aria-label={copy.available}>
          {available.map((persona) => {
            const selectedPersona = persona.scientist_id === selectedId;
            const activePersona = Boolean(
              snapshot?.active && snapshot.scientist_id === persona.scientist_id,
            );
            return (
              <button
                key={persona.scientist_id}
                type="button"
                className={selectedPersona ? "selected" : ""}
                aria-pressed={selectedPersona}
                disabled={busy}
                onClick={() => setSelectedId(persona.scientist_id)}
              >
                <PersonaAvatar persona={persona} active={activePersona} />
                <span>{persona.scientist_name}</span>
                {activePersona ? <IconCheck size={13} aria-label={copy.active} /> : null}
              </button>
            );
          })}
        </div>
      ) : (
        <p className="persona-empty">{copy.empty}</p>
      )}
      <FolderPath path={folderPath} label={copy.folderLabel} />
      {selected && !enabled ? (
        <div className="persona-quick-action">
          <button
            type="button"
            className="btn persona-activate"
            disabled={busy || !snapshot?.writable}
            onClick={() => void submit()}
          >
            <IconSteer size={15} />
            {actionLabel(action, copy)}
          </button>
        </div>
      ) : null}
      {busy && pendingAction ? <PendingState locale={locale} action={pendingAction} /> : null}
      {busy && !pendingAction ? <p className="persona-feedback" role="status">{copy.mutationBusy}</p> : null}
      {error ? <p className="persona-feedback error" role="alert">{error}</p> : null}
      {snapshot && !snapshot.writable ? <p className="persona-feedback error">{copy.readOnly}</p> : null}
      {notice && !error ? <p className="persona-feedback" role="status">{notice}</p> : null}
      {!error ? <p className="persona-quick-hint">{copy.activateHint}</p> : null}
    </section>
  );
}

export function PersonaSettings({
  locale,
  snapshot,
  loading,
  busy,
  error = "",
  notice = "",
  folderPath = "",
  pendingScientistId = "",
  pendingAction = "",
  onStart,
  onReload,
}: PersonaControlProps & { onReload: () => Promise<void> }) {
  const copy = COPY[locale];
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(snapshot?.scientist_id || snapshot?.available[0]?.scientist_id || "");
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    const available = snapshot?.available || [];
    if (!needle) return available;
    return available.filter((item) =>
      [item.scientist_name, item.scientist_id, ...item.aliases]
        .join(" ")
        .toLocaleLowerCase()
        .includes(needle),
    );
  }, [query, snapshot?.available]);

  useEffect(() => {
    if (!snapshot) return;
    if (!snapshot.available.some((item) => item.scientist_id === selectedId)) {
      setSelectedId(snapshot.scientist_id || snapshot.available[0]?.scientist_id || "");
    }
  }, [selectedId, snapshot]);

  if (loading && !snapshot) {
    return <p className="muted persona-settings-loading" aria-busy="true">{copy.loading}</p>;
  }
  if (!snapshot) {
    return (
      <div className="settings-stack">
        <p className="banner error" role="alert">{error || copy.select}</p>
        <button type="button" className="btn ghost persona-reload" onClick={() => void onReload()}>
          <IconRefresh size={14} />
          {copy.retry}
        </button>
      </div>
    );
  }

  const selected = filtered.find((item) => item.scientist_id === selectedId) || filtered[0] || null;
  const action = selected ? actionFor(snapshot, selected.scientist_id) : "activate";
  const enabled = selected ? alreadyEnabled(snapshot, selected.scientist_id) : false;
  const source = snapshot.scanner === "project" ? copy.sourceProject : copy.sourceHome;
  const submit = async () => {
    if (!selected || enabled) return;
    await onStart({
      action,
      scientist_id: selected.scientist_id,
    });
  };

  return (
    <div className="settings-stack persona-settings">
      <section className="persona-status-card">
        <div className="persona-heading-icon" aria-hidden="true"><IconPersona size={18} /></div>
        <div>
          <span className="persona-kicker">{copy.scope}</span>
          <h3>{snapshot.active ? snapshot.scientist_name : copy.standard}</h3>
          <p>{snapshot.active ? copy.active : copy.standardHint} · {source}</p>
          <FolderPath path={folderPath} label={copy.folderLabel} />
        </div>
        <div className="persona-status-actions">
          <button
            type="button"
            className="btn ghost persona-reload"
            disabled={busy || loading}
            onClick={() => void onReload()}
          >
            <IconRefresh size={14} />
            {copy.retry}
          </button>
          {snapshot.active ? (
            <button
              type="button"
              className="btn ghost persona-restore"
              disabled={busy || !snapshot.writable}
              onClick={() => void onStart({ action: "unload" })}
            >
              {copy.restore}
            </button>
          ) : null}
        </div>
      </section>
      <div>
        <h3 className="persona-section-title">{copy.available}</h3>
        <p className="muted persona-safety">{copy.safety}</p>
      </div>
      {error ? <p className="banner error" role="alert">{error}</p> : null}
      {!snapshot.writable ? <p className="banner error">{copy.readOnly}</p> : null}
      {notice && !error ? <p className="banner" role="status">{notice}</p> : null}
      {busy && pendingAction ? <PendingState locale={locale} action={pendingAction} /> : null}
      {busy && !pendingAction ? <p className="persona-feedback" role="status">{copy.mutationBusy}</p> : null}
      {snapshot.available.length ? (
        <div className="persona-catalog-shell">
          <div className="persona-catalog-sidebar">
            <label className="persona-search">
              <IconSearch size={14} />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={copy.search}
                aria-label={copy.search}
              />
            </label>
            <div className="persona-list" role="group" aria-label={copy.available}>
              {filtered.map((persona) => {
                const active = snapshot.active && snapshot.scientist_id === persona.scientist_id;
                const pending = busy && pendingScientistId === persona.scientist_id;
                return (
                  <button
                    key={persona.scientist_id}
                    type="button"
                    aria-pressed={selected?.scientist_id === persona.scientist_id}
                    className={selected?.scientist_id === persona.scientist_id ? "active" : ""}
                    disabled={busy}
                    onClick={() => setSelectedId(persona.scientist_id)}
                  >
                    <PersonaAvatar persona={persona} active={active} />
                    <span className="persona-list-copy">
                      <strong>{persona.scientist_name}</strong>
                      <small>{active ? copy.active : pending ? copy.configuring : persona.scientist_id}</small>
                    </span>
                    {active ? <IconCheck size={14} /> : null}
                  </button>
                );
              })}
              {!filtered.length ? <p className="persona-empty">{copy.noMatches}</p> : null}
            </div>
          </div>
          <div className="persona-detail">
            {selected ? (
              <>
                <div className="persona-detail-head">
                  <PersonaAvatar persona={selected} active={snapshot.active && snapshot.scientist_id === selected.scientist_id} />
                  <div>
                    <h3>{selected.scientist_name}</h3>
                    <p>{selected.scientist_id}</p>
                  </div>
                  {snapshot.active && snapshot.scientist_id === selected.scientist_id ? (
                    <span className="persona-active-badge"><IconCheck size={12} />{copy.active}</span>
                  ) : null}
                </div>
                <dl className="persona-facts">
                  <div><dt>{copy.aliases}</dt><dd>{selected.aliases.join(" · ") || "—"}</dd></div>
                  <div><dt>{copy.scope}</dt><dd>{source}</dd></div>
                </dl>
                {!error ? <p className="muted persona-activate-hint">{copy.activateHint}</p> : null}
                <div className="settings-actions persona-detail-actions">
                  <button
                    type="button"
                    className="btn"
                    disabled={busy || !snapshot.writable || enabled}
                    onClick={() => void submit()}
                  >
                    <IconSteer size={15} />
                    {enabled ? copy.alreadyEnabled : actionLabel(action, copy)}
                  </button>
                </div>
              </>
            ) : <p className="muted">{copy.select}</p>}
          </div>
        </div>
      ) : <p className="persona-empty">{copy.empty}</p>}
      {snapshot.invalid.length ? (
        <details className="persona-invalid">
          <summary>{copy.invalid} · {snapshot.invalid.length}</summary>
          <ul>{snapshot.invalid.map((item) => <li key={item.directory}><code>{item.directory}</code> — {item.error}</li>)}</ul>
        </details>
      ) : null}
    </div>
  );
}
