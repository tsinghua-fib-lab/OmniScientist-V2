import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { displayTitle, relativeTime, shortId, workerLabel } from "../format";
import {
  IconChannels,
  IconClose,
  IconFolder,
  IconFolderOpen,
  IconPanelLeftClose,
  IconPlus,
  IconRefresh,
  IconSearch,
  IconSettings,
} from "../icons";
import { trapFocus } from "../focus";
import { channelStatusUnavailableLabel, channelSummaryLabel } from "../channelStatus";
import type { ChannelSummary } from "../channelTypes";
import {
  DEFAULT_WORKSPACE_SECTION_HEIGHT,
  SIDEBAR_SPLIT_STORAGE_KEY,
  clampWorkspaceSectionHeight,
  maxWorkspaceSectionHeight,
  minWorkspaceSectionHeight,
  parseWorkspaceSectionHeight,
  serializeWorkspaceSectionHeight,
} from "../sidebarSplit";
import { actions, useAppState } from "../store";
import type { Session } from "../types";
import { SidebarSplitResizer } from "./SidebarSplitResizer";

const CHANNELS = ["", "cli", "web", "wechat", "feishu", "dingtalk"];

function readWorkspaceSectionHeight(): number {
  if (typeof window === "undefined") return DEFAULT_WORKSPACE_SECTION_HEIGHT;
  try {
    return parseWorkspaceSectionHeight(window.localStorage.getItem(SIDEBAR_SPLIT_STORAGE_KEY));
  } catch {
    return DEFAULT_WORKSPACE_SECTION_HEIGHT;
  }
}

function persistWorkspaceSectionHeight(workspaceHeight: number): void {
  try {
    window.localStorage.setItem(
      SIDEBAR_SPLIT_STORAGE_KEY,
      serializeWorkspaceSectionHeight(workspaceHeight),
    );
  } catch {
    // A private or quota-limited browser can still use the split for this page load.
  }
}

type SidebarProps = {
  onNavigate?: () => void;
  overlay?: boolean;
  open?: boolean;
  blocked?: boolean;
  hidden?: boolean;
  onCollapse?: () => void;
  onOpenSettings?: () => void;
  settingsLabel?: string;
  channelSummary?: ChannelSummary;
  channelStatusAvailable?: boolean;
  onOpenChannels?: () => void;
  locale?: "zh" | "en";
};

export function Sidebar({
  onNavigate,
  overlay = false,
  open = false,
  blocked = false,
  hidden = false,
  onCollapse,
  onOpenSettings,
  settingsLabel = "设置",
  channelSummary,
  channelStatusAvailable = true,
  onOpenChannels,
  locale = "zh",
}: SidebarProps) {
  const { catalog, sessions, sessionId, workspace, channelFilter } = useAppState();
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [menuId, setMenuId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Session | null>(null);
  const [preferredWorkspaceHeight, setPreferredWorkspaceHeight] = useState(
    readWorkspaceSectionHeight,
  );
  const [splitContainerHeight, setSplitContainerHeight] = useState(640);
  const [splitDragging, setSplitDragging] = useState(false);
  const splitContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuId) return;
    const close = () => setMenuId(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [menuId]);

  useEffect(() => {
    const container = splitContainerRef.current;
    if (!container) return;
    const updateHeight = () => {
      const height = Math.round(container.getBoundingClientRect().height);
      if (height > 0) setSplitContainerHeight(height);
    };
    updateHeight();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", updateHeight);
      return () => window.removeEventListener("resize", updateHeight);
    }
    const observer = new ResizeObserver(updateHeight);
    observer.observe(container);
    return () => observer.disconnect();
  }, []);
  const filteredCatalog = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return catalog;
    return catalog.filter((item) =>
      `${item.label} ${item.root || ""} ${item.project_dir}`.toLowerCase().includes(q),
    );
  }, [catalog, query]);
  const unavailable = hidden || blocked || (overlay && !open);
  const workspaceHeightMin = minWorkspaceSectionHeight(splitContainerHeight);
  const workspaceHeightMax = maxWorkspaceSectionHeight(splitContainerHeight);
  const workspaceHeight = clampWorkspaceSectionHeight(
    preferredWorkspaceHeight,
    splitContainerHeight,
  );
  const splitStyle = {
    "--workspace-section-height": `${workspaceHeight}px`,
  } as CSSProperties;
  const previewWorkspaceHeight = (height: number) => {
    splitContainerRef.current?.style.setProperty("--workspace-section-height", `${height}px`);
  };
  const setSidebarSplitDragging = (active: boolean) => {
    splitContainerRef.current?.classList.toggle("is-resizing", active);
    setSplitDragging(active);
  };
  const channelLabel = channelSummary
    ? channelStatusAvailable
      ? channelSummaryLabel(channelSummary, locale)
      : channelStatusUnavailableLabel(locale)
    : "";

  return (
    <aside
      id="workspace-sidebar"
      className="sidebar"
      aria-label="工作区与会话导航"
      aria-hidden={unavailable ? true : undefined}
      aria-modal={overlay ? true : undefined}
      role={overlay ? "dialog" : undefined}
      inert={unavailable ? "" : undefined}
      hidden={hidden}
      tabIndex={overlay ? -1 : undefined}
      onKeyDown={overlay ? trapFocus : undefined}
    >
      <div className="brand-row">
        <div className="brand-lockup">
          <span className="brand">Omni</span>
          <span className="brand-product">Scientist</span>
        </div>
        <div className="sidebar-head-actions">
          <button
            type="button"
            className="icon-btn square sidebar-collapse"
            aria-label="收起侧栏"
            title="收起侧栏"
            onClick={onCollapse}
          >
            <IconPanelLeftClose size={17} />
          </button>
          <button
            type="button"
            className="icon-btn square sidebar-close"
            aria-label="关闭导航"
            onClick={onNavigate}
          >
            <IconClose size={17} />
          </button>
        </div>
      </div>
      <button
        type="button"
        className="btn-new"
        disabled={!workspace?.writable}
        title={
          !workspace
            ? "请先选择工作区"
            : workspace.writable
              ? "新建 Web 会话"
              : "此工作区只读"
        }
        onClick={() => {
          void actions.newSession();
          onNavigate?.();
        }}
      >
        <IconPlus size={16} />
        新会话
      </button>
      <div className="side-actions">
        <button
          type="button"
          className="icon-btn"
          onClick={() => setSearchOpen((v) => !v)}
          aria-pressed={searchOpen}
        >
          <IconSearch size={16} />
          搜索
        </button>
        <button type="button" className="icon-btn" onClick={() => void actions.openPicker()}>
          <IconFolderOpen size={16} />
          打开目录
        </button>
        <button
          type="button"
          className="icon-btn square"
          aria-label="刷新工作区"
          onClick={() => void actions.refreshCatalog()}
        >
          <IconRefresh size={16} />
        </button>
      </div>
      {searchOpen && (
        <input
          className="search"
          placeholder="搜索工作区"
          aria-label="搜索工作区"
          value={query}
          autoFocus
          onChange={(e) => setQuery(e.target.value)}
        />
      )}
      <div
        ref={splitContainerRef}
        className={`sidebar-browser${splitDragging ? " is-resizing" : ""}`}
        style={splitStyle}
      >
      <section id="workspace-section" className="section workspace-section">
        <div className="section-head">
          <span>工作区</span>
          <span>{filteredCatalog.length}</span>
        </div>
        <div className="list" role="list">
          {filteredCatalog.length === 0 && <div className="empty">还没有打开过目录</div>}
          {filteredCatalog.map((item) => (
            <button
              key={item.project_dir}
              type="button"
              className={`item ${workspace?.project_dir === item.project_dir ? "active" : ""}`}
              aria-current={workspace?.project_dir === item.project_dir ? "page" : undefined}
              onClick={() => {
                void actions.selectCatalog(item);
                onNavigate?.();
              }}
            >
              <IconFolder size={15} className="item-leading" />
              <span className="item-copy">
                <span className="title">{item.label}</span>
                <span className="meta">
                  <span>{item.root || item.project_dir}</span>
                </span>
              </span>
            </button>
          ))}
        </div>
      </section>
      <SidebarSplitResizer
        controlsId="workspace-section session-section"
        value={workspaceHeight}
        min={workspaceHeightMin}
        max={workspaceHeightMax}
        defaultValue={DEFAULT_WORKSPACE_SECTION_HEIGHT}
        onChange={previewWorkspaceHeight}
        onCommit={(height) => {
          setPreferredWorkspaceHeight(height);
          persistWorkspaceSectionHeight(height);
        }}
        onDraggingChange={setSidebarSplitDragging}
      />
      <section id="session-section" className="section session-section">
        <div className="section-head">
          <span>会话</span>
          <label className="sr-only" htmlFor="channel-filter">
            按渠道筛选会话
          </label>
          <select
            id="channel-filter"
            className="select"
            value={channelFilter}
            onChange={(e) => actions.setChannelFilter(e.target.value)}
          >
            {CHANNELS.map((ch) => (
              <option key={ch || "all"} value={ch}>
                {ch || "全部渠道"}
              </option>
            ))}
          </select>
        </div>
        <div className="list">
          {!workspace && <div className="empty">选择一个工作区开始</div>}
          {workspace && sessions.length === 0 && <div className="empty">暂无会话</div>}
          {sessions.map((session) => {
            const title = displayTitle(session);
            return (
              <div
                key={session.id}
                className={`item session-item${sessionId === session.id ? " active" : ""}${
                  menuId === session.id ? " menu-open" : ""
                }`}
              >
                <button
                  type="button"
                  className="session-open"
                  aria-label={`打开会话：${title}`}
                  aria-current={sessionId === session.id ? "page" : undefined}
                  onClick={() => {
                    void actions.openSession(session.id);
                    onNavigate?.();
                  }}
                >
                  <span className="item-copy">
                    <span className="title session-title" title={title}>
                      {title}
                    </span>
                    <span className="meta">
                      <span className={`badge ${session.channel}`}>{session.channel}</span>
                      {workerLabel(session) && <span>{workerLabel(session)}</span>}
                      <span>{relativeTime(session.last_activity_at || session.updated_at)}</span>
                    </span>
                    <span className="meta session-sub">
                      <span>创建 {relativeTime(session.first_task_at || session.created_at)}</span>
                      {session.latest_task_id && <span>Task {shortId(session.latest_task_id)}</span>}
                    </span>
                  </span>
                </button>
                <button
                  type="button"
                  className="session-more"
                  aria-label={`会话菜单：${title}`}
                  aria-expanded={menuId === session.id}
                  title={`Session ${shortId(session.id)}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    setMenuId((current) => (current === session.id ? null : session.id));
                  }}
                >
                  ···
                </button>
                {menuId === session.id && (
                  <div
                    className="session-menu"
                    role="menu"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <button
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setMenuId(null);
                        const next = window.prompt(
                          `重命名会话（留空则恢复自动标题）\nSession ${shortId(session.id)}`,
                          session.title || "",
                        );
                        if (next !== null) void actions.renameSession(session.id, next.trim());
                      }}
                    >
                      重命名
                    </button>
                    <button
                      type="button"
                      role="menuitem"
                      className="danger"
                      disabled={!workspace?.writable}
                      onClick={() => {
                        setMenuId(null);
                        setPendingDelete(session);
                      }}
                    >
                      删除
                    </button>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </section>
      </div>
      <div className="side-foot">
        <div
          className="side-foot-row side-foot-actions"
          role="group"
          aria-label={locale === "en" ? "Channels and settings" : "渠道与设置"}
        >
          {channelSummary ? (
            <button
              type="button"
              className={`icon-btn channel-launch${
                !channelStatusAvailable
                  ? " warning"
                  : channelSummary.attention
                    ? " warning"
                    : channelSummary.running
                      ? " good"
                      : ""
              }`}
              onClick={onOpenChannels}
              title={channelLabel}
            >
              <IconChannels size={16} />
              <span className="channel-launch-copy">{channelLabel}</span>
              <span className="channel-status-dot" aria-hidden="true" />
            </button>
          ) : null}
          <button type="button" className="icon-btn settings-launch" onClick={onOpenSettings}>
            <IconSettings size={16} />
            {settingsLabel}
          </button>
        </div>
        <div className="side-foot-row side-foot-context">
          {workspace ? (
            <>
              <strong title={workspace.label}>{workspace.label}</strong>
              <span className={`trust-state${workspace.writable ? " writable" : ""}`}>
                <span className="trust-dot" aria-hidden="true" />
                {workspace.writable ? "已信任 · 可写入" : "未信任 · 只读"}
              </span>
            </>
          ) : (
            <span className="side-foot-placeholder">本地目录、本地会话、同一智能体</span>
          )}
        </div>
      </div>
      {pendingDelete && (
        <div
          className="modal-backdrop"
          role="presentation"
          onMouseDown={() => setPendingDelete(null)}
        >
          <div
            className="modal compact"
            role="dialog"
            aria-modal="true"
            aria-labelledby="session-delete-title"
            tabIndex={-1}
            onMouseDown={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                setPendingDelete(null);
              }
              trapFocus(event);
            }}
          >
            <header>
              <div>
                <h2 id="session-delete-title">删除会话</h2>
                <p className="muted">
                  {displayTitle(pendingDelete)}
                  {pendingDelete.messages
                    ? ` · ${pendingDelete.messages} 条消息`
                    : ""}
                </p>
              </div>
              <button
                type="button"
                className="icon-btn square"
                aria-label="取消删除"
                onClick={() => setPendingDelete(null)}
              >
                <IconClose size={17} />
              </button>
            </header>
            <div className="session-delete-body">
              <p>
                将删除这条会话及其关联的 Task。产物文件会保留。若仍有运行中的任务，删除会被拒绝。
              </p>
            </div>
            <footer>
              <button type="button" className="btn ghost" onClick={() => setPendingDelete(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn danger"
                onClick={() => {
                  const id = pendingDelete.id;
                  setPendingDelete(null);
                  void actions.deleteSession(id);
                  onNavigate?.();
                }}
              >
                删除
              </button>
            </footer>
          </div>
        </div>
      )}
    </aside>
  );
}
