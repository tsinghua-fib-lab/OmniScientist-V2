import { useSyncExternalStore } from "react";
import { ApiError, api, watchTask } from "./api";
import { bindAttachments } from "./attachments";
import { readNav, sameNav, writeNav } from "./nav";
import {
  applyActivity,
  applyPartial,
  applyToken,
  bucketKey,
  emptyDraft,
  emptyTurn,
  finishTurn,
  isActiveTask,
  latestFollowable,
  sessionBusy,
} from "./turnState";
import { dropLocalMessage, mergeTranscript } from "./transcript";
import type {
  ActivityItem,
  Artifact,
  CatalogWorkspace,
  ChatMessage,
  DirectoryListing,
  DraftState,
  Drawer,
  Mode,
  Session,
  SessionFingerprint,
  SessionTimeline,
  TaskSummary,
  TimelineTurn,
  TurnState,
  WorkerState,
  Workspace,
} from "./types";

export type Snapshot = {
  workspace: Workspace | null;
  catalog: CatalogWorkspace[];
  sessions: Session[];
  sessionId: string | null;
  sessionOpenRevision: number;
  channelFilter: string;
  messages: ChatMessage[];
  streamingText: string;
  activities: ActivityItem[];
  streaming: boolean;
  currentTurn: TurnState | null;
  composer: string;
  mode: Mode;
  pickerOpen: boolean;
  picker: DirectoryListing | null;
  showHidden: boolean;
  drawer: Drawer;
  tasks: TaskSummary[];
  sessionTasks: TaskSummary[];
  sessionTurns: TimelineTurn[];
  taskSelectedId: string;
  taskDetail: Record<string, unknown> | null;
  taskDetailLoading: boolean;
  taskDetailError: string;
  artifacts: Artifact[];
  artifactTaskId: string;
  artifactListLoading: boolean;
  artifactListError: string;
  artifactDetail: Artifact | null;
  artifactLoading: boolean;
  artifactError: string;
  rom: Record<string, unknown> | null;
  notebook: string;
  cost: Record<string, unknown> | null;
  attachments: { name: string; uri: string }[];
  error: string;
  notice: string;
};

const listeners = new Set<() => void>();
const watchers = new Map<string, AbortController>();
const drafts: Record<string, DraftState> = {};
const turns: Record<string, TurnState> = {};
const messagesBySession: Record<string, ChatMessage[]> = {};
const transcriptFingerprints: Record<string, SessionFingerprint> = {};
const requestGens = new Map<string, number>();
const watchEpochs = new Map<string, number>();
let taskDrawerRequestId = 0;
let taskDetailRequestId = 0;
let artifactListRequestId = 0;
let artifactDetailRequestId = 0;
let workspaceOpenRequestId = 0;
let navigationHydrated = false;
let applyingLocation = false;
let persistedNavSignature = "";
let syncInFlight: { key: string; promise: Promise<void> } | null = null;
let taskDetailSyncInFlight: { key: string; promise: Promise<void> } | null = null;
let artifactSyncInFlight: { key: string; promise: Promise<void> } | null = null;

let core = {
  workspace: null as Workspace | null,
  catalog: [] as CatalogWorkspace[],
  sessions: [] as Session[],
  sessionId: null as string | null,
  sessionOpenRevision: 0,
  channelFilter: "",
  pickerOpen: false,
  picker: null as DirectoryListing | null,
  showHidden: false,
  drawer: "none" as Drawer,
  tasks: [] as TaskSummary[],
  sessionTasks: [] as TaskSummary[],
  sessionTurns: [] as TimelineTurn[],
  taskSelectedId: "",
  taskDetail: null as Record<string, unknown> | null,
  taskDetailLoading: false,
  taskDetailError: "",
  artifacts: [] as Artifact[],
  artifactTaskId: "",
  artifactListLoading: false,
  artifactListError: "",
  artifactDetail: null as Artifact | null,
  artifactLoading: false,
  artifactError: "",
  rom: null as Record<string, unknown> | null,
  notebook: "",
  cost: null as Record<string, unknown> | null,
  error: "",
  notice: "",
  defaultMode: "auto" as Mode,
};

function workspaceKey(workspace: Workspace | null = core.workspace): string {
  if (!workspace) return "";
  return workspace.project_dir || workspace.open_path;
}

function keyOf(sessionId: string): string {
  return bucketKey(workspaceKey(), sessionId);
}

function draftOf(sessionId: string | null): DraftState {
  return drafts[keyOf(sessionId || "pending")] ?? emptyDraft(core.defaultMode);
}

function turnOf(sessionId: string | null): TurnState | undefined {
  if (!sessionId) return undefined;
  return turns[keyOf(sessionId)];
}

function project(): Snapshot {
  const sessionId = core.sessionId;
  const draft = draftOf(sessionId);
  const turn = turnOf(sessionId);
  return {
    workspace: core.workspace,
    catalog: core.catalog,
    sessions: core.sessions,
    sessionId,
    sessionOpenRevision: core.sessionOpenRevision,
    channelFilter: core.channelFilter,
    messages: sessionId ? messagesBySession[keyOf(sessionId)] || [] : [],
    streamingText: turn?.partialText || "",
    activities: turn?.activities || [],
    streaming: sessionBusy(turn),
    currentTurn: turn ?? null,
    composer: draft.composer,
    mode: draft.mode,
    pickerOpen: core.pickerOpen,
    picker: core.picker,
    showHidden: core.showHidden,
    drawer: core.drawer,
    tasks: core.tasks,
    sessionTasks: core.sessionTasks,
    sessionTurns: core.sessionTurns,
    taskSelectedId: core.taskSelectedId,
    taskDetail: core.taskDetail,
    taskDetailLoading: core.taskDetailLoading,
    taskDetailError: core.taskDetailError,
    artifacts: core.artifacts,
    artifactTaskId: core.artifactTaskId,
    artifactListLoading: core.artifactListLoading,
    artifactListError: core.artifactListError,
    artifactDetail: core.artifactDetail,
    artifactLoading: core.artifactLoading,
    artifactError: core.artifactError,
    rom: core.rom,
    notebook: core.notebook,
    cost: core.cost,
    attachments: draft.attachments,
    error: core.error,
    notice: core.notice,
  };
}

let state = project();

function emit(patch: Partial<typeof core> = {}) {
  core = { ...core, ...patch };
  if (navigationHydrated) persistNav();
  state = project();
  listeners.forEach((fn) => fn());
}

function persistNav() {
  if (!core.workspace) return;
  const signature = [
    core.workspace.kind,
    core.workspace.project_dir,
    core.workspace.project_name,
    core.workspace.open_path,
    core.sessionId || "",
  ].join("\u0000");
  if (signature === persistedNavSignature) return;
  const push = navigationHydrated && !applyingLocation && Boolean(persistedNavSignature);
  writeNav(core.workspace, core.sessionId, { push });
  persistedNavSignature = signature;
}

function nextGen(scope: string): number {
  const value = (requestGens.get(scope) || 0) + 1;
  requestGens.set(scope, value);
  return value;
}

function isGen(scope: string, value: number): boolean {
  return requestGens.get(scope) === value;
}

function messageScope(workspace: string, sessionId: string): string {
  return `msg:${workspace}::${sessionId}`;
}

function syncScope(workspace: string, sessionId: string | null): string {
  return `sync:${workspace}::${sessionId || ""}::${core.channelFilter}`;
}

function taskScope(workspace: string, sessionId: string): string {
  return `tasks:${workspace}::${sessionId}`;
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function getSnapshot() {
  return state;
}

export function useAppState(): Snapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

function ws(): string {
  const w = core.workspace;
  if (!w) throw new Error("no workspace");
  return w.open_path || w.project_dir;
}

function putDraft(sessionId: string, patch: Partial<DraftState>) {
  const key = keyOf(sessionId);
  drafts[key] = { ...draftOf(sessionId), ...patch };
  emit();
}

function putTurn(sessionId: string, turn: TurnState) {
  turns[bucketKey(turn.workspaceKey, sessionId)] = turn;
  emit();
}

function patchTurn(
  owner: { workspaceKey: string; sessionId: string; clientRunId: string },
  update: (turn: TurnState) => TurnState | null,
) {
  const key = bucketKey(owner.workspaceKey, owner.sessionId);
  const current = turns[key] ?? emptyTurn(owner);
  const next = update(current);
  if (!next) return;
  turns[key] = next;
  emit();
}

function abortWatch(key: string) {
  watchEpochs.set(key, (watchEpochs.get(key) || 0) + 1);
  const prior = watchers.get(key);
  if (prior) {
    prior.abort();
    watchers.delete(key);
  }
}

function abortForeignWatches(keepKey: string) {
  for (const key of [...watchers.keys()]) {
    if (key !== keepKey) abortWatch(key);
  }
}

function beginWatch(key: string): { controller: AbortController; epoch: number } {
  abortWatch(key);
  const epoch = (watchEpochs.get(key) || 0) + 1;
  watchEpochs.set(key, epoch);
  const controller = new AbortController();
  watchers.set(key, controller);
  return { controller, epoch };
}

function isWatchCurrent(key: string, epoch: number, controller: AbortController): boolean {
  return watchEpochs.get(key) === epoch && watchers.get(key) === controller;
}

function abortWorkspace(workspace: string) {
  for (const key of [...watchers.keys()]) {
    if (key.startsWith(`${workspace}::`)) abortWatch(key);
  }
}

function upsertSession(sessions: Session[], selected: Session | null | undefined): Session[] {
  if (!selected) return sessions;
  const index = sessions.findIndex((row) => row.id === selected.id);
  if (index < 0) return [selected, ...sessions];
  const next = [...sessions];
  next[index] = {
    ...sessions[index],
    ...selected,
    messages: selected.messages ?? sessions[index].messages,
  };
  return next;
}

function fingerprintOf(
  value: (Partial<SessionFingerprint> & Partial<Session>) | null | undefined,
): SessionFingerprint {
  const row = value || {};
  const messageCount =
    typeof row.message_count === "number"
      ? row.message_count
      : typeof row.messages === "number"
        ? row.messages
        : undefined;
  return {
    session_id: row.session_id || row.id || "",
    message_count: messageCount,
    last_message_id: row.last_message_id || "",
    latest_task_id: row.latest_task_id || "",
    latest_task_status: row.latest_task_status || "",
    latest_event_seq: row.latest_event_seq || 0,
    updated_at: row.updated_at,
    last_activity_at: row.last_activity_at,
  };
}

function transcriptChanged(key: string, next: SessionFingerprint): boolean {
  const previous = transcriptFingerprints[key];
  if (!previous) return true;
  return (
    previous.message_count !== next.message_count ||
    previous.last_message_id !== next.last_message_id ||
    previous.latest_task_id !== next.latest_task_id ||
    previous.latest_task_status !== next.latest_task_status ||
    previous.latest_event_seq !== next.latest_event_seq
  );
}

function observeTranscript(key: string, next: SessionFingerprint): void {
  transcriptFingerprints[key] = next;
}

function asTaskSummary(turn: TimelineTurn): TaskSummary {
  const task = { ...turn };
  delete task.executions;
  return task;
}

function applyTimeline(
  workspace: string,
  sessionId: string,
  data: Pick<SessionTimeline, "messages" | "turns"> & {
    session?: Session | null;
    fingerprint?: SessionFingerprint;
  },
): void {
  const key = bucketKey(workspace, sessionId);
  const incoming = (data.messages || []) as ChatMessage[];
  messagesBySession[key] = mergeTranscript(
    messagesBySession[key] || [],
    incoming,
    sessionBusy(turns[key]),
  );
  const fingerprint =
    data.fingerprint ||
    fingerprintOf({
      ...data.session,
      message_count: incoming.length,
      last_message_id: incoming.at(-1)?.id || "",
    });
  observeTranscript(key, fingerprint);
  const sessionTurns = (data.turns || []) as TimelineTurn[];
  settleLocalTurn(workspace, sessionId, sessionTurns, data.session);
  const viewing = workspaceKey() === workspace && core.sessionId === sessionId;
  emit({
    ...(viewing
      ? { sessionTurns, sessionTasks: sessionTurns.map(asTaskSummary) }
      : {}),
    sessions: upsertSession(core.sessions, data.session || null),
  });
}

function settleLocalTurn(
  workspace: string,
  sessionId: string,
  sessionTurns: TimelineTurn[],
  session?: Session | null,
): boolean {
  const key = bucketKey(workspace, sessionId);
  const current = turns[key];
  if (!current || current.status !== "running" || !current.taskId) return false;
  const follow = latestFollowable(sessionTurns, session);
  if (follow?.id && follow.id === current.taskId) return false;
  if (follow?.id) return false;
  turns[key] = finishTurn(current);
  return true;
}

function ensureTaskWatch(
  sessionId: string,
  taskId: string,
  status: string,
  workspace = workspaceKey(),
): void {
  if (!isActiveTask(status)) return;
  const key = bucketKey(workspace, sessionId);
  if (watchers.has(key) && turns[key]?.taskId === taskId) return;
  void actions.watchExisting(sessionId, taskId, "", workspace);
}

function resetWorkspaceState(workspace: Workspace, notice = ""): void {
  const previous = workspaceKey();
  if (previous) abortWorkspace(previous);
  if (previous) nextGen(`ws:${previous}`);
  taskDrawerRequestId += 1;
  taskDetailRequestId += 1;
  artifactListRequestId += 1;
  artifactDetailRequestId += 1;
  taskDetailSyncInFlight = null;
  artifactSyncInFlight = null;
  emit({
    workspace,
    pickerOpen: false,
    sessionId: null,
    sessions: [],
    tasks: [],
    sessionTasks: [],
    sessionTurns: [],
    taskSelectedId: "",
    taskDetail: null,
    taskDetailLoading: false,
    taskDetailError: "",
    artifacts: [],
    artifactTaskId: "",
    artifactListLoading: false,
    artifactListError: "",
    artifactDetail: null,
    artifactLoading: false,
    artifactError: "",
    rom: null,
    notebook: "",
    cost: null,
    error: "",
    notice,
  });
}

function bumpInspectorRequests() {
  taskDrawerRequestId += 1;
  taskDetailRequestId += 1;
  artifactListRequestId += 1;
  artifactDetailRequestId += 1;
}

function inspectorScopeReset(): Partial<typeof core> {
  return {
    tasks: [],
    taskSelectedId: "",
    taskDetail: null,
    taskDetailLoading: false,
    taskDetailError: "",
    artifacts: [],
    artifactTaskId: "",
    artifactListLoading: false,
    artifactListError: "",
    artifactDetail: null,
    artifactLoading: false,
    artifactError: "",
    rom: null,
    notebook: "",
    cost: null,
  };
}

function refreshOpenInspector() {
  if (core.drawer === "none") return;
  void actions.openDrawer(core.drawer);
}

async function loadArtifactScope(taskId: string, { silent = false } = {}) {
  if (!core.workspace) return;
  const request = {
    id: ++artifactListRequestId,
    workspace: workspaceKey(),
    sessionId: core.sessionId,
    taskId,
  };
  if (!silent) {
    artifactDetailRequestId += 1;
    emit({
      drawer: "artifact",
      artifactTaskId: taskId,
      artifacts: [],
      artifactListLoading: true,
      artifactListError: "",
      artifactDetail: null,
      artifactLoading: false,
      artifactError: "",
      error: "",
    });
  }
  try {
    const data = await api.listArtifacts(ws(), core.sessionId || "", taskId, 200);
    if (
      request.id !== artifactListRequestId ||
      request.workspace !== workspaceKey() ||
      request.sessionId !== core.sessionId ||
      request.taskId !== core.artifactTaskId ||
      core.drawer !== "artifact"
    ) {
      return;
    }
    emit({
      artifacts: data.artifacts || [],
      artifactListLoading: false,
      artifactListError: "",
    });
  } catch (err) {
    if (
      request.id !== artifactListRequestId ||
      request.workspace !== workspaceKey() ||
      request.sessionId !== core.sessionId ||
      request.taskId !== core.artifactTaskId ||
      core.drawer !== "artifact"
    ) {
      return;
    }
    if (!silent) {
      emit({
        artifactListLoading: false,
        artifactListError: err instanceof Error ? err.message : String(err),
      });
    }
  }
}

async function refreshArtifactScopeSilently(taskId: string): Promise<void> {
  const key = [workspaceKey(), core.sessionId || "", taskId].join("\u0000");
  if (artifactSyncInFlight?.key === key) return artifactSyncInFlight.promise;
  const promise = loadArtifactScope(taskId, { silent: true });
  artifactSyncInFlight = { key, promise };
  try {
    await promise;
  } finally {
    if (artifactSyncInFlight?.promise === promise) artifactSyncInFlight = null;
  }
}

async function refreshVisibleTaskDetail(
  taskId: string,
  rpcWorkspace: string,
  ownerWorkspace: string,
): Promise<void> {
  const key = `${ownerWorkspace}\u0000${taskId}`;
  if (taskDetailSyncInFlight?.key === key) return taskDetailSyncInFlight.promise;
  const promise = (async () => {
    const requestId = ++taskDetailRequestId;
    try {
      const detail = await api.getTask(rpcWorkspace, taskId);
      if (
        requestId !== taskDetailRequestId ||
        workspaceKey() !== ownerWorkspace ||
        core.drawer !== "task" ||
        core.taskSelectedId !== taskId
      ) {
        return;
      }
      emit({
        taskDetail: detail as Record<string, unknown>,
        taskDetailLoading: false,
        taskDetailError: "",
      });
    } catch {
      // Inspector synchronization is auxiliary. Keep the last complete detail
      // and retry on the next visible-page tick.
    }
  })();
  taskDetailSyncInFlight = { key, promise };
  try {
    await promise;
  } finally {
    if (taskDetailSyncInFlight?.promise === promise) taskDetailSyncInFlight = null;
  }
}

export const actions = {
  setComposer(text: string) {
    putDraft(core.sessionId || "pending", { composer: text });
  },
  setMode(mode: Mode) {
    core.defaultMode = mode;
    putDraft(core.sessionId || "pending", { mode });
  },
  setChannelFilter(channelFilter: string) {
    emit({ channelFilter });
    void actions.refreshSessions();
  },
  async refreshCatalog() {
    try {
      const data = await api.listWorkspaces();
      emit({
        catalog: (data.workspaces || []) as CatalogWorkspace[],
        error: "",
      });
    } catch (err) {
      emit({ error: err instanceof Error ? err.message : String(err) });
    }
  },
  async openPicker(path?: string) {
    try {
      const picker = await api.listDirectory(path, core.showHidden);
      emit({ pickerOpen: true, picker, error: "" });
    } catch (err) {
      emit({ error: err instanceof Error ? err.message : String(err) });
    }
  },
  closePicker() {
    emit({ pickerOpen: false });
  },
  async setShowHidden(showHidden: boolean) {
    emit({ showHidden });
    if (core.picker) {
      await actions.openPicker(core.picker.path);
    }
  },
  async openWorkspace(path: string, restoreSessionId?: string | null) {
    navigationHydrated = true;
    const requestId = ++workspaceOpenRequestId;
    try {
      const data = await api.openWorkspace(path);
      if (requestId !== workspaceOpenRequestId) return;
      resetWorkspaceState(
        data.workspace,
        data.workspace.writable ? "" : "此目录尚未信任，网页只读。请先在 CLI 运行 omni trust。",
      );
      await actions.refreshCatalog();
      if (requestId !== workspaceOpenRequestId) return;
      await actions.refreshSessions();
      if (requestId !== workspaceOpenRequestId) return;
      if (restoreSessionId) await actions.openSession(restoreSessionId);
      else refreshOpenInspector();
    } catch (err) {
      if (requestId !== workspaceOpenRequestId) return;
      emit({ error: err instanceof Error ? err.message : String(err) });
    }
  },
  async selectCatalog(item: CatalogWorkspace) {
    navigationHydrated = true;
    if (core.workspace && core.workspace.project_dir === item.project_dir) {
      if (core.sessionId) actions.closeSession();
      return;
    }
    const requestId = ++workspaceOpenRequestId;
    try {
      const data = await api.selectWorkspace(
        item.root ? { path: item.root } : { project_dir: item.project_dir, name: item.name },
      );
      if (requestId !== workspaceOpenRequestId) return;
      resetWorkspaceState(data.workspace);
      await actions.refreshSessions();
      if (requestId !== workspaceOpenRequestId) return;
      refreshOpenInspector();
    } catch (err) {
      if (requestId !== workspaceOpenRequestId) return;
      emit({ error: err instanceof Error ? err.message : String(err) });
    }
  },
  async refreshSessions() {
    if (!core.workspace) {
      emit({ sessions: [] });
      return;
    }
    const ownerWorkspace = workspaceKey();
    const channelFilter = core.channelFilter;
    const gen = nextGen(`list:${ownerWorkspace}::${channelFilter}`);
    const data = await api.workspaceInbox(ws(), channelFilter, core.sessionId || "");
    if (
      !isGen(`list:${ownerWorkspace}::${channelFilter}`, gen) ||
      ownerWorkspace !== workspaceKey() ||
      channelFilter !== core.channelFilter
    ) {
      return;
    }
    emit({ sessions: upsertSession(data.sessions || [], data.focus) });
  },
  async openSession(sessionId: string, { signalOpen = true }: { signalOpen?: boolean } = {}) {
    if (!core.workspace) return;
    const ownerWorkspace = workspaceKey();
    const key = bucketKey(ownerWorkspace, sessionId);
    abortForeignWatches(key);
    const gen = nextGen(messageScope(ownerWorkspace, sessionId));
    bumpInspectorRequests();
    emit({
      sessionId,
      sessionOpenRevision: signalOpen
        ? core.sessionOpenRevision + 1
        : core.sessionOpenRevision,
      sessionTasks: [],
      sessionTurns: [],
      ...inspectorScopeReset(),
      error: "",
    });
    try {
      const data = await api.sessionTimeline(ws(), sessionId);
      if (
        !isGen(messageScope(ownerWorkspace, sessionId), gen) ||
        workspaceKey() !== ownerWorkspace ||
        core.sessionId !== sessionId
      ) {
        return;
      }
      applyTimeline(ownerWorkspace, sessionId, data);
      const follow = latestFollowable(data.turns || [], data.session);
      if (follow?.id) ensureTaskWatch(sessionId, follow.id, follow.status || "", ownerWorkspace);
      refreshOpenInspector();
    } catch (err) {
      if (
        isGen(messageScope(ownerWorkspace, sessionId), gen) &&
        workspaceKey() === ownerWorkspace &&
        core.sessionId === sessionId
      ) {
        emit({ error: err instanceof Error ? err.message : String(err) });
      }
    }
  },
  async newSession() {
    if (!core.workspace) return;
    const ownerWorkspace = workspaceKey();
    const gen = nextGen(`open:${ownerWorkspace}`);
    const data = await api.createSession(ws());
    if (!isGen(`open:${ownerWorkspace}`, gen) || workspaceKey() !== ownerWorkspace) {
      await actions.refreshSessions();
      return;
    }
    const session = data.session;
    abortForeignWatches(bucketKey(ownerWorkspace, session.id));
    const sessionKey = keyOf(session.id);
    messagesBySession[sessionKey] = [];
    observeTranscript(sessionKey, {
      session_id: session.id,
      message_count: 0,
      last_message_id: "",
    });
    drafts[keyOf(session.id)] = drafts[keyOf("pending")] ?? emptyDraft(core.defaultMode);
    delete drafts[keyOf("pending")];
    bumpInspectorRequests();
    emit({
      sessionId: session.id,
      sessionTasks: [],
      sessionTurns: [],
      ...inspectorScopeReset(),
    });
    refreshOpenInspector();
    await actions.refreshSessions();
  },
  closeSession() {
    if (!core.sessionId) return;
    abortWatch(keyOf(core.sessionId));
    bumpInspectorRequests();
    emit({
      sessionId: null,
      sessionTurns: [],
      sessionTasks: [],
      ...inspectorScopeReset(),
    });
    refreshOpenInspector();
  },
  async renameSession(sessionId: string, title: string) {
    if (!core.workspace) return;
    await api.renameSession(ws(), sessionId, title);
    await actions.refreshSessions();
  },
  async deleteSession(sessionId: string) {
    if (!core.workspace) return;
    try {
      await api.deleteSession(ws(), sessionId);
      abortWatch(keyOf(sessionId));
      delete messagesBySession[keyOf(sessionId)];
      delete transcriptFingerprints[keyOf(sessionId)];
      delete drafts[keyOf(sessionId)];
      delete turns[keyOf(sessionId)];
      const closingCurrent = core.sessionId === sessionId;
      if (closingCurrent) bumpInspectorRequests();
      emit({
        sessionId: closingCurrent ? null : core.sessionId,
        sessionTasks: closingCurrent ? [] : core.sessionTasks,
        sessionTurns: closingCurrent ? [] : core.sessionTurns,
        ...(closingCurrent ? inspectorScopeReset() : {}),
        error: "",
      });
      await actions.refreshSessions();
      if (closingCurrent) refreshOpenInspector();
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "";
      const fallback =
        code === "busy"
          ? "会话仍有正在运行的任务，请先取消或等它结束"
          : code === "untrusted"
            ? "此工作区只读，无法删除会话"
            : err instanceof Error
              ? err.message
              : String(err);
      emit({ error: fallback });
    }
  },
  async restoreNav() {
    if (navigationHydrated) return;
    const nav = readNav();
    if (!nav?.workspace) {
      navigationHydrated = true;
      persistNav();
      return;
    }
    const requestId = ++workspaceOpenRequestId;
    try {
      const navWorkspace = nav.workspace;
      const legacyNamed =
        navWorkspace.kind === "path"
          ? core.catalog.find(
              (item) => !item.root && item.project_dir === navWorkspace.path,
            )
          : null;
      const data =
        navWorkspace.kind === "named"
          ? await api.selectWorkspace({
              ...(navWorkspace.projectDir ? { project_dir: navWorkspace.projectDir } : {}),
              name: navWorkspace.name,
            })
          : legacyNamed
            ? await api.selectWorkspace({
                project_dir: legacyNamed.project_dir,
                name: legacyNamed.name,
              })
          : await api.openWorkspace(navWorkspace.path);
      if (requestId !== workspaceOpenRequestId) return;
      resetWorkspaceState(
        data.workspace,
        data.workspace.writable ? "" : "此目录尚未信任，网页只读。请先在 CLI 运行 omni trust。",
      );
      await actions.refreshSessions();
      if (requestId !== workspaceOpenRequestId) return;
      if (nav.sessionId) await actions.openSession(nav.sessionId);
    } catch (err) {
      if (requestId !== workspaceOpenRequestId) return;
      emit({ error: err instanceof Error ? err.message : String(err) });
    } finally {
      if (requestId === workspaceOpenRequestId) {
        navigationHydrated = true;
        persistNav();
      }
    }
  },
  async restoreFromHash() {
    if (!navigationHydrated) return;
    const nav = readNav();
    if (!nav?.workspace) return;
    if (
      core.workspace &&
      sameNav(
        {
          workspace:
            core.workspace.kind === "named"
              ? { kind: "named", projectDir: core.workspace.project_dir, name: core.workspace.project_name }
              : { kind: "path", path: core.workspace.open_path || core.workspace.project_dir },
          sessionId: core.sessionId,
        },
        nav,
      )
    ) {
      return;
    }
    applyingLocation = true;
    try {
      if (nav.workspace.kind === "named") {
        const sameNamed =
          core.workspace?.kind === "named" && core.workspace.project_name === nav.workspace.name;
        if (!sameNamed) {
          await actions.selectCatalog({
            name: nav.workspace.name,
            root: null,
            project_dir: nav.workspace.projectDir || "",
            kind: "named",
            label: nav.workspace.name,
            last_seen: 0,
          });
        }
      } else if (
        !core.workspace ||
        (core.workspace.open_path || core.workspace.project_dir) !== nav.workspace.path
      ) {
        await actions.openWorkspace(nav.workspace.path);
      }
      if (nav.sessionId && nav.sessionId !== core.sessionId) {
        await actions.openSession(nav.sessionId);
      } else if (!nav.sessionId && core.sessionId) {
        actions.closeSession();
      }
    } finally {
      applyingLocation = false;
    }
  },
  async syncWorkspace() {
    if (!core.workspace) return;
    const ownerWorkspace = workspaceKey();
    const ownerSessionId = core.sessionId;
    const inflightKey = syncScope(ownerWorkspace, ownerSessionId);
    if (syncInFlight?.key === inflightKey) return syncInFlight.promise;
    const rpcWorkspace = ws();
    const channelFilter = core.channelFilter;
    const gen = nextGen(inflightKey);
    const promise = (async () => {
      try {
        const inbox = await api
          .workspaceInbox(rpcWorkspace, channelFilter, ownerSessionId || "")
          .catch(() => null);
        if (
          !isGen(inflightKey, gen) ||
          ownerWorkspace !== workspaceKey() ||
          ownerSessionId !== core.sessionId ||
          channelFilter !== core.channelFilter
        ) {
          return;
        }
        if (inbox) {
          emit({ sessions: upsertSession(inbox.sessions || [], inbox.focus) });
        }
        if (!ownerSessionId) return;
        const focus = fingerprintOf(inbox?.focus);
        const key = bucketKey(ownerWorkspace, ownerSessionId);
        if (settleLocalTurn(ownerWorkspace, ownerSessionId, core.sessionTurns, inbox?.focus || null)) {
          emit();
        }
        const follow = latestFollowable(core.sessionTurns, inbox?.focus || null);
        if (follow?.id) ensureTaskWatch(ownerSessionId, follow.id, follow.status || "", ownerWorkspace);
        const focusedTaskId = core.taskSelectedId;
        if (core.drawer === "task" && focusedTaskId && isActiveTask(follow?.status)) {
          void refreshVisibleTaskDetail(focusedTaskId, rpcWorkspace, ownerWorkspace);
        }
        if (core.drawer === "artifact" && (follow || core.artifactTaskId)) {
          void refreshArtifactScopeSilently(core.artifactTaskId);
        }
        if (!transcriptChanged(key, focus)) {
          observeTranscript(key, { ...transcriptFingerprints[key], ...focus });
          return;
        }
        const messageGen = nextGen(messageScope(ownerWorkspace, ownerSessionId));
        const timeline = await api.sessionTimeline(rpcWorkspace, ownerSessionId).catch(() => null);
        if (
          !timeline ||
          !isGen(messageScope(ownerWorkspace, ownerSessionId), messageGen) ||
          !isGen(inflightKey, gen) ||
          ownerWorkspace !== workspaceKey() ||
          ownerSessionId !== core.sessionId
        ) {
          return;
        }
        applyTimeline(ownerWorkspace, ownerSessionId, timeline);
        const nextFollow = latestFollowable(timeline.turns || [], timeline.session);
        if (nextFollow?.id) {
          ensureTaskWatch(ownerSessionId, nextFollow.id, nextFollow.status || "", ownerWorkspace);
        }
        if (core.drawer === "task") {
          emit({
            tasks: (timeline.turns || []).map(asTaskSummary),
          });
        }
      } catch {
        // Cross-process synchronization is best-effort. Existing content stays
        // usable and the next visible-page tick retries without a warning loop.
      }
    })();
    syncInFlight = { key: inflightKey, promise };
    try {
      await promise;
    } finally {
      if (syncInFlight?.promise === promise) syncInFlight = null;
    }
  },
  async send() {
    if (!core.workspace) return;
    let sessionId = core.sessionId;
    if (!sessionId) {
      await actions.newSession();
      sessionId = core.sessionId;
    }
    if (!sessionId) return;
    const ownerWorkspace = workspaceKey();
    const draft = draftOf(sessionId);
    const text = draft.composer.trim();
    if (!text || sessionBusy(turnOf(sessionId))) return;
    const bound = bindAttachments(
      text,
      draft.attachments.map((a) => a.uri),
    );
    const clientRunId =
      typeof crypto !== "undefined" && crypto.randomUUID
        ? crypto.randomUUID().replace(/-/g, "")
        : `run-${Date.now()}`;
    const owner = { workspaceKey: ownerWorkspace, sessionId, clientRunId };
    const localId = `local-${clientRunId}`;
    const existingMessages = messagesBySession[keyOf(sessionId)] || [];
    messagesBySession[keyOf(sessionId)] = [
      ...existingMessages,
      {
        id: localId,
        role: "user",
        content: bound.text,
        created_at: new Date().toISOString(),
      },
    ];
    putDraft(sessionId, { composer: "", attachments: [] });
    putTurn(
      sessionId,
      emptyTurn({
        workspaceKey: ownerWorkspace,
        sessionId,
        clientRunId,
      }),
    );
    patchTurn(owner, (turn) => ({ ...turn, status: "running", worker: "live", error: "" }));
    emit({ error: "" });
    try {
      const started = await api.startTurn(ws(), {
        text: bound.text,
        session_id: sessionId,
        interaction_mode: draft.mode,
        file_uris: bound.fileUris,
        client_run_id: clientRunId,
      });
      if (started.kind === "command") {
        await actions.reloadMessages(owner.workspaceKey, started.session_id || sessionId);
        patchTurn(owner, (turn) => ({
          ...turn,
          status: "done",
          worker: "",
          partialText: "",
        }));
        await actions.refreshSessions();
        return;
      }
      const boundSession = started.session_id || sessionId;
      const boundTask = started.task_id;
      const boundRun = started.client_run_id || clientRunId;
      const streamOwner = {
        workspaceKey: ownerWorkspace,
        sessionId: boundSession,
        clientRunId: boundRun,
      };
      if (boundSession !== sessionId) {
        turns[bucketKey(ownerWorkspace, boundSession)] = {
          ...(turns[bucketKey(ownerWorkspace, sessionId)] || emptyTurn(streamOwner)),
          sessionId: boundSession,
          taskId: boundTask,
          clientRunId: boundRun,
        };
      } else {
        patchTurn(streamOwner, (turn) => ({ ...turn, taskId: boundTask, clientRunId: boundRun }));
      }
      if (
        workspaceKey() === ownerWorkspace &&
        core.sessionId === sessionId &&
        boundSession !== sessionId
      ) {
        emit({ sessionId: boundSession });
      }
      if (workspaceKey() === ownerWorkspace) {
        void actions.refreshSessionTasks(boundSession);
      }
      await actions.watchExisting(boundSession, boundTask, boundRun, ownerWorkspace);
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "";
      const message = err instanceof Error ? err.message : String(err);
      messagesBySession[keyOf(sessionId)] = dropLocalMessage(
        messagesBySession[keyOf(sessionId)] || [],
        localId,
      );
      patchTurn(owner, (turn) => ({
        ...turn,
        status: code === "capacity" || code === "busy" ? "queued" : "error",
        error: message,
      }));
      if (workspaceKey() === ownerWorkspace && core.sessionId === sessionId) {
        emit({ error: message });
      } else {
        emit();
      }
    }
  },
  async watchExisting(
    sessionId: string,
    taskId: string,
    clientRunId = "",
    workspace = workspaceKey(),
  ) {
    const key = bucketKey(workspace, sessionId);
    const { controller, epoch } = beginWatch(key);
    const previous = turns[key];
    const current =
      previous?.taskId === taskId
        ? previous
        : emptyTurn({ workspaceKey: workspace, sessionId, taskId, clientRunId });
    turns[key] = {
      ...current,
      taskId,
      clientRunId: clientRunId || current.clientRunId,
      status: "running",
      worker: current.worker || "quiet",
    };
    emit();
    const owner = {
      workspaceKey: workspace,
      sessionId,
      clientRunId: turns[key].clientRunId,
    };
    try {
      await watchTask(
        workspace,
        taskId,
        turns[key].lastEventSeq,
        {
          onToken(piece) {
            if (!isWatchCurrent(key, epoch, controller)) return;
            patchTurn(owner, (turn) => applyToken(turn, piece, owner));
          },
          onPartial(text) {
            if (!isWatchCurrent(key, epoch, controller)) return;
            patchTurn(owner, (turn) => applyPartial(turn, text, owner));
          },
          onActivity(item) {
            if (!isWatchCurrent(key, epoch, controller)) return;
            patchTurn(owner, (turn) => applyActivity(turn, item, owner));
          },
          onWorker(info) {
            if (!isWatchCurrent(key, epoch, controller)) return;
            const worker = String(info.state || "") as WorkerState;
            patchTurn(owner, (turn) => ({ ...turn, worker }));
          },
          onDone() {
            if (watchEpochs.get(key) !== epoch) return;
            patchTurn(owner, (turn) => ({ ...turn, status: "done", worker: "" }));
            void (async () => {
              if (watchEpochs.get(key) !== epoch) return;
              try {
                await actions.reloadMessages(workspace, sessionId);
                if (watchEpochs.get(key) === epoch) {
                  patchTurn(owner, (turn) => ({ ...turn, partialText: "" }));
                }
              } catch (err) {
                if (workspaceKey() === workspace && core.sessionId === sessionId) {
                  emit({ error: err instanceof Error ? err.message : String(err) });
                }
              }
              if (workspaceKey() !== workspace || watchEpochs.get(key) !== epoch) return;
              if (core.sessionId === sessionId) {
                await actions.refreshSessions().catch(() => undefined);
              }
              if (core.drawer === "task" && core.taskSelectedId === taskId) {
                void refreshVisibleTaskDetail(taskId, workspace, workspace);
              }
              if (core.drawer === "artifact" && core.sessionId === sessionId) {
                void refreshArtifactScopeSilently(core.artifactTaskId);
              }
            })();
          },
          onError(message) {
            if (!isWatchCurrent(key, epoch, controller)) return;
            patchTurn(owner, (turn) => ({ ...turn, status: "error", error: message }));
            if (workspaceKey() === workspace && core.sessionId === sessionId) {
              emit({ error: message });
            }
          },
        },
        controller.signal,
      );
      if (
        !controller.signal.aborted &&
        isWatchCurrent(key, epoch, controller) &&
        turns[key]?.status === "running"
      ) {
        patchTurn(owner, (turn) => ({
          ...turn,
          status: "error",
          worker: "lost",
          error: turn.error || "同步中断",
        }));
      }
    } catch (err) {
      if (controller.signal.aborted) return;
      const message = err instanceof Error ? err.message : String(err);
      patchTurn(owner, (turn) => ({
        ...turn,
        status: "error",
        worker: "lost",
        error: message,
      }));
    } finally {
      if (watchers.get(key) === controller) watchers.delete(key);
    }
  },
  async reloadMessages(workspace: string, sessionId: string) {
    if (workspaceKey() !== workspace) return;
    const gen = nextGen(messageScope(workspace, sessionId));
    const data = await api.sessionTimeline(ws(), sessionId);
    if (!isGen(messageScope(workspace, sessionId), gen) || workspaceKey() !== workspace) {
      return;
    }
    applyTimeline(workspace, sessionId, data);
  },
  async refreshSessionTasks(sessionId = core.sessionId || "") {
    if (!core.workspace || !sessionId) return;
    const ownerWorkspace = workspaceKey();
    const gen = nextGen(taskScope(ownerWorkspace, sessionId));
    try {
      const data = await api.sessionTimeline(ws(), sessionId);
      if (
        !isGen(taskScope(ownerWorkspace, sessionId), gen) ||
        workspaceKey() !== ownerWorkspace ||
        core.sessionId !== sessionId
      ) {
        return;
      }
      applyTimeline(ownerWorkspace, sessionId, data);
    } catch {
      // Turn executions are an auxiliary read model. Messages remain
      // authoritative when this optional refresh is unavailable.
    }
  },
  async attach(files: FileList | File[]) {
    if (!core.workspace) return;
    if (!core.sessionId) await actions.newSession();
    if (!core.sessionId) return;
    const next = [...draftOf(core.sessionId).attachments];
    for (const file of Array.from(files)) {
      const uri = await api.upload(ws(), file);
      next.push({ name: file.name, uri });
    }
    putDraft(core.sessionId, { attachments: next });
  },
  removeAttachment(uri: string) {
    if (!core.sessionId) return;
    putDraft(core.sessionId, {
      attachments: draftOf(core.sessionId).attachments.filter((a) => a.uri !== uri),
    });
  },
  async steer(instruction: string) {
    if (!core.workspace || !core.sessionId) return;
    const turn = turnOf(core.sessionId);
    await api.steer(ws(), core.sessionId, instruction, turn?.taskId || "");
  },
  async cancel() {
    if (!core.workspace || !core.sessionId) return;
    const sessionId = core.sessionId;
    const ownerWorkspace = workspaceKey();
    const key = bucketKey(ownerWorkspace, sessionId);
    const turn = turnOf(sessionId);
    try {
      const result = await api.cancel(ws(), sessionId, turn?.taskId || "");
      if (!result.settled) return;
      abortWatch(key);
      if (turns[key]) turns[key] = finishTurn(turns[key]);
      emit();
      await actions.reloadMessages(ownerWorkspace, sessionId).catch(() => undefined);
      await actions.refreshSessions().catch(() => undefined);
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "";
      if (code === "not_active" || code === "not_found") {
        abortWatch(key);
        if (turns[key]) turns[key] = finishTurn(turns[key]);
        emit();
        return;
      }
      emit({ error: err instanceof Error ? err.message : String(err) });
    }
  },
  async approveTask(taskId: string) {
    if (!core.workspace) return;
    const awaiting = core.tasks.find(
      (task) => task.id === taskId && task.status === "awaiting_approval",
    );
    if (!awaiting) return;
    await api.approve(ws(), awaiting.id);
    if (core.sessionId) await actions.openSession(core.sessionId, { signalOpen: false });
    await actions.openDrawer("task");
  },
  async openDrawer(drawer: Drawer) {
    if (!core.workspace) {
      emit({ drawer });
      return;
    }
    const request = {
      id: ++taskDrawerRequestId,
      workspace: workspaceKey(),
      rpcWorkspace: ws(),
      sessionId: core.sessionId,
      drawer,
      detailId: ++taskDetailRequestId,
    };
    if (drawer === "artifact") {
      await loadArtifactScope("");
      return;
    }
    const isCurrent = () =>
      request.id === taskDrawerRequestId &&
      request.workspace === workspaceKey() &&
      request.sessionId === core.sessionId &&
      core.drawer === request.drawer;
    emit({
      drawer,
      error: "",
      ...(drawer === "task"
        ? {
            taskSelectedId: "",
            taskDetail: null,
            taskDetailLoading: false,
            taskDetailError: "",
          }
        : {}),
    });
    try {
      if (drawer === "task") {
        const data = await api.listTasks(
          request.rpcWorkspace,
          request.sessionId || "",
          200,
        );
        if (!isCurrent()) return;
        const tasks = (data.tasks || []) as TaskSummary[];
        const focus =
          tasks.find((t) => t.session_id === request.sessionId) || tasks[0];
        emit({
          tasks,
          taskSelectedId: focus?.id || "",
          taskDetail: null,
          taskDetailLoading: Boolean(focus),
          taskDetailError: "",
        });
        if (focus) {
          const detail = await api.getTask(request.rpcWorkspace, focus.id);
          if (!isCurrent() || request.detailId !== taskDetailRequestId) return;
          emit({
            taskDetail: detail as Record<string, unknown>,
            taskDetailLoading: false,
            taskDetailError: "",
          });
        }
      } else if (drawer === "rom") {
        const data = await api.getRom(
          request.rpcWorkspace,
          request.sessionId || "",
        );
        if (!isCurrent()) return;
        emit({ rom: data.rom as Record<string, unknown> });
      } else if (drawer === "notebook") {
        const data = await api.getNotebook(
          request.rpcWorkspace,
          request.sessionId || "",
        );
        if (!isCurrent()) return;
        emit({ notebook: data.notebook });
      } else if (drawer === "cost") {
        const data = await api.getCost(
          request.rpcWorkspace,
          request.sessionId || "",
        );
        if (!isCurrent()) return;
        emit({ cost: data.cost as Record<string, unknown> });
      }
    } catch (err) {
      if (!isCurrent()) return;
      const message = err instanceof Error ? err.message : String(err);
      emit({
        error: message,
        ...(drawer === "task"
          ? { taskDetailLoading: false, taskDetailError: message }
          : {}),
      });
    }
  },
  async showTaskArtifacts(taskId: string) {
    await loadArtifactScope(taskId);
  },
  async showAllArtifacts() {
    await loadArtifactScope("");
  },
  async showTask(id: string) {
    if (!core.workspace) return;
    taskDrawerRequestId += 1;
    const requestId = ++taskDetailRequestId;
    if (core.taskSelectedId === id) {
      emit({
        taskSelectedId: "",
        taskDetail: null,
        taskDetailLoading: false,
        taskDetailError: "",
        error: "",
      });
      return;
    }
    const ownerWorkspace = workspaceKey();
    const rpcWorkspace = ws();
    emit({
      drawer: "task",
      taskSelectedId: id,
      taskDetail: null,
      taskDetailLoading: true,
      taskDetailError: "",
      error: "",
    });
    try {
      const detail = await api.getTask(rpcWorkspace, id);
      if (
        requestId !== taskDetailRequestId ||
        ownerWorkspace !== workspaceKey() ||
        core.drawer !== "task"
      ) {
        return;
      }
      emit({
        taskDetail: detail as Record<string, unknown>,
        taskDetailLoading: false,
        taskDetailError: "",
      });
    } catch (err) {
      if (
        requestId === taskDetailRequestId &&
        ownerWorkspace === workspaceKey() &&
        core.drawer === "task"
      ) {
        const message = err instanceof Error ? err.message : String(err);
        emit({
          taskDetailLoading: false,
          taskDetailError: message,
          error: message,
        });
      }
    }
  },
  async showArtifact(id: string) {
    if (!core.workspace) return;
    const requestId = ++artifactDetailRequestId;
    if (core.artifactDetail?.id === id) {
      emit({ artifactDetail: null, artifactLoading: false, artifactError: "" });
      return;
    }
    const summary = core.artifacts.find((artifact) => artifact.id === id);
    if (!summary) return;
    const ownerWorkspace = workspaceKey();
    const ownerSession = core.sessionId;
    const ownerTaskId = core.artifactTaskId;
    emit({
      artifactDetail: summary,
      artifactLoading: true,
      artifactError: "",
      drawer: "artifact",
    });
    try {
      const data = await api.getArtifact(ws(), id);
      if (
        requestId !== artifactDetailRequestId ||
        workspaceKey() !== ownerWorkspace ||
        core.sessionId !== ownerSession ||
        core.artifactTaskId !== ownerTaskId ||
        core.artifactDetail?.id !== id
      ) {
        return;
      }
      emit({
        artifactDetail: data.artifact as Artifact,
        artifactLoading: false,
        artifactError: "",
      });
    } catch (err) {
      if (
        requestId !== artifactDetailRequestId ||
        workspaceKey() !== ownerWorkspace ||
        core.sessionId !== ownerSession ||
        core.artifactTaskId !== ownerTaskId ||
        core.artifactDetail?.id !== id
      ) {
        return;
      }
      emit({
        artifactLoading: false,
        artifactError: err instanceof Error ? err.message : String(err),
      });
    }
  },
};

void (async () => {
  await actions.refreshCatalog();
  await actions.restoreNav();
})();
