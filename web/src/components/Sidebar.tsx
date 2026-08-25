import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { displayTitle, relativeTime, shortId } from "../format";
import { sessionStatusGroup, sessionStatusLabel } from "../sessionManagement";
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
import type {
  CatalogWorkspace,
  Session,
  SessionScope,
  SessionSort,
  SessionStatusGroup,
} from "../types";
import { SidebarSplitResizer } from "./SidebarSplitResizer";

const CHANNELS = ["", "cli", "web", "wechat", "feishu", "dingtalk"];
const SESSION_SORTS: Array<{ value: SessionSort; label: string }> = [
  { value: "activity", label: "最近活动" },
  { value: "started", label: "最近执行" },
  { value: "completed", label: "最近完成" },
  { value: "created", label: "创建时间" },
];
const SESSION_STATUSES: Array<{ value: SessionStatusGroup | ""; label: string }> = [
  { value: "", label: "全部状态" },
  { value: "running", label: "执行中" },
  { value: "needs_attention", label: "待处理" },
  { value: "completed", label: "已完成" },
  { value: "warning", label: "有警告" },
  { value: "error", label: "有问题" },
  { value: "cancelled", label: "已取消" },
  { value: "empty", label: "暂无任务" },
];

function SessionCopy({ session, global }: { session: Session; global: boolean }) {
  const title = displayTitle(session);
  const status = sessionStatusGroup(session);
  return (
    <span className="item-copy">
      <span className="title session-title" title={title}>
        {title}
      </span>
      <span className="meta">
        <span className={`badge ${session.channel}`}>{session.channel}</span>
        <span className={`session-status ${status}`}>{sessionStatusLabel(session)}</span>
        <span>{relativeTime(session.last_activity_at || session.updated_at)}</span>
      </span>
      <span className="meta session-sub">
        {global && <span className="workspace-chip">{session.workspace_label || "未知工作区"}</span>}
        <span>创建 {relativeTime(session.first_task_at || session.created_at)}</span>
        {session.latest_task_id && <span>Task {shortId(session.latest_task_id)}</span>}
      </span>
    </span>
  );
}

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
  const {
    catalog,
    hiddenWorkspaces = [],
    sessions,
    sessionResults = sessions,
    sessionId,
    workspace,
    channelFilter,
    sessionScope = "workspace",
    sessionSort = "activity",
    sessionStatusFilter = "",
    sessionNextCursor = null,
    sessionListLoading = false,
    sessionListError = "",
  } = useAppState();
  const [query, setQuery] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [menuId, setMenuId] = useState<string | null>(null);
  const [workspaceManage, setWorkspaceManage] = useState(false);
  const [sessionManage, setSessionManage] = useState(false);
  const [selectedWorkspaces, setSelectedWorkspaces] = useState<Set<string>>(() => new Set());
  const [selectedSessions, setSelectedSessions] = useState<Set<string>>(() => new Set());
  const [pendingDelete, setPendingDelete] = useState<Session[] | null>(null);
  const [pendingHide, setPendingHide] = useState<CatalogWorkspace[] | null>(null);
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
    setSelectedWorkspaces((current) => {
      const available = new Set(catalog.map((item) => item.project_dir));
      return new Set([...current].filter((id) => available.has(id)));
    });
  }, [catalog]);

  useEffect(() => {
    setSelectedSessions((current) => {
      const available = new Set(sessionResults.map((item) => item.id));
      return new Set([...current].filter((id) => available.has(id)));
    });
  }, [sessionResults]);

  useEffect(() => {
    if (sessionScope !== "all") return;
    setSessionManage(false);
    setSelectedSessions(new Set());
  }, [sessionScope]);

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
  const selectedWorkspaceRows = filteredCatalog.filter((item) =>
    selectedWorkspaces.has(item.project_dir),
  );
  const selectedSessionRows = sessionResults.filter((item) => selectedSessions.has(item.id));
  const pendingDeleteMessageCount =
    pendingDelete?.reduce(
      (total, item) => total + (item.message_count ?? item.messages ?? 0),
      0,
    ) || 0;

  const toggleWorkspace = (projectDir: string) => {
    setSelectedWorkspaces((current) => {
      const next = new Set(current);
      if (next.has(projectDir)) next.delete(projectDir);
      else next.add(projectDir);
      return next;
    });
  };

  const toggleSession = (session: Session) => {
    if (sessionStatusGroup(session) === "running") return;
    setSelectedSessions((current) => {
      const next = new Set(current);
      if (next.has(session.id)) next.delete(session.id);
      else next.add(session.id);
      return next;
    });
  };

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
          onClick={() => {
            void actions.refreshCatalog();
            void actions.refreshSessions();
          }}
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
          <span>
            工作区 · {filteredCatalog.length}
            {hiddenWorkspaces.length ? ` · 已隐藏 ${hiddenWorkspaces.length}` : ""}
          </span>
          <button
            type="button"
            className={`section-manage${workspaceManage ? " active" : ""}`}
            aria-label="管理工作区"
            aria-pressed={workspaceManage}
            onClick={() => {
              setWorkspaceManage((current) => !current);
              setSelectedWorkspaces(new Set());
            }}
          >
            {workspaceManage ? "取消" : "管理"}
          </button>
        </div>
        <div className="list" role="list">
          {filteredCatalog.length === 0 && <div className="empty">还没有打开过目录</div>}
          {filteredCatalog.map((item) => {
            const current = workspace?.project_dir === item.project_dir;
            if (workspaceManage) {
              return (
                <label
                  key={item.project_dir}
                  className={`item selectable-item${current ? " active protected" : ""}`}
                  title={current ? "当前工作区不能从侧栏移除" : "选择工作区"}
                >
                  <input
                    type="checkbox"
                    checked={selectedWorkspaces.has(item.project_dir)}
                    disabled={current}
                    aria-label={`选择工作区：${item.label}`}
                    onChange={() => toggleWorkspace(item.project_dir)}
                  />
                  <IconFolder size={15} className="item-leading" />
                  <span className="item-copy">
                    <span className="title">{item.label}</span>
                    <span className="meta">
                      <span>{current ? "当前工作区 · 不可移除" : item.root || item.project_dir}</span>
                    </span>
                  </span>
                </label>
              );
            }
            return (
              <button
                key={item.project_dir}
                type="button"
                className={`item ${current ? "active" : ""}`}
                aria-current={current ? "page" : undefined}
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
            );
          })}
        </div>
        {workspaceManage && (
          <div className="selection-bar">
            <span>已选 {selectedWorkspaceRows.length}</span>
            <span className="selection-actions">
              {hiddenWorkspaces.length > 0 && (
                <button
                  type="button"
                  className="section-manage"
                  onClick={() =>
                    void actions.unhideWorkspaces(
                      hiddenWorkspaces.map((item) => item.project_dir),
                    )
                  }
                >
                  恢复已隐藏
                </button>
              )}
              <button
                type="button"
                className="danger-link"
                disabled={!selectedWorkspaceRows.length}
                onClick={() => setPendingHide(selectedWorkspaceRows)}
              >
                从侧栏移除
              </button>
            </span>
          </div>
        )}
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
          <span>会话 · {sessionResults.length}</span>
          <button
            type="button"
            className={`section-manage${sessionManage ? " active" : ""}`}
            aria-label="管理会话"
            aria-pressed={sessionManage}
            disabled={sessionScope === "all" || !workspace?.writable}
            title={sessionScope === "all" ? "批量删除仅支持当前工作区" : "批量管理会话"}
            onClick={() => {
              setSessionManage((current) => !current);
              setSelectedSessions(new Set());
            }}
          >
            {sessionManage ? "取消" : "管理"}
          </button>
        </div>
        <div className="session-filters">
          <select
            className="select"
            aria-label="会话范围"
            value={sessionScope}
            onChange={(event) => void actions.setSessionScope(event.target.value as SessionScope)}
          >
            <option value="workspace">当前工作区</option>
            <option value="all">全部工作区</option>
          </select>
          <select
            className="select"
            aria-label="会话排序"
            value={sessionSort}
            onChange={(event) => void actions.setSessionSort(event.target.value as SessionSort)}
          >
            {SESSION_SORTS.map((item) => (
              <option key={item.value} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <select
            className="select"
            aria-label="会话状态"
            value={sessionStatusFilter}
            onChange={(event) =>
              void actions.setSessionStatusFilter(event.target.value as SessionStatusGroup | "")
            }
          >
            {SESSION_STATUSES.map((item) => (
              <option key={item.value || "all"} value={item.value}>
                {item.label}
              </option>
            ))}
          </select>
          <select
            id="channel-filter"
            className="select"
            aria-label="按渠道筛选会话"
            value={channelFilter}
            onChange={(event) => actions.setChannelFilter(event.target.value)}
          >
            {CHANNELS.map((channel) => (
              <option key={channel || "all"} value={channel}>
                {channel || "全部渠道"}
              </option>
            ))}
          </select>
        </div>
        <div className="list">
          {!workspace && sessionScope === "workspace" && (
            <div className="empty">选择一个工作区开始</div>
          )}
          {sessionListLoading && sessionResults.length === 0 && (
            <div className="empty">正在加载会话…</div>
          )}
          {!sessionListLoading && (workspace || sessionScope === "all") && sessionResults.length === 0 && (
            <div className="empty">没有符合条件的会话</div>
          )}
          {sessionListError && <div className="session-list-warning">{sessionListError}</div>}
          {sessionResults.map((session) => {
            const title = displayTitle(session);
            const busy = sessionStatusGroup(session) === "running";
            if (sessionManage) {
              return (
                <label
                  key={session.id}
                  className={`item session-item selectable-item${
                    sessionId === session.id ? " active" : ""
                  }${busy ? " protected" : ""}`}
                  title={busy ? "执行中的会话不能删除" : "选择会话"}
                >
                  <input
                    type="checkbox"
                    checked={selectedSessions.has(session.id)}
                    disabled={busy}
                    aria-label={`选择会话：${title}`}
                    onChange={() => toggleSession(session)}
                  />
                  <SessionCopy session={session} global={false} />
                </label>
              );
            }
            return (
              <div
                key={`${session.project_dir || workspace?.project_dir || ""}:${session.id}`}
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
                    void actions.openSessionResult(session);
                    onNavigate?.();
                  }}
                >
                  <SessionCopy session={session} global={sessionScope === "all"} />
                </button>
                {sessionScope === "workspace" && (
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
                )}
                {sessionScope === "workspace" && menuId === session.id && (
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
                        setPendingDelete([session]);
                      }}
                    >
                      删除
                    </button>
                  </div>
                )}
              </div>
            );
          })}
          {sessionNextCursor && (
            <button
              type="button"
              className="load-more"
              disabled={sessionListLoading}
              onClick={() => void actions.loadMoreSessionResults()}
            >
              {sessionListLoading ? "正在加载…" : "加载更多"}
            </button>
          )}
        </div>
        {sessionManage && (
          <div className="selection-bar">
            <span>已选 {selectedSessionRows.length}</span>
            <button
              type="button"
              className="danger-link"
              disabled={!selectedSessionRows.length}
              onClick={() => setPendingDelete(selectedSessionRows)}
            >
              删除会话
            </button>
          </div>
        )}
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
                  {pendingDelete.length === 1
                    ? displayTitle(pendingDelete[0])
                    : `${pendingDelete.length} 个会话`}
                  {pendingDeleteMessageCount ? ` · ${pendingDeleteMessageCount} 条消息` : ""}
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
                将删除所选会话及其关联的 Task。产物文件和长期科研数据会保留；若任一会话仍在运行，整批删除都会被拒绝。
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
                  const ids = pendingDelete.map((item) => item.id);
                  setPendingDelete(null);
                  setSelectedSessions(new Set());
                  setSessionManage(false);
                  void actions.deleteSessions(ids);
                  onNavigate?.();
                }}
              >
                删除
              </button>
            </footer>
          </div>
        </div>
      )}
      {pendingHide && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setPendingHide(null)}>
          <div
            className="modal compact"
            role="dialog"
            aria-modal="true"
            aria-labelledby="workspace-hide-title"
            tabIndex={-1}
            onMouseDown={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                setPendingHide(null);
              }
              trapFocus(event);
            }}
          >
            <header>
              <div>
                <h2 id="workspace-hide-title">从侧栏移除工作区</h2>
                <p className="muted">已选择 {pendingHide.length} 个工作区</p>
              </div>
              <button
                type="button"
                className="icon-btn square"
                aria-label="取消移除"
                onClick={() => setPendingHide(null)}
              >
                <IconClose size={17} />
              </button>
            </header>
            <div className="session-delete-body">
              <p>
                仅从 Web 侧栏隐藏这些工作区，不会删除源目录、会话、Task、产物或科研数据。再次打开目录即可恢复。
              </p>
            </div>
            <footer>
              <button type="button" className="btn ghost" onClick={() => setPendingHide(null)}>
                取消
              </button>
              <button
                type="button"
                className="btn danger"
                onClick={() => {
                  const ids = pendingHide.map((item) => item.project_dir);
                  setPendingHide(null);
                  setSelectedWorkspaces(new Set());
                  setWorkspaceManage(false);
                  void actions.hideWorkspaces(ids);
                }}
              >
                从侧栏移除
              </button>
            </footer>
          </div>
        </div>
      )}
    </aside>
  );
}
