import { useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../api";
import type { ConfigDescribe } from "../configTypes";
import { IconChevronUp, IconClose, IconFolder, IconPlus, IconSearch } from "../icons";
import type { SettingsCopy } from "../settingsCopy";
import type { SkillDetail, SkillSummary } from "../skillTypes";
import type { DirectoryListing } from "../types";
import { Field } from "./settingsFields";

type SkillTab = "skills" | "mcp";
type PendingAction = "trust" | "remove" | null;

type SkillSettingsProps = {
  copy: SettingsCopy;
  describe: ConfigDescribe;
  busy: boolean;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
  initialSkills?: SkillSummary[];
  initialSelected?: string;
  initialTab?: SkillTab;
};

export function SkillSettings({
  copy,
  describe,
  busy,
  run,
  initialSkills,
  initialSelected,
  initialTab = "skills",
}: SkillSettingsProps) {
  const [tab, setTab] = useState<SkillTab>(initialTab);
  const [skills, setSkills] = useState<SkillSummary[]>(initialSkills || []);
  const [selectedId, setSelectedId] = useState(initialSelected || initialSkills?.[0]?.skill_id || "");
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<PendingAction>(null);
  const [picker, setPicker] = useState<DirectoryListing | null>(null);
  const [showHidden, setShowHidden] = useState(false);
  const [working, setWorking] = useState(false);

  const filtered = useMemo(() => skills.filter((skill) => matchesSkill(skill, query)), [query, skills]);
  const selected = filtered.find((skill) => skill.skill_id === selectedId) || filtered[0] || null;
  const view = detail && selected && detail.skill_id === selected.skill_id ? detail : selected;

  const reload = async (selectId = selectedId) => {
    const data = await api.listSkills();
    setSkills(data.skills);
    const next = data.skills.some((skill) => skill.skill_id === selectId)
      ? selectId
      : data.skills[0]?.skill_id || "";
    setSelectedId(next);
    return data.skills;
  };

  useEffect(() => {
    if (initialSkills) return;
    void reload().catch((err: unknown) => {
      setError(err instanceof Error ? err.message : copy.failed);
    });
  }, []);

  useEffect(() => {
    if (!selected?.skill_id || initialSkills) return;
    let cancelled = false;
    void api
      .getSkill(selected.skill_id)
      .then((data) => {
        if (!cancelled) setDetail(data.skill);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : copy.failed);
      });
    return () => {
      cancelled = true;
    };
  }, [copy.failed, initialSkills, selected?.skill_id]);

  const mutate = async (task: () => Promise<string | undefined>) => {
    setWorking(true);
    setError("");
    try {
      const message = await task();
      setNotice(message || copy.skillNotice);
      setPending(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : copy.failed);
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="settings-stack">
      <div className="settings-subnav" role="tablist" aria-label={copy.skills}>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "skills"}
          className={tab === "skills" ? "active" : ""}
          data-settings-tab="skills"
          onClick={() => setTab("skills")}
        >
          {copy.skillsTab}
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "mcp"}
          className={tab === "mcp" ? "active" : ""}
          data-settings-tab="mcp"
          onClick={() => setTab("mcp")}
        >
          {copy.mcpTab}
        </button>
      </div>
      {error ? (
        <p className="banner error" role="alert">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="banner" role="status">
          {notice}
        </p>
      ) : null}
      {tab === "mcp" ? (
        <McpPanel copy={copy} describe={describe} busy={busy} run={run} />
      ) : (
        <div className="skill-catalog">
          <div className="skill-catalog-toolbar">
            <label className="skill-search">
              <IconSearch size={14} />
              <input
                className="settings-input"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={copy.skillSearch}
                aria-label={copy.skillSearch}
              />
            </label>
            <button type="button" className="btn" disabled={working} onClick={() => void openPicker("")}>
              <IconPlus size={14} />
              {copy.skillAdd}
            </button>
          </div>
          <p className="muted skill-add-hint">{copy.skillAddHint}</p>
          <div className="skill-catalog-shell">
            <div className="skill-list" role="listbox" aria-label={copy.skillsTab}>
              {filtered.length === 0 ? <p className="muted">{copy.skillEmpty}</p> : null}
              {filtered.map((skill) => (
                <button
                  key={skill.skill_id}
                  type="button"
                  role="option"
                  aria-selected={selected?.skill_id === skill.skill_id}
                  className={selected?.skill_id === skill.skill_id ? "active" : ""}
                  data-skill-id={skill.skill_id}
                  onClick={() => setSelectedId(skill.skill_id)}
                >
                  <span className="skill-list-title">{skill.name}</span>
                  <span className="skill-badges">{skillBadges(skill, copy)}</span>
                  <span className="muted">{skill.description}</span>
                </button>
              ))}
            </div>
            <div className="skill-detail">
              {view ? (
                <>
                  <div className="skill-detail-head">
                    <div>
                      <h3>{view.name}</h3>
                      <p className="muted">
                        {copy.skillKind} {view.kind} · {copy.skillDelivery} {view.delivery_mode}
                      </p>
                    </div>
                    <div className="skill-detail-actions">
                      {view.can_trust ? (
                        <button
                          type="button"
                          className="btn"
                          data-skill-action="trust"
                          disabled={working}
                          onClick={() => setPending("trust")}
                        >
                          {copy.skillTrust}
                        </button>
                      ) : null}
                      {view.can_untrust ? (
                        <button
                          type="button"
                          className="btn ghost"
                          data-skill-action="untrust"
                          disabled={working}
                          onClick={() =>
                            void mutate(async () => {
                              const result = await api.untrustSkill(view.skill_id);
                              await reload(view.skill_id);
                              return result.notice || copy.skillNotice;
                            })
                          }
                        >
                          {copy.skillUntrust}
                        </button>
                      ) : null}
                      {view.can_remove ? (
                        <button
                          type="button"
                          className="btn ghost"
                          data-skill-action="remove"
                          disabled={working}
                          onClick={() => setPending("remove")}
                        >
                          {copy.skillRemove}
                        </button>
                      ) : null}
                    </div>
                  </div>
                  {pending ? (
                    <div className="skill-confirm" role="alertdialog">
                      <p>{pending === "trust" ? copy.skillTrustConfirm : copy.skillRemoveConfirm}</p>
                      {isSkillDetail(view) && view.executable_files.length > 0 ? (
                        <p className="muted">{view.executable_files.join(", ")}</p>
                      ) : null}
                      <div className="settings-actions">
                        <button type="button" className="btn ghost" onClick={() => setPending(null)}>
                          {copy.skillCancel}
                        </button>
                        <button
                          type="button"
                          className="btn"
                          disabled={working}
                          onClick={() =>
                            void mutate(async () => {
                              const result =
                                pending === "trust"
                                  ? await api.trustSkill(view.skill_id)
                                  : await api.removeSkill(view.skill_id);
                              await reload(pending === "remove" ? "" : view.skill_id);
                              return result.notice || copy.skillNotice;
                            })
                          }
                        >
                          {copy.skillConfirm}
                        </button>
                      </div>
                    </div>
                  ) : null}
                  {view.shadowed ? (
                    <p className="muted">
                      {copy.skillShadowed}
                      {view.shadowed_by ? ` · ${view.shadowed_by}` : ""}
                    </p>
                  ) : null}
                  <p>{view.description}</p>
                  {isSkillDetail(view) && view.body ? (
                    <pre className="settings-pre skill-body" aria-label={copy.skillBody}>
                      {view.body}
                    </pre>
                  ) : null}
                </>
              ) : (
                <p className="muted">{copy.skillEmpty}</p>
              )}
            </div>
          </div>
        </div>
      )}
      {picker ? (
        <SkillDirectoryPicker
          copy={copy}
          listing={picker}
          showHidden={showHidden}
          busy={working}
          onClose={() => setPicker(null)}
          onToggleHidden={(next) => {
            setShowHidden(next);
            void openPicker(picker.path, next);
          }}
          onOpen={(path) => void openPicker(path, showHidden)}
          onImport={(path) =>
            void mutate(async () => {
              const result = await api.addSkill(path);
              setPicker(null);
              await reload(result.skill_id);
              return result.notice || copy.skillNotice;
            })
          }
        />
      ) : null}
    </div>
  );

  async function openPicker(path: string, hidden = showHidden) {
    setError("");
    try {
      setPicker(await api.listDirectory(path, hidden));
    } catch (err) {
      setError(err instanceof Error ? err.message : copy.failed);
    }
  }
}

function isSkillDetail(view: SkillSummary | SkillDetail): view is SkillDetail {
  return "body" in view && Array.isArray(view.executable_files);
}

function matchesSkill(skill: SkillSummary, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return [skill.name, skill.description, skill.source, skill.skill_id].some((part) =>
    part.toLowerCase().includes(needle),
  );
}

function skillBadges(skill: SkillSummary, copy: SettingsCopy) {
  const badges = [skill.source === "builtin" ? copy.skillBuiltin : copy.skillUser];
  if (!skill.trusted) badges.push(copy.skillQuarantined);
  else if (skill.source === "user_omni") badges.push(copy.skillTrusted);
  if (skill.shadowed) badges.push(copy.skillShadowed);
  return badges.map((label) => (
    <span key={label} className="skill-badge">
      {label}
    </span>
  ));
}

function McpPanel({
  copy,
  describe,
  busy,
  run,
}: {
  copy: SettingsCopy;
  describe: ConfigDescribe;
  busy: boolean;
  run: (task: () => Promise<string | undefined>) => Promise<void>;
}) {
  const [mcpName, setMcpName] = useState("");
  const [mcpCommand, setMcpCommand] = useState("");
  return (
    <section className="settings-block">
      <h3>MCP</h3>
      {describe.mcp_servers.length === 0 ? <p className="muted">—</p> : null}
      {describe.mcp_servers.map((server) => (
        <div key={server.name} className="settings-mcp-row">
          <code>{server.name}</code>
          <span className="muted">{server.command}</span>
          <button
            type="button"
            className="btn ghost"
            disabled={busy}
            onClick={() => void run(async () => (await api.unsetConfig(`mcp_servers.${server.name}`)).notice)}
          >
            {copy.remove}
          </button>
        </div>
      ))}
      <Field label={copy.mcpName}>
        <input className="settings-input" value={mcpName} onChange={(event) => setMcpName(event.target.value)} />
      </Field>
      <Field label={copy.mcpCommand}>
        <input className="settings-input" value={mcpCommand} onChange={(event) => setMcpCommand(event.target.value)} />
      </Field>
      <div className="settings-actions">
        <button
          type="button"
          className="btn"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              const name = mcpName.trim();
              if (!name || !mcpCommand.trim()) return copy.failed;
              const looksUrl = mcpCommand.includes("://");
              await api.setConfig(looksUrl ? `mcp_servers.${name}.url` : `mcp_servers.${name}.command`, mcpCommand.trim());
              await api.setConfig(`mcp_servers.${name}.enabled`, true);
              return copy.saved;
            })
          }
        >
          {busy ? copy.saving : copy.save}
        </button>
      </div>
    </section>
  );
}

function SkillDirectoryPicker({
  copy,
  listing,
  showHidden,
  busy,
  onClose,
  onToggleHidden,
  onOpen,
  onImport,
}: {
  copy: SettingsCopy;
  listing: DirectoryListing;
  showHidden: boolean;
  busy: boolean;
  onClose: () => void;
  onToggleHidden: (next: boolean) => void;
  onOpen: (path: string) => void;
  onImport: (path: string) => void;
}) {
  return (
    <div className="modal-backdrop skill-picker-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="modal skill-picker"
        role="dialog"
        aria-modal="true"
        aria-labelledby="skill-dir-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div>
            <h2 id="skill-dir-title">{copy.skillPickDir}</h2>
            <p className="muted">{copy.skillAddHint}</p>
          </div>
          <button type="button" className="icon-btn square" aria-label={copy.skillCancel} onClick={onClose}>
            <IconClose size={17} />
          </button>
        </header>
        <div className="directory-location">
          <div className="crumbs">
            {listing.breadcrumbs.map((crumb) => (
              <button key={crumb.path} type="button" onClick={() => onOpen(crumb.path)}>
                {crumb.name}
              </button>
            ))}
          </div>
          <code title={listing.path}>{listing.path}</code>
        </div>
        <div className="entries">
          {listing.parent ? (
            <button type="button" className="entry" onClick={() => onOpen(listing.parent || "")}>
              <span className="row">
                <IconChevronUp size={15} />
                {copy.skillUp}
              </span>
            </button>
          ) : null}
          {listing.entries
            .filter((entry) => entry.is_dir)
            .map((entry) => (
              <button key={entry.path} type="button" className="entry" onClick={() => onOpen(entry.path)}>
                <span className="row">
                  <IconFolder size={15} />
                  {entry.name}
                </span>
              </button>
            ))}
        </div>
        <footer>
          <label className="row muted">
            <input type="checkbox" checked={showHidden} onChange={(event) => onToggleHidden(event.target.checked)} />
            {copy.skillShowHidden}
          </label>
          <span className="grow" />
          <button type="button" className="btn ghost" onClick={onClose}>
            {copy.skillCancel}
          </button>
          <button type="button" className="btn" disabled={busy} onClick={() => onImport(listing.path)}>
            {copy.skillUseDir}
          </button>
        </footer>
      </div>
    </div>
  );
}

export function visibleSkillActions(skill: SkillSummary): string[] {
  const actions: string[] = [];
  if (skill.can_trust) actions.push("trust");
  if (skill.can_untrust) actions.push("untrust");
  if (skill.can_remove) actions.push("remove");
  return actions;
}
