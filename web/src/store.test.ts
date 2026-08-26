import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionTimeline, TaskSummary, TimelineTurn, Workspace } from "./types";

const mocked = vi.hoisted(() => ({
  listWorkspaces: vi.fn(),
  openWorkspace: vi.fn(),
  selectWorkspace: vi.fn(),
  listSessions: vi.fn(),
  getSession: vi.fn(),
  sessionMessages: vi.fn(),
  workspaceInbox: vi.fn(),
  listAllSessions: vi.fn(),
  deleteSessions: vi.fn(),
  hideWorkspaces: vi.fn(),
  unhideWorkspaces: vi.fn(),
  sessionTimeline: vi.fn(),
  listTasks: vi.fn(),
  getTask: vi.fn(),
  listArtifacts: vi.fn(),
  getRom: vi.fn(),
  getNotebook: vi.fn(),
  getCost: vi.fn(),
  startTurn: vi.fn(),
  watchTask: vi.fn(),
  cancel: vi.fn(),
  approve: vi.fn(),
  readNav: vi.fn(),
  writeNav: vi.fn(),
}));

vi.mock("./api", () => {
  class ApiError extends Error {
    code: string;
    extra?: Record<string, unknown>;

    constructor(code: string, message: string, extra?: Record<string, unknown>) {
      super(message);
      this.code = code;
      this.extra = extra;
    }
  }

  return {
    ApiError,
    api: {
      listWorkspaces: mocked.listWorkspaces,
      openWorkspace: mocked.openWorkspace,
      selectWorkspace: mocked.selectWorkspace,
      listSessions: mocked.listSessions,
      getSession: mocked.getSession,
      sessionMessages: mocked.sessionMessages,
      workspaceInbox: mocked.workspaceInbox,
      listAllSessions: mocked.listAllSessions,
      deleteSessions: mocked.deleteSessions,
      hideWorkspaces: mocked.hideWorkspaces,
      unhideWorkspaces: mocked.unhideWorkspaces,
      sessionTimeline: mocked.sessionTimeline,
      listTasks: mocked.listTasks,
      getTask: mocked.getTask,
      listArtifacts: mocked.listArtifacts,
      getRom: mocked.getRom,
      getNotebook: mocked.getNotebook,
      getCost: mocked.getCost,
      startTurn: mocked.startTurn,
      cancel: mocked.cancel,
      approve: mocked.approve,
    },
    watchTask: mocked.watchTask,
  };
});

vi.mock("./nav", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./nav")>();
  return {
    ...actual,
    readNav: mocked.readNav,
    writeNav: mocked.writeNav,
  };
});

type StoreModule = typeof import("./store");

const workspace: Workspace = {
  root: "/tmp/project",
  project_dir: "/tmp/project",
  project_name: "project",
  invocation_cwd: "/tmp/project",
  kind: "workspace",
  label: "project",
  trusted: true,
  writable: true,
  open_path: "/tmp/project",
  artifacts_dir: "/tmp/project/artifacts",
  db: "/tmp/project/sessions.sqlite3",
};

const namedWorkspace: Workspace = {
  ...workspace,
  root: null,
  project_dir: "/tmp/.omni/projects/default",
  project_name: "default",
  invocation_cwd: "/tmp/.omni/projects/default",
  kind: "named",
  label: "default",
  open_path: "/tmp/.omni/projects/default",
  artifacts_dir: "/tmp/.omni/projects/default/artifacts",
  db: "/tmp/.omni/projects/default/sessions.sqlite3",
};

const session = {
  id: "session-a",
  title: "Research session",
  channel: "web",
  status: "active",
};

function task(status: string): TaskSummary {
  return {
    id: "task-a",
    session_id: session.id,
    parent_task_id: "",
    channel: "web",
    status,
    kind: "turn",
    title: "Research task",
    summary: "",
    error: "",
    created_at: "2026-08-19T10:00:00Z",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function readSnapshot(store: StoreModule) {
  let current: ReturnType<StoreModule["useAppState"]> | null = null;
  function Probe() {
    current = store.useAppState();
    return null;
  }
  renderToStaticMarkup(createElement(Probe));
  return current as unknown as ReturnType<StoreModule["useAppState"]>;
}

async function openSession(store: StoreModule) {
  await store.actions.openWorkspace(workspace.open_path);
  await store.actions.openSession(session.id);
  await Promise.resolve();
}

describe("session Task marker read model", () => {
  beforeEach(() => {
    vi.resetModules();
    for (const fn of Object.values(mocked)) fn.mockReset();
    mocked.listWorkspaces.mockResolvedValue({ workspaces: [] });
    mocked.readNav.mockReturnValue(null);
    mocked.openWorkspace.mockResolvedValue({ workspace });
    mocked.selectWorkspace.mockResolvedValue({ workspace: namedWorkspace });
    mocked.listSessions.mockResolvedValue({ sessions: [session] });
    mocked.getSession.mockResolvedValue({ session });
    mocked.sessionMessages.mockResolvedValue({
      messages: [{ id: "message-a", role: "assistant", content: "Core result" }],
    });
    mocked.listTasks.mockResolvedValue({ tasks: [] });
    mocked.listArtifacts.mockResolvedValue({ artifacts: [] });
    mocked.getRom.mockResolvedValue({ rom: {} });
    mocked.getNotebook.mockResolvedValue({ notebook: "", path: "" });
    mocked.getCost.mockResolvedValue({ cost: {} });
    mocked.workspaceInbox.mockImplementation(async (path: string, channel?: string, sessionId?: string) => {
      const listed = await mocked.listSessions(path, channel);
      const focus =
        listed.sessions.find((row: { id: string }) => row.id === sessionId) || null;
      return { sessions: listed.sessions, focus };
    });
    mocked.listAllSessions.mockResolvedValue({ sessions: [], next_cursor: null, errors: [] });
    mocked.deleteSessions.mockResolvedValue({
      deleted_session_ids: [],
      deleted_task_ids: [],
      retained_artifact_count: 0,
    });
    mocked.hideWorkspaces.mockResolvedValue({ project_dirs: [], workspaces: [] });
    mocked.unhideWorkspaces.mockResolvedValue({ project_dirs: [], workspaces: [] });
    mocked.sessionTimeline.mockImplementation(async (path: string, sessionId: string) => {
      const messages = await mocked.sessionMessages(path, sessionId);
      const tasks = await mocked.listTasks(path, sessionId, 200).catch(() => ({ tasks: [] }));
      const selected = await mocked.getSession(path, sessionId).catch(() => null);
      const rows = messages.messages || [];
      const taskRows = (tasks.tasks || []).map((row: TaskSummary & Partial<TimelineTurn>) => ({
        ...row,
        executions: row.executions || [],
      }));
      return {
        session: selected?.session || { ...session, id: sessionId },
        messages: rows,
        turns: taskRows,
        fingerprint: {
          message_count: rows.length,
          last_message_id: rows.at(-1)?.id || "",
          latest_task_id: taskRows[0]?.id || "",
          latest_task_status: taskRows[0]?.status || "",
          latest_event_seq: 0,
        },
        followable: ["pending", "queued", "running", "recovering", "awaiting_approval"].includes(
          String(taskRows[0]?.status || ""),
        ),
      };
    });
    mocked.getTask.mockResolvedValue({ task: null });
    mocked.listArtifacts.mockResolvedValue({ artifacts: [] });
    mocked.startTurn.mockResolvedValue({
      session_id: session.id,
      task_id: "task-started",
      client_run_id: "run-started",
      channel: "web",
      kind: "turn",
    });
    mocked.cancel.mockResolvedValue({
      task_id: "task-a",
      action: "cancel",
      settled: true,
      status: "cancelled",
    });
    mocked.approve.mockResolvedValue({ task_id: "task-a", status: "running" });
  });

  it("publishes a new open revision before a current-session reload settles", async () => {
    const store = await import("./store");
    expect(readSnapshot(store).sessionOpenRevision).toBe(0);
    await openSession(store);
    const firstOpen = readSnapshot(store);
    expect(firstOpen.sessionOpenRevision).toBe(1);
    const pendingReload = deferred<SessionTimeline>();
    mocked.sessionTimeline.mockImplementationOnce(() => pendingReload.promise);

    const reopening = store.actions.openSession(session.id);
    const reopened = readSnapshot(store);

    expect(reopened.sessionId).toBe(session.id);
    expect(reopened.sessionOpenRevision).toBe(firstOpen.sessionOpenRevision + 1);
    pendingReload.reject(new Error("reload failed"));
    await reopening;
    expect(readSnapshot(store).sessionOpenRevision).toBe(reopened.sessionOpenRevision);
    expect(readSnapshot(store).error).toBe("reload failed");
  });

  it("does not publish an open revision for an internal approval refresh", async () => {
    const awaiting = task("awaiting_approval");
    mocked.listTasks.mockResolvedValue({ tasks: [awaiting] });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    const revision = readSnapshot(store).sessionOpenRevision;

    await store.actions.approveTask(awaiting.id);

    expect(mocked.approve).toHaveBeenCalledWith(workspace.open_path, awaiting.id);
    expect(readSnapshot(store).sessionOpenRevision).toBe(revision);
  });

  it("restores a named workspace and exact session without requiring it in session.list", async () => {
    mocked.readNav.mockReturnValue({
      workspace: {
        kind: "named",
        projectDir: namedWorkspace.project_dir,
        name: namedWorkspace.project_name,
      },
      sessionId: "session-outside-first-page",
    });
    mocked.listSessions.mockResolvedValue({ sessions: [] });
    mocked.getSession.mockResolvedValue({
      session: { ...session, id: "session-outside-first-page" },
    });
    mocked.sessionMessages.mockResolvedValue({
      messages: [{ id: "restored", role: "assistant", content: "Restored result" }],
    });
    const store = await import("./store");

    await vi.waitFor(() => {
      expect(readSnapshot(store).sessionId).toBe("session-outside-first-page");
      expect(readSnapshot(store).messages[0]?.content).toBe("Restored result");
    });

    expect(mocked.selectWorkspace).toHaveBeenCalledWith({
      project_dir: namedWorkspace.project_dir,
      name: namedWorkspace.project_name,
    });
    expect(mocked.openWorkspace).not.toHaveBeenCalled();
    expect(readSnapshot(store).sessions.map((item) => item.id)).toContain(
      "session-outside-first-page",
    );
    expect(mocked.writeNav).not.toHaveBeenCalledWith(namedWorkspace, null);
    expect(mocked.writeNav).toHaveBeenLastCalledWith(
      namedWorkspace,
      "session-outside-first-page",
      expect.objectContaining({ push: expect.any(Boolean) }),
    );
  });

  it("migrates a legacy named-project path through the catalog selector", async () => {
    mocked.readNav.mockReturnValue({
      workspace: { kind: "path", path: namedWorkspace.project_dir },
      sessionId: session.id,
    });
    mocked.listWorkspaces.mockResolvedValue({
      workspaces: [
        {
          name: namedWorkspace.project_name,
          root: null,
          project_dir: namedWorkspace.project_dir,
          kind: "named",
          label: namedWorkspace.label,
          last_seen: 1,
        },
      ],
    });
    const store = await import("./store");

    await vi.waitFor(() => expect(readSnapshot(store).sessionId).toBe(session.id));

    expect(mocked.selectWorkspace).toHaveBeenCalledWith({
      project_dir: namedWorkspace.project_dir,
      name: namedWorkspace.project_name,
    });
    expect(mocked.openWorkspace).not.toHaveBeenCalled();
  });

  it("synchronizes cross-channel messages and starts one watcher for a new active task", async () => {
    const store = await import("./store");
    await openSession(store);
    const active = task("running");
    mocked.listSessions.mockResolvedValue({
      sessions: [
        {
          ...session,
          messages: 2,
          latest_task_id: active.id,
          latest_task_status: active.status,
          worker: "external",
        },
      ],
    });
    mocked.sessionMessages.mockResolvedValue({
      messages: [
        { id: "message-a", role: "assistant", content: "Core result" },
        { id: "message-b", role: "user", content: "Sent from WeChat" },
      ],
    });
    mocked.listTasks.mockResolvedValue({ tasks: [active] });
    mocked.watchTask.mockImplementation(() => new Promise<void>(() => undefined));

    await store.actions.syncWorkspace();
    await store.actions.syncWorkspace();

    const snapshot = readSnapshot(store);
    expect(snapshot.messages.map((message) => message.content)).toEqual([
      "Core result",
      "Sent from WeChat",
    ]);
    expect(snapshot.sessionTasks).toEqual([active]);
    expect(mocked.watchTask).toHaveBeenCalledTimes(1);
  });

  it("does not attach a live watcher for a completed latest task", async () => {
    const completed = { ...task("succeeded"), id: "task-completed" };
    const completedSession = {
      ...session,
      messages: 1,
      latest_task_id: completed.id,
      latest_task_status: completed.status,
    };
    mocked.listSessions.mockResolvedValue({ sessions: [completedSession] });
    mocked.getSession.mockResolvedValue({ session: completedSession });
    mocked.listTasks.mockResolvedValue({
      tasks: [{ ...completed, executions: [{ id: "execution-1", skill_name: "research-pptx", status: "succeeded" }] }],
    });
    const store = await import("./store");

    await store.actions.openWorkspace(workspace.open_path);
    await store.actions.openSession(session.id);

    expect(mocked.watchTask).not.toHaveBeenCalled();
    expect(readSnapshot(store).sessionTurns[0]?.id).toBe(completed.id);
    expect(readSnapshot(store).sessionTurns[0]?.executions?.[0]?.skill_name).toBe("research-pptx");
  });

  it("does not follow an older running turn after a newer root turn finished", async () => {
    const completed = { ...task("succeeded"), id: "task-new", created_at: "2026-08-19T12:00:00Z" };
    const zombie = { ...task("running"), id: "task-old", created_at: "2026-08-19T03:00:00Z" };
    mocked.listTasks.mockResolvedValue({ tasks: [completed, zombie] });
    mocked.getSession.mockResolvedValue({
      session: {
        ...session,
        latest_task_id: completed.id,
        latest_task_status: completed.status,
      },
    });
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);
    await store.actions.openSession(session.id);

    expect(mocked.watchTask).not.toHaveBeenCalled();
    expect(readSnapshot(store).streaming).toBe(false);
  });

  it("unlocks a leftover watch when the latest root turn is no longer followable", async () => {
    mocked.watchTask.mockImplementation(() => new Promise(() => undefined));
    mocked.listTasks.mockResolvedValue({ tasks: [task("running")] });
    const store = await import("./store");
    await openSession(store);
    expect(readSnapshot(store).streaming).toBe(true);

    const completed = { ...task("succeeded"), id: "task-new" };
    const zombie = { ...task("running"), id: "task-a" };
    mocked.listTasks.mockResolvedValue({ tasks: [completed, zombie] });
    mocked.listSessions.mockResolvedValue({
      sessions: [
        {
          ...session,
          messages: 1,
          last_message_id: "message-a",
          latest_task_id: completed.id,
          latest_task_status: completed.status,
          latest_event_seq: 1,
        },
      ],
    });
    mocked.getSession.mockResolvedValue({
      session: {
        ...session,
        latest_task_id: completed.id,
        latest_task_status: completed.status,
      },
    });

    await store.actions.syncWorkspace();

    expect(readSnapshot(store).streaming).toBe(false);
    expect(readSnapshot(store).currentTurn?.status).toBe("done");
  });

  it("clears local streaming after a settled cancel", async () => {
    mocked.watchTask.mockImplementation(() => new Promise(() => undefined));
    mocked.listTasks.mockResolvedValue({ tasks: [task("running")] });
    mocked.cancel.mockImplementation(async () => {
      mocked.listTasks.mockResolvedValue({ tasks: [task("cancelled")] });
      mocked.getSession.mockResolvedValue({
        session: {
          ...session,
          latest_task_id: "task-a",
          latest_task_status: "cancelled",
        },
      });
      return {
        task_id: "task-a",
        action: "cancel",
        settled: true,
        status: "cancelled",
      };
    });
    const store = await import("./store");
    await openSession(store);
    expect(readSnapshot(store).streaming).toBe(true);

    await store.actions.cancel();

    expect(mocked.cancel).toHaveBeenCalledWith(workspace.open_path, session.id, "task-a");
    expect(readSnapshot(store).streaming).toBe(false);
    expect(readSnapshot(store).currentTurn?.status).toBe("done");
  });

  it("does not download the transcript again while its server fingerprint is unchanged", async () => {
    const store = await import("./store");
    await openSession(store);
    mocked.sessionMessages.mockClear();
    mocked.sessionTimeline.mockClear();
    mocked.listSessions.mockResolvedValue({
      sessions: [
        {
          ...session,
          messages: 1,
          last_message_id: "message-a",
          latest_task_id: "",
          latest_task_status: "",
          latest_event_seq: 0,
          updated_at: "2026-08-19T10:00:00Z",
        },
      ],
    });
    mocked.getSession.mockResolvedValue({
      session: { ...session, updated_at: "2026-08-19T10:00:00Z" },
    });

    await store.actions.syncWorkspace();
    await store.actions.syncWorkspace();

    expect(mocked.sessionTimeline).not.toHaveBeenCalled();
    expect(mocked.sessionMessages).not.toHaveBeenCalled();
    expect(readSnapshot(store).messages.map((message) => message.content)).toEqual([
      "Core result",
    ]);
  });

  it("downloads the transcript once when the durable message count changes", async () => {
    const store = await import("./store");
    await openSession(store);
    mocked.sessionMessages.mockClear();
    mocked.sessionTimeline.mockClear();
    mocked.listSessions.mockResolvedValue({
      sessions: [
        {
          ...session,
          messages: 2,
          last_message_id: "message-b",
          latest_task_id: "",
          latest_task_status: "",
          latest_event_seq: 0,
          updated_at: "2026-08-19T10:01:00Z",
        },
      ],
    });
    mocked.getSession.mockResolvedValue({
      session: { ...session, updated_at: "2026-08-19T10:01:00Z" },
    });
    mocked.sessionMessages.mockResolvedValue({
      messages: [
        { id: "message-a", role: "assistant", content: "Core result" },
        { id: "message-b", role: "user", content: "New message" },
      ],
    });

    await store.actions.syncWorkspace();
    await store.actions.syncWorkspace();

    expect(mocked.sessionTimeline).toHaveBeenCalledTimes(1);
    expect(mocked.sessionMessages).toHaveBeenCalledTimes(1);
    expect(readSnapshot(store).messages.map((message) => message.content)).toEqual([
      "Core result",
      "New message",
    ]);
  });

  it("keeps message synchronization independent from the auxiliary task index", async () => {
    const store = await import("./store");
    await openSession(store);
    mocked.sessionMessages.mockResolvedValue({
      messages: [
        { id: "message-a", role: "assistant", content: "Core result" },
        { id: "message-wechat", role: "user", content: "WeChat update" },
      ],
    });
    mocked.listSessions.mockResolvedValue({ sessions: [{ ...session, messages: 2 }] });
    mocked.listTasks.mockRejectedValue(new Error("task index unavailable"));

    await store.actions.syncWorkspace();

    expect(readSnapshot(store).messages.map((message) => message.content)).toEqual([
      "Core result",
      "WeChat update",
    ]);
    expect(readSnapshot(store).error).toBe("");
  });

  it("does not let a late synchronization overwrite a newly opened workspace", async () => {
    const store = await import("./store");
    await openSession(store);
    const staleInbox = deferred<{
      sessions: Array<typeof session>;
      focus: typeof session | null;
    }>();
    mocked.workspaceInbox.mockImplementation((path: string) =>
      path === workspace.open_path
        ? staleInbox.promise
        : Promise.resolve({
            sessions: [{ ...session, id: "session-b", title: "B" }],
            focus: null,
          }),
    );
    mocked.sessionTimeline.mockImplementation((path: string) =>
      path === workspace.open_path
        ? new Promise(() => undefined)
        : Promise.resolve({
            session: { ...session, id: "session-b", title: "B" },
            messages: [],
            turns: [],
            fingerprint: { message_count: 0, last_message_id: "" },
            followable: false,
          }),
    );
    const syncing = store.actions.syncWorkspace();
    const workspaceB = {
      ...workspace,
      root: "/tmp/project-b",
      project_dir: "/tmp/project-b",
      project_name: "project-b",
      invocation_cwd: "/tmp/project-b",
      label: "project-b",
      open_path: "/tmp/project-b",
      artifacts_dir: "/tmp/project-b/artifacts",
      db: "/tmp/project-b/sessions.sqlite3",
    };
    mocked.openWorkspace.mockResolvedValueOnce({ workspace: workspaceB });
    await store.actions.openWorkspace(workspaceB.open_path);
    staleInbox.resolve({ sessions: [{ ...session, title: "stale A" }], focus: null });
    await syncing;

    expect(readSnapshot(store).workspace?.open_path).toBe(workspaceB.open_path);
    expect(readSnapshot(store).sessions.map((item) => item.id)).toEqual(["session-b"]);
    expect(readSnapshot(store).messages).toEqual([]);
  });

  it("keeps the latest workspace selection when an older open finishes late", async () => {
    const store = await import("./store");
    const older = deferred<{ workspace: Workspace }>();
    const workspaceB = {
      ...workspace,
      root: "/tmp/project-b",
      project_dir: "/tmp/project-b",
      project_name: "project-b",
      invocation_cwd: "/tmp/project-b",
      label: "project-b",
      open_path: "/tmp/project-b",
      artifacts_dir: "/tmp/project-b/artifacts",
      db: "/tmp/project-b/sessions.sqlite3",
    };
    mocked.openWorkspace
      .mockImplementationOnce(() => older.promise)
      .mockResolvedValueOnce({ workspace: workspaceB });
    mocked.listSessions.mockResolvedValue({ sessions: [] });

    const first = store.actions.openWorkspace(workspace.open_path);
    await store.actions.openWorkspace(workspaceB.open_path);
    older.resolve({ workspace });
    await first;

    expect(readSnapshot(store).workspace?.open_path).toBe(workspaceB.open_path);
  });

  it("does not let an auxiliary task.list failure block the conversation", async () => {
    mocked.listTasks.mockRejectedValue(new Error("task index unavailable"));
    const store = await import("./store");

    await openSession(store);
    const snapshot = readSnapshot(store);

    expect(snapshot.messages.map((message) => message.content)).toEqual(["Core result"]);
    expect(snapshot.sessionId).toBe(session.id);
    expect(snapshot.sessionTasks).toEqual([]);
    expect(snapshot.error).toBe("");
  });

  it("drops the optimistic user row when turn.start fails", async () => {
    mocked.startTurn.mockRejectedValueOnce(
      Object.assign(new Error("capacity reached"), { code: "capacity" }),
    );
    const store = await import("./store");
    await openSession(store);
    store.actions.setComposer("try from web");

    await store.actions.send();

    expect(readSnapshot(store).messages.map((item) => item.content)).toEqual(["Core result"]);
    expect(readSnapshot(store).messages.some((item) => item.id.startsWith("local-"))).toBe(false);
    expect(readSnapshot(store).error).toContain("capacity reached");
  });

  it("unlocks the composer when the observation stream fails", async () => {
    mocked.watchTask.mockRejectedValueOnce(new Error("stream reset"));
    const store = await import("./store");
    await openSession(store);

    await store.actions.watchExisting(session.id, "task-a", "run-a", workspace.open_path);

    expect(readSnapshot(store).streaming).toBe(false);
    expect(readSnapshot(store).currentTurn?.status).toBe("error");
    expect(readSnapshot(store).currentTurn?.worker).toBe("lost");
  });

  it("settles a locally running turn once the durable task is no longer followable", async () => {
    mocked.watchTask.mockImplementation(() => new Promise(() => undefined));
    mocked.listTasks.mockResolvedValue({ tasks: [task("running")] });
    const store = await import("./store");
    await openSession(store);
    expect(readSnapshot(store).streaming).toBe(true);

    mocked.listSessions.mockResolvedValue({
      sessions: [
        {
          ...session,
          messages: 2,
          last_message_id: "message-b",
          latest_task_id: "task-a",
          latest_task_status: "succeeded",
          latest_event_seq: 1,
        },
      ],
    });
    mocked.listTasks.mockResolvedValue({ tasks: [task("succeeded")] });
    mocked.sessionMessages.mockResolvedValue({
      messages: [
        { id: "message-a", role: "assistant", content: "Core result" },
        { id: "message-b", role: "user", content: "done" },
      ],
    });

    await store.actions.syncWorkspace();

    expect(readSnapshot(store).currentTurn?.status).toBe("done");
    expect(readSnapshot(store).streaming).toBe(false);
  });

  it("clears the open session when the hash only names the workspace", async () => {
    const store = await import("./store");
    await openSession(store);
    mocked.readNav.mockReturnValue({
      workspace: { kind: "path", path: workspace.open_path },
      sessionId: null,
    });

    await store.actions.restoreFromHash();

    expect(readSnapshot(store).sessionId).toBeNull();
    expect(readSnapshot(store).workspace?.open_path).toBe(workspace.open_path);
    expect(readSnapshot(store).messages).toEqual([]);
  });

  it("does not let a leftover watch reload wipe a newly opened session", async () => {
    const store = await import("./store");
    await openSession(store);
    const lateReload = deferred<{
      session: typeof session;
      messages: { id: string; role: string; content: string }[];
      turns: TaskSummary[];
      fingerprint: { message_count: number; last_message_id: string };
      followable: boolean;
    }>();
    mocked.watchTask.mockImplementationOnce(async (...args: unknown[]) => {
      const handlers = args[3] as { onDone?: (info: Record<string, unknown>) => void };
      handlers.onDone?.({});
    });
    mocked.listTasks.mockResolvedValue({ tasks: [task("running")] });
    mocked.sessionTimeline.mockImplementation(async (_path: string, sessionId: string) => {
      if (sessionId === "session-b") {
        return {
          session: { ...session, id: "session-b", title: "B" },
          messages: [{ id: "b1", role: "assistant", content: "session B" }],
          turns: [],
          fingerprint: { message_count: 1, last_message_id: "b1" },
          followable: false,
        };
      }
      return lateReload.promise;
    });
    const watching = store.actions.watchExisting(
      session.id,
      "task-a",
      "run-a",
      workspace.open_path,
    );
    await Promise.resolve();
    const openingB = store.actions.openSession("session-b");
    await openingB;
    lateReload.resolve({
      session,
      messages: [{ id: "stale-a", role: "assistant", content: "stale A" }],
      turns: [],
      fingerprint: { message_count: 1, last_message_id: "stale-a" },
      followable: false,
    });
    await watching;

    expect(readSnapshot(store).sessionId).toBe("session-b");
    expect(readSnapshot(store).messages.map((item) => item.content)).toEqual(["session B"]);
  });

  it("rejects an older same-session task snapshot that finishes last", async () => {
    const store = await import("./store");
    await openSession(store);
    mocked.listTasks.mockReset();
    const older = deferred<{ tasks: TaskSummary[] }>();
    const newer = deferred<{ tasks: TaskSummary[] }>();
    mocked.listTasks
      .mockImplementationOnce(() => older.promise)
      .mockImplementationOnce(() => newer.promise);

    const first = store.actions.refreshSessionTasks(session.id);
    const second = store.actions.refreshSessionTasks(session.id);
    newer.resolve({ tasks: [task("succeeded")] });
    await second;
    older.resolve({ tasks: [task("running")] });
    await first;

    expect(readSnapshot(store).sessionTasks.map((item) => item.status)).toEqual(["succeeded"]);
  });

  it("records the terminal turn state before auxiliary refreshes complete", async () => {
    mocked.listTasks.mockRejectedValue(new Error("task index unavailable"));
    mocked.watchTask.mockImplementation(async (...args: unknown[]) => {
      const handlers = args[3] as {
        onPartial?: (text: string) => void;
        onDone?: (info: Record<string, unknown>) => void;
      };
      handlers.onPartial?.("fallback result");
      handlers.onDone?.({});
    });
    const store = await import("./store");
    await openSession(store);

    await store.actions.watchExisting(
      session.id,
      "task-running",
      "client-run",
      workspace.open_path,
    );

    expect(readSnapshot(store).currentTurn?.status).toBe("done");
    expect(readSnapshot(store).currentTurn?.worker).toBe("");
  });

  it("starts a new Task watch with an empty timeline and cursor", async () => {
    mocked.watchTask
      .mockImplementationOnce(async (...args: unknown[]) => {
        const handlers = args[3] as {
          onPartial?: (text: string) => void;
          onActivity?: (item: Record<string, unknown>) => void;
        };
        handlers.onPartial?.("old partial");
        handlers.onActivity?.({
          task_id: "task-old",
          seq: 50,
          kind: "tool",
          phase: "done",
          status: "succeeded",
          title: "old activity",
          summary: "old",
        });
      })
      .mockImplementationOnce(async (...args: unknown[]) => {
        const afterSeq = args[2] as number;
        const handlers = args[3] as {
          onActivity?: (item: Record<string, unknown>) => void;
        };
        expect(afterSeq).toBe(0);
        handlers.onActivity?.({
          task_id: "task-new",
          seq: 1,
          kind: "plan",
          phase: "plan",
          status: "running",
          title: "new activity",
          summary: "new",
        });
      });
    const store = await import("./store");
    await openSession(store);

    await store.actions.watchExisting(
      session.id,
      "task-old",
      "",
      workspace.open_path,
    );
    await store.actions.watchExisting(
      session.id,
      "task-new",
      "",
      workspace.open_path,
    );

    const turn = readSnapshot(store).currentTurn;
    expect(turn?.taskId).toBe("task-new");
    expect(turn?.partialText).toBe("");
    expect(turn?.lastEventSeq).toBe(1);
    expect(turn?.activities.map((item) => item.title)).toEqual(["new activity"]);
  });

  it("keeps a pending turn watch bound to its origin workspace", async () => {
    const started = deferred<{
      session_id: string;
      task_id: string;
      client_run_id: string;
      channel: string;
      kind: string;
    }>();
    mocked.startTurn.mockImplementationOnce(() => started.promise);
    mocked.watchTask.mockImplementationOnce(async (...args: unknown[]) => {
      const handlers = args[3] as { onError?: (message: string) => void };
      handlers.onError?.("origin workspace failed");
    });
    const store = await import("./store");
    await openSession(store);
    store.actions.setComposer("start in workspace A");

    const sending = store.actions.send();
    await vi.waitFor(() => expect(mocked.startTurn).toHaveBeenCalledTimes(1));
    const workspaceB = {
      ...workspace,
      root: "/tmp/project-b",
      project_dir: "/tmp/project-b",
      project_name: "project-b",
      invocation_cwd: "/tmp/project-b",
      label: "project-b",
      open_path: "/tmp/project-b",
      artifacts_dir: "/tmp/project-b/artifacts",
      db: "/tmp/project-b/sessions.sqlite3",
    };
    mocked.openWorkspace.mockResolvedValueOnce({ workspace: workspaceB });
    await store.actions.openWorkspace(workspaceB.open_path);
    await store.actions.openSession(session.id);
    started.resolve({
      session_id: session.id,
      task_id: "task-from-a",
      client_run_id: "run-from-a",
      channel: "web",
      kind: "turn",
    });
    await sending;

    expect(mocked.watchTask).toHaveBeenCalledWith(
      workspace.open_path,
      "task-from-a",
      0,
      expect.any(Object),
      expect.any(AbortSignal),
    );
    expect(readSnapshot(store).workspace?.open_path).toBe(workspaceB.open_path);
    expect(readSnapshot(store).sessionId).toBe(session.id);
    expect(readSnapshot(store).error).toBe("");
  });

  it("does not rewrite navigation storage for streamed turn updates", async () => {
    mocked.watchTask.mockImplementationOnce(async (...args: unknown[]) => {
      const handlers = args[3] as {
        onToken?: (text: string) => void;
        onActivity?: (item: Record<string, unknown>) => void;
      };
      handlers.onToken?.("one");
      handlers.onToken?.("two");
      handlers.onActivity?.({
        task_id: "task-stream",
        seq: 1,
        kind: "tool",
        phase: "done",
        status: "succeeded",
        title: "tool done",
        summary: "done",
      });
    });
    const store = await import("./store");
    await openSession(store);
    mocked.writeNav.mockClear();

    await store.actions.watchExisting(
      session.id,
      "task-stream",
      "run-stream",
      workspace.open_path,
    );

    expect(readSnapshot(store).streamingText).toBe("onetwo");
    expect(mocked.writeNav).not.toHaveBeenCalled();
  });

  it("refreshes an open Task inspector while its Task is active", async () => {
    const active = task("running");
    mocked.listTasks.mockResolvedValue({ tasks: [active] });
    mocked.getTask
      .mockResolvedValueOnce({
        task: active,
        executions: [{ id: "execution-1", status: "running" }],
      })
      .mockResolvedValueOnce({
        task: active,
        executions: [
          { id: "execution-1", status: "succeeded" },
          { id: "execution-2", status: "running" },
        ],
      });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");

    await store.actions.syncWorkspace();
    await vi.waitFor(() => expect(mocked.getTask).toHaveBeenCalledTimes(2));

    const executions = readSnapshot(store).taskDetail?.executions as unknown[];
    expect(executions).toHaveLength(2);
  });

  it("refreshes an open Artifact inspector while the session has active work", async () => {
    const active = task("running");
    mocked.listTasks.mockResolvedValue({ tasks: [active] });
    mocked.listArtifacts
      .mockResolvedValueOnce({
        artifacts: [{ id: "artifact-1", task_id: active.id, title: "draft" }],
      })
      .mockResolvedValueOnce({
        artifacts: [
          { id: "artifact-1", task_id: active.id, title: "draft" },
          { id: "artifact-2", task_id: active.id, title: "final" },
        ],
      });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("artifact");

    await store.actions.syncWorkspace();
    await vi.waitFor(() => expect(mocked.listArtifacts).toHaveBeenCalledTimes(2));

    expect(readSnapshot(store).artifacts.map((item) => item.id)).toEqual([
      "artifact-1",
      "artifact-2",
    ]);
    expect(readSnapshot(store).artifactListLoading).toBe(false);
  });

  it("does not let a late Task drawer request cross a workspace switch", async () => {
    const store = await import("./store");
    await openSession(store);
    const staleTasks = deferred<{ tasks: TaskSummary[] }>();
    mocked.listTasks.mockImplementationOnce(() => staleTasks.promise);
    const opening = store.actions.openDrawer("task");
    const workspaceB = {
      ...workspace,
      root: "/tmp/project-b",
      project_dir: "/tmp/project-b",
      project_name: "project-b",
      invocation_cwd: "/tmp/project-b",
      label: "project-b",
      open_path: "/tmp/project-b",
      artifacts_dir: "/tmp/project-b/artifacts",
      db: "/tmp/project-b/sessions.sqlite3",
    };
    mocked.openWorkspace.mockResolvedValueOnce({ workspace: workspaceB });
    await store.actions.openWorkspace(workspaceB.open_path);
    staleTasks.resolve({ tasks: [task("running")] });
    await opening;

    expect(readSnapshot(store).workspace?.open_path).toBe(workspaceB.open_path);
    expect(readSnapshot(store).tasks).toEqual([]);
    expect(readSnapshot(store).taskSelectedId).toBe("");
    expect(readSnapshot(store).taskDetail).toBeNull();
    expect(readSnapshot(store).taskDetailLoading).toBe(false);
    expect(readSnapshot(store).taskDetailError).toBe("");
  });

  it("does not let a slower Task detail replace the latest selection", async () => {
    const first = { ...task("running"), id: "task-first" };
    const second = { ...task("running"), id: "task-second" };
    mocked.listTasks.mockResolvedValue({ tasks: [first, second] });
    mocked.getTask.mockResolvedValueOnce({ task: first });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    await store.actions.showTask(first.id);
    const slowFirst = deferred<{ task: TaskSummary }>();
    const fastSecond = deferred<{ task: TaskSummary }>();
    mocked.getTask
      .mockImplementationOnce(() => slowFirst.promise)
      .mockImplementationOnce(() => fastSecond.promise);

    const selectingFirst = store.actions.showTask(first.id);
    const selectingSecond = store.actions.showTask(second.id);
    expect(readSnapshot(store).taskSelectedId).toBe(second.id);
    expect(readSnapshot(store).taskDetailLoading).toBe(true);
    expect(readSnapshot(store).taskDetail).toBeNull();
    fastSecond.resolve({ task: second });
    await selectingSecond;
    slowFirst.resolve({ task: first });
    await selectingFirst;

    expect((readSnapshot(store).taskDetail?.task as TaskSummary).id).toBe(second.id);
    expect(readSnapshot(store).taskDetailLoading).toBe(false);
  });

  it("selects the requested Task immediately and keeps failures on that inline row", async () => {
    const first = { ...task("succeeded"), id: "task-first" };
    const second = { ...task("running"), id: "task-second" };
    mocked.listTasks.mockResolvedValue({ tasks: [first, second] });
    mocked.getTask.mockResolvedValueOnce({ task: first });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    const pending = deferred<{ task: TaskSummary }>();
    mocked.getTask.mockImplementationOnce(() => pending.promise);

    const selecting = store.actions.showTask(second.id);
    expect(readSnapshot(store).taskSelectedId).toBe(second.id);
    expect(readSnapshot(store).taskDetailLoading).toBe(true);
    expect(readSnapshot(store).taskDetail).toBeNull();

    pending.reject(new Error("detail unavailable"));
    await selecting;

    expect(readSnapshot(store).taskSelectedId).toBe(second.id);
    expect(readSnapshot(store).taskDetailLoading).toBe(false);
    expect(readSnapshot(store).taskDetailError).toBe("detail unavailable");
  });

  it("collapses the active Task without fetching it again", async () => {
    const active = task("succeeded");
    mocked.listTasks.mockResolvedValue({ tasks: [active] });
    mocked.getTask.mockResolvedValue({ task: active });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    expect((readSnapshot(store).taskDetail?.task as TaskSummary).id).toBe(active.id);
    mocked.getTask.mockClear();

    await store.actions.showTask(active.id);

    expect(readSnapshot(store).taskDetail).toBeNull();
    expect(readSnapshot(store).taskSelectedId).toBe("");
    expect(readSnapshot(store).taskDetailLoading).toBe(false);
    expect(mocked.getTask).not.toHaveBeenCalled();
  });

  it("keeps Task inspector background refresh single-flight", async () => {
    const active = task("running");
    mocked.listTasks.mockResolvedValue({ tasks: [active] });
    mocked.getTask.mockResolvedValueOnce({ task: active });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    const pending = deferred<{ task: TaskSummary }>();
    mocked.getTask.mockImplementation(() => pending.promise);
    mocked.getTask.mockClear();

    await store.actions.syncWorkspace();
    await store.actions.syncWorkspace();

    expect(mocked.getTask).toHaveBeenCalledTimes(1);
    pending.resolve({ task: active });
    await vi.waitFor(() => expect(readSnapshot(store).taskDetail).not.toBeNull());
  });

  it("keeps Artifact inspector background refresh single-flight", async () => {
    const active = task("running");
    mocked.listTasks.mockResolvedValue({ tasks: [active] });
    mocked.listArtifacts.mockResolvedValueOnce({ artifacts: [] });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("artifact");
    const pending = deferred<{ artifacts: never[] }>();
    mocked.listArtifacts.mockImplementation(() => pending.promise);
    mocked.listArtifacts.mockClear();

    await store.actions.syncWorkspace();
    await store.actions.syncWorkspace();

    expect(mocked.listArtifacts).toHaveBeenCalledTimes(1);
    pending.resolve({ artifacts: [] });
    await vi.waitFor(() => expect(readSnapshot(store).artifactListLoading).toBe(false));
  });
});

describe("inspector workspace vs session scope", () => {
  beforeEach(() => {
    vi.resetModules();
    for (const fn of Object.values(mocked)) fn.mockReset();
    mocked.listWorkspaces.mockResolvedValue({ workspaces: [] });
    mocked.readNav.mockReturnValue(null);
    mocked.openWorkspace.mockResolvedValue({ workspace });
    mocked.selectWorkspace.mockResolvedValue({ workspace: namedWorkspace });
    mocked.listSessions.mockResolvedValue({ sessions: [session] });
    mocked.getSession.mockResolvedValue({ session });
    mocked.sessionMessages.mockResolvedValue({
      messages: [{ id: "message-a", role: "assistant", content: "Core result" }],
    });
    mocked.listTasks.mockResolvedValue({ tasks: [] });
    mocked.listArtifacts.mockResolvedValue({ artifacts: [] });
    mocked.getRom.mockResolvedValue({ rom: {} });
    mocked.getNotebook.mockResolvedValue({ notebook: "", path: "" });
    mocked.getCost.mockResolvedValue({ cost: {} });
    mocked.workspaceInbox.mockImplementation(async (path: string, channel?: string, sessionId?: string) => {
      const listed = await mocked.listSessions(path, channel);
      const focus =
        listed.sessions.find((row: { id: string }) => row.id === sessionId) || null;
      return { sessions: listed.sessions, focus };
    });
    mocked.sessionTimeline.mockImplementation(async (path: string, sessionId: string) => {
      const messages = await mocked.sessionMessages(path, sessionId);
      const tasks = await mocked.listTasks(path, sessionId, 200).catch(() => ({ tasks: [] }));
      const selected = await mocked.getSession(path, sessionId).catch(() => null);
      const rows = messages.messages || [];
      const taskRows = (tasks.tasks || []).map((row: TaskSummary & Partial<TimelineTurn>) => ({
        ...row,
        executions: row.executions || [],
      }));
      return {
        session: selected?.session || { ...session, id: sessionId },
        messages: rows,
        turns: taskRows,
        fingerprint: {
          message_count: rows.length,
          last_message_id: rows.at(-1)?.id || "",
          latest_task_id: taskRows[0]?.id || "",
          latest_task_status: taskRows[0]?.status || "",
          latest_event_seq: 0,
        },
        followable: false,
      };
    });
  });

  it("lists Task drawer rows for the open session", async () => {
    const sessionTask = task("succeeded");
    const other = { ...task("succeeded"), id: "task-other", session_id: "session-b" };
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask] });
    mocked.getTask.mockResolvedValue({ task: sessionTask });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");

    expect(mocked.listTasks).toHaveBeenCalledWith(workspace.open_path, session.id, 200);
    expect(readSnapshot(store).tasks.map((item) => item.id)).toEqual([sessionTask.id]);
    expect(readSnapshot(store).tasks).not.toEqual(expect.arrayContaining([other]));
  });

  it("lists Task drawer rows for the whole workspace when no session is open", async () => {
    const sessionTask = task("succeeded");
    const other = { ...task("succeeded"), id: "task-other", session_id: "session-b" };
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask, other] });
    mocked.getTask.mockResolvedValue({ task: sessionTask });
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);
    await store.actions.openDrawer("task");

    expect(mocked.listTasks).toHaveBeenCalledWith(workspace.open_path, "", 200);
    expect(readSnapshot(store).sessionId).toBeNull();
    expect(readSnapshot(store).tasks.map((item) => item.id)).toEqual([
      sessionTask.id,
      other.id,
    ]);
  });

  it("reloads an open inspector when the user opens a session", async () => {
    const workspaceTask = { ...task("succeeded"), id: "task-workspace", session_id: "session-b" };
    const sessionTask = task("succeeded");
    mocked.listTasks.mockResolvedValue({ tasks: [workspaceTask, sessionTask] });
    mocked.getTask.mockResolvedValue({ task: workspaceTask });
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);
    await store.actions.openDrawer("task");
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask] });
    mocked.getTask.mockResolvedValue({ task: sessionTask });
    mocked.listTasks.mockClear();

    await store.actions.openSession(session.id);
    await vi.waitFor(() =>
      expect(mocked.listTasks).toHaveBeenCalledWith(workspace.open_path, session.id, 200),
    );
    expect(readSnapshot(store).tasks.map((item) => item.id)).toEqual([sessionTask.id]);
  });

  it("reloads an open inspector when the user returns to the workspace", async () => {
    const sessionTask = task("succeeded");
    const workspaceTask = { ...task("succeeded"), id: "task-workspace", session_id: "session-b" };
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask] });
    mocked.getTask.mockResolvedValue({ task: sessionTask });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask, workspaceTask] });
    mocked.getTask.mockResolvedValue({ task: workspaceTask });
    mocked.listTasks.mockClear();

    store.actions.closeSession();
    await vi.waitFor(() =>
      expect(mocked.listTasks).toHaveBeenCalledWith(workspace.open_path, "", 200),
    );
    expect(readSnapshot(store).sessionId).toBeNull();
    expect(readSnapshot(store).tasks.map((item) => item.id)).toEqual([
      sessionTask.id,
      workspaceTask.id,
    ]);
  });

  it("does not keep other workspace tasks while a session inspector is syncing", async () => {
    const sessionTask = task("succeeded");
    const leftover = { ...task("succeeded"), id: "task-other", session_id: "session-b" };
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask] });
    mocked.getTask.mockResolvedValue({ task: sessionTask });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    const snapshot = readSnapshot(store);
    expect(snapshot.tasks.map((item) => item.id)).toEqual([sessionTask.id]);

    mocked.listSessions.mockResolvedValue({
      sessions: [
        {
          ...session,
          messages: 2,
          last_message_id: "message-b",
          latest_task_id: sessionTask.id,
          latest_task_status: "succeeded",
        },
      ],
    });
    mocked.sessionMessages.mockResolvedValue({
      messages: [
        { id: "message-a", role: "assistant", content: "Core result" },
        { id: "message-b", role: "user", content: "follow up" },
      ],
    });
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask] });

    await store.actions.syncWorkspace();

    expect(readSnapshot(store).tasks.map((item) => item.id)).toEqual([sessionTask.id]);
    expect(readSnapshot(store).tasks.map((item) => item.id)).not.toContain(leftover.id);
  });

  it("scopes ROM, notebook, and cost requests to the open session", async () => {
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("rom");
    await store.actions.openDrawer("notebook");
    await store.actions.openDrawer("cost");

    expect(mocked.getRom).toHaveBeenCalledWith(workspace.open_path, session.id);
    expect(mocked.getNotebook).toHaveBeenCalledWith(workspace.open_path, session.id);
    expect(mocked.getCost).toHaveBeenCalledWith(workspace.open_path, session.id);
  });

  it("closes the session and reloads workspace inspector when the current workspace is clicked again", async () => {
    const sessionTask = task("succeeded");
    const workspaceTask = { ...task("succeeded"), id: "task-workspace", session_id: "session-b" };
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask] });
    mocked.getTask.mockResolvedValue({ task: sessionTask });
    const store = await import("./store");
    await openSession(store);
    await store.actions.openDrawer("task");
    mocked.listTasks.mockResolvedValue({ tasks: [sessionTask, workspaceTask] });
    mocked.getTask.mockResolvedValue({ task: workspaceTask });
    mocked.listTasks.mockClear();
    mocked.selectWorkspace.mockClear();

    await store.actions.selectCatalog({
      name: workspace.project_name,
      root: workspace.root,
      project_dir: workspace.project_dir,
      kind: "workspace",
      label: workspace.label,
      last_seen: 0,
    });

    expect(mocked.selectWorkspace).not.toHaveBeenCalled();
    expect(readSnapshot(store).sessionId).toBeNull();
    await vi.waitFor(() =>
      expect(mocked.listTasks).toHaveBeenCalledWith(workspace.open_path, "", 200),
    );
  });

  it("loads globally sorted and filtered sessions through the Home-level catalog RPC", async () => {
    const globalSession = {
      ...session,
      id: "session-global",
      project_dir: "/tmp/other",
      workspace_label: "other",
      status_group: "warning",
    };
    mocked.listAllSessions.mockResolvedValue({
      sessions: [globalSession],
      next_cursor: "next-page",
      errors: [],
    });
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);

    await store.actions.setSessionScope("all");
    await store.actions.setSessionSort("completed");
    await store.actions.setSessionStatusFilter("warning");

    expect(mocked.listAllSessions).toHaveBeenLastCalledWith({
      workspace: undefined,
      channel: "",
      status: ["warning"],
      sort: "completed",
      cursor: "",
      limit: 50,
    });
    expect(readSnapshot(store).sessionResults).toEqual([globalSession]);
    expect(readSnapshot(store).sessionNextCursor).toBe("next-page");
  });

  it("ignores an in-flight append after a fresh first-page request starts", async () => {
    const first = { ...session, id: "session-first", project_dir: "/tmp/other" };
    const stale = { ...session, id: "session-stale", project_dir: "/tmp/other" };
    const refreshed = { ...session, id: "session-refreshed", project_dir: "/tmp/other" };
    mocked.listAllSessions.mockResolvedValueOnce({
      sessions: [first],
      next_cursor: "cursor-a",
      errors: [],
    });
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);
    await store.actions.setSessionScope("all");

    const staleAppend = deferred<{
      sessions: typeof stale[];
      next_cursor: string | null;
      errors: never[];
    }>();
    const freshPage = deferred<{
      sessions: typeof refreshed[];
      next_cursor: string | null;
      errors: never[];
    }>();
    mocked.listAllSessions
      .mockReturnValueOnce(staleAppend.promise)
      .mockReturnValueOnce(freshPage.promise);

    const appendPromise = store.actions.loadMoreSessionResults();
    const refreshPromise = store.actions.refreshSessionResults();
    freshPage.resolve({ sessions: [refreshed], next_cursor: "cursor-fresh", errors: [] });
    await refreshPromise;
    staleAppend.resolve({ sessions: [stale], next_cursor: "cursor-stale", errors: [] });
    await appendPromise;

    expect(readSnapshot(store).sessionResults).toEqual([refreshed]);
    expect(readSnapshot(store).sessionNextCursor).toBe("cursor-fresh");
  });

  it("does not let an older append failure replace the current page state", async () => {
    const first = { ...session, id: "session-first", project_dir: "/tmp/other" };
    const latest = { ...session, id: "session-latest", project_dir: "/tmp/other" };
    mocked.listAllSessions.mockResolvedValueOnce({
      sessions: [first],
      next_cursor: "cursor-a",
      errors: [],
    });
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);
    await store.actions.setSessionScope("all");

    const olderAppend = deferred<{
      sessions: typeof latest[];
      next_cursor: string | null;
      errors: never[];
    }>();
    const newerAppend = deferred<{
      sessions: typeof latest[];
      next_cursor: string | null;
      errors: Array<{ message: string }>;
    }>();
    mocked.listAllSessions
      .mockReturnValueOnce(olderAppend.promise)
      .mockReturnValueOnce(newerAppend.promise);

    const olderPromise = store.actions.loadMoreSessionResults();
    const newerPromise = store.actions.loadMoreSessionResults();
    newerAppend.resolve({
      sessions: [latest],
      next_cursor: "cursor-b",
      errors: [{ message: "current catalog warning" }],
    });
    await newerPromise;
    olderAppend.reject(new Error("stale append failed"));
    await olderPromise;

    expect(readSnapshot(store).sessionResults).toEqual([first, latest]);
    expect(readSnapshot(store).sessionNextCursor).toBe("cursor-b");
    expect(readSnapshot(store).sessionListError).toBe("current catalog warning");
  });

  it("deletes a same-workspace session batch and opens the newest surviving session", async () => {
    const survivor = { ...session, id: "session-b", title: "Survivor" };
    mocked.listSessions.mockResolvedValue({ sessions: [session, survivor] });
    mocked.deleteSessions.mockResolvedValue({
      deleted_session_ids: [session.id],
      deleted_task_ids: ["task-a"],
      retained_artifact_count: 2,
    });
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);
    await store.actions.openSession(session.id);
    mocked.listSessions.mockResolvedValue({ sessions: [survivor] });
    mocked.getSession.mockResolvedValue({ session: survivor });

    await store.actions.deleteSessions([session.id]);

    expect(mocked.deleteSessions).toHaveBeenCalledWith(workspace.open_path, [session.id]);
    expect(readSnapshot(store).sessionId).toBe(survivor.id);
    expect(readSnapshot(store).notice).toContain("保留 2 个产物");
  });

  it("hides several catalog workspaces without changing the selected workspace", async () => {
    const other = {
      name: "other",
      root: "/tmp/other",
      project_dir: "/tmp/other",
      kind: "path",
      label: "other",
      last_seen: 1,
    };
    mocked.listWorkspaces
      .mockResolvedValueOnce({ workspaces: [other] })
      .mockResolvedValueOnce({ workspaces: [] });
    mocked.hideWorkspaces.mockResolvedValue({
      project_dirs: [other.project_dir],
      workspaces: [],
    });
    const store = await import("./store");
    await store.actions.refreshCatalog();

    await store.actions.hideWorkspaces([other.project_dir]);

    expect(mocked.hideWorkspaces).toHaveBeenCalledWith([other.project_dir]);
    expect(readSnapshot(store).catalog).toEqual([]);
  });

  it("keeps hidden workspaces in the snapshot so the sidebar can restore them", async () => {
    const hidden = {
      name: "hidden",
      root: "/tmp/hidden",
      project_dir: "/tmp/hidden",
      kind: "path",
      label: "hidden",
      last_seen: 1,
    };
    mocked.listWorkspaces.mockResolvedValue({
      workspaces: [],
      hidden_workspaces: [hidden],
      hidden_count: 1,
    });
    const store = await import("./store");

    await store.actions.refreshCatalog();

    expect(readSnapshot(store).hiddenWorkspaces).toEqual([hidden]);
  });

  it("never asks the backend to hide the selected workspace", async () => {
    const store = await import("./store");
    await store.actions.openWorkspace(workspace.open_path);

    await store.actions.hideWorkspaces([workspace.project_dir]);

    expect(mocked.hideWorkspaces).not.toHaveBeenCalled();
    expect(readSnapshot(store).error).toContain("当前工作区不能");
  });

  it("restores hidden workspaces from the visibility RPC response", async () => {
    const hidden = {
      name: "hidden",
      root: "/tmp/hidden",
      project_dir: "/tmp/hidden",
      kind: "path",
      label: "hidden",
      last_seen: 1,
    };
    mocked.unhideWorkspaces.mockResolvedValue({
      project_dirs: [hidden.project_dir],
      workspaces: [hidden],
      hidden_workspaces: [],
      hidden_count: 0,
    });
    const store = await import("./store");

    await store.actions.unhideWorkspaces([hidden.project_dir]);

    expect(readSnapshot(store).catalog).toEqual([hidden]);
    expect(readSnapshot(store).hiddenWorkspaces).toEqual([]);
  });
});
