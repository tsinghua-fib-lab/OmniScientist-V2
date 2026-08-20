import type { ConfigDescribe, ConfigWriteResult } from "./configTypes";
import type { SkillDetail, SkillListResponse, SkillMutationResponse } from "./skillTypes";
import type {
  PersonaSnapshot,
  PersonaStartRequest,
  PersonaStartResponse,
  PersonaStatusResponse,
} from "./personaTypes";
import type {
  ChannelDescribeResponse,
  ChannelMutationResponse,
  ChannelName,
  WechatLoginResponse,
} from "./channelTypes";
import type {
  ActivityItem,
  Artifact,
  DirectoryListing,
  Session,
  SessionTimeline,
  TaskSummary,
  TaskDetail,
  Workspace,
  WorkspaceInbox,
} from "./types";

export type RpcError = { code: string; message: string };

export class ApiError extends Error {
  code: string;
  extra?: Record<string, unknown>;
  constructor(code: string, message: string, extra?: Record<string, unknown>) {
    super(message);
    this.code = code;
    this.extra = extra;
  }
}

async function rpc<T = Record<string, unknown>>(
  method: string,
  params: Record<string, unknown> = {},
  workspace?: string | null,
): Promise<T> {
  const body: Record<string, unknown> = { method, params: { ...params } };
  if (workspace) {
    (body.params as Record<string, unknown>).workspace = workspace;
  }
  const res = await fetch("/api", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Omni-Web": "1" },
    body: JSON.stringify(body),
  });
  if (res.headers.get("content-type")?.includes("text/event-stream")) {
    return res as unknown as T;
  }
  const data = (await res.json()) as { ok?: boolean; error?: RpcError } & T;
  if (!data.ok) {
    throw new ApiError(data.error?.code || "error", data.error?.message || "request failed", data.error);
  }
  return data;
}

export const api = {
  rpc,
  listDirectory(path?: string, showHidden = false) {
    return rpc<DirectoryListing>("host.listDirectory", {
      path: path || "",
      show_hidden: showHidden,
    });
  },
  listWorkspaces() {
    return rpc<{ workspaces: unknown[]; selected: Workspace | null }>("workspace.list");
  },
  openWorkspace(path: string) {
    return rpc<{ workspace: Workspace }>("workspace.open", { path });
  },
  selectWorkspace(ref: { path?: string; project_dir?: string; name?: string }) {
    return rpc<{ workspace: Workspace }>("workspace.select", ref);
  },
  listSessions(workspace: string, channel = "") {
    return rpc<{ sessions: Session[] }>("session.list", { channel }, workspace);
  },
  getSession(workspace: string, sessionId: string) {
    return rpc<{ session: Session }>("session.get", { session_id: sessionId }, workspace);
  },
  workspaceInbox(workspace: string, channel = "", sessionId = "") {
    return rpc<WorkspaceInbox>("workspace.inbox", { channel, session_id: sessionId }, workspace);
  },
  sessionMessages(workspace: string, sessionId: string) {
    return rpc<{ messages: unknown[] }>("session.messages", { session_id: sessionId }, workspace);
  },
  sessionTimeline(workspace: string, sessionId: string) {
    return rpc<SessionTimeline>("session.timeline", { session_id: sessionId }, workspace);
  },
  createSession(workspace: string, title = "") {
    return rpc<{ session: Session }>("session.create", { title }, workspace);
  },
  renameSession(workspace: string, sessionId: string, title: string) {
    return rpc<{ session: Session }>("session.rename", { session_id: sessionId, title }, workspace);
  },
  deleteSession(workspace: string, sessionId: string) {
    return rpc<{ session_id: string; deleted_task_ids: string[] }>(
      "session.delete",
      { session_id: sessionId },
      workspace,
    );
  },
  listTasks(workspace: string, sessionId = "", limit = 40) {
    return rpc<{ tasks: TaskSummary[] }>(
      "task.list",
      { session_id: sessionId, limit },
      workspace,
    );
  },
  getTask(workspace: string, taskId: string) {
    return rpc<TaskDetail>("task.get", { task_id: taskId }, workspace);
  },
  taskEvents(workspace: string, taskId: string, afterSeq = 0, limit = 200) {
    return rpc<{ events: ActivityItem[]; last_seq: number }>(
      "task.events",
      { task_id: taskId, after_seq: afterSeq, limit },
      workspace,
    );
  },
  listArtifacts(workspace: string, sessionId = "", taskId = "", limit = 40) {
    return rpc<{ artifacts: Artifact[] }>(
      "artifact.list",
      { session_id: sessionId, task_id: taskId, limit },
      workspace,
    );
  },
  getArtifact(workspace: string, id: string) {
    return rpc<{ artifact: unknown }>("artifact.get", { id }, workspace);
  },
  getRom(workspace: string) {
    return rpc<{ rom: unknown }>("rom.get", {}, workspace);
  },
  getNotebook(workspace: string) {
    return rpc<{ notebook: string; path: string }>("notebook.get", {}, workspace);
  },
  getCost(workspace: string, sessionId = "") {
    return rpc<{ cost: unknown }>("cost.get", { session_id: sessionId }, workspace);
  },
  describePersona(workspace: string) {
    return rpc<{ persona: PersonaSnapshot }>("persona.describe", {}, workspace);
  },
  startPersona(
    workspace: string,
    params: PersonaStartRequest & { session_id?: string; client_run_id?: string },
  ) {
    return rpc<PersonaStartResponse>("persona.start", params, workspace);
  },
  personaStatus(workspace: string, taskId: string) {
    return rpc<PersonaStatusResponse>("persona.status", { task_id: taskId }, workspace);
  },
  startTurn(
    workspace: string,
    params: {
      text: string;
      session_id?: string;
      interaction_mode?: string;
      file_uris?: string[];
      client_run_id?: string;
    },
  ) {
    return rpc<{
      session_id: string;
      task_id: string;
      client_run_id: string;
      channel: string;
      kind: string;
      markdown?: string;
    }>("turn.start", params, workspace);
  },
  steer(workspace: string, sessionId: string, instruction: string, taskId = "") {
    return rpc("turn.steer", { session_id: sessionId, instruction, task_id: taskId }, workspace);
  },
  cancel(workspace: string, sessionId: string, taskId = "") {
    return rpc("turn.cancel", { session_id: sessionId, task_id: taskId }, workspace);
  },
  approve(workspace: string, taskId: string) {
    return rpc("task.approve", { task_id: taskId }, workspace);
  },
  runCommand(workspace: string, sessionId: string, text: string) {
    return rpc("command.run", { session_id: sessionId, text }, workspace);
  },
  describeConfig() {
    return rpc<ConfigDescribe>("config.describe");
  },
  getConfig(key: string) {
    return rpc<{ key: string; value: unknown; secret: boolean; set: boolean }>("config.get", { key });
  },
  setConfig(key: string, value: unknown) {
    return rpc<ConfigWriteResult & { key: string; display: string; target: string; secret: boolean }>(
      "config.set",
      { key, value },
    );
  },
  unsetConfig(key: string) {
    return rpc<ConfigWriteResult & { key: string; target: string }>("config.unset", { key });
  },
  applyModel(params: { provider?: string; base_url?: string; model?: string; api_key?: string }) {
    return rpc<ConfigWriteResult>("config.applyModel", params);
  },
  applyVlm(params: {
    endpoint?: string;
    model?: string;
    api_key?: string;
    protocol?: string;
    timeout_s?: number;
    enabled?: boolean;
  }) {
    return rpc<ConfigWriteResult>("config.applyVlm", params);
  },
  applySemanticScholar(params: { api_key?: string }) {
    return rpc<ConfigWriteResult>("config.applySemanticScholar", params);
  },
  applyEmbeddings(params: {
    enabled: boolean;
    provider?: string;
    base_url?: string;
    model?: string;
    api_key?: string;
    python?: string;
    base_model?: string;
    adapter?: string;
    device?: string;
  }) {
    return rpc<ConfigWriteResult>("config.applyEmbeddings", params);
  },
  configHome(params: { path?: string; reset?: boolean } = {}) {
    return rpc<ConfigWriteResult & { active: string; source: string }>("config.home", params);
  },
  testConfig(target: "model" | "vlm" | "semantic_scholar") {
    return rpc<{ passed: boolean; target: string; detail: string }>("config.test", { target });
  },
  listSkills() {
    return rpc<SkillListResponse>("skill.list");
  },
  getSkill(skillId: string) {
    return rpc<{ skill: SkillDetail }>("skill.info", { skill_id: skillId });
  },
  addSkill(path: string) {
    return rpc<SkillMutationResponse>("skill.add", { path });
  },
  trustSkill(skillId: string) {
    return rpc<SkillMutationResponse>("skill.trust", { skill_id: skillId });
  },
  untrustSkill(skillId: string) {
    return rpc<SkillMutationResponse>("skill.untrust", { skill_id: skillId });
  },
  removeSkill(skillId: string) {
    return rpc<SkillMutationResponse>("skill.remove", { skill_id: skillId });
  },
  describeChannels() {
    return rpc<ChannelDescribeResponse>("channel.describe");
  },
  configureChannel(
    channel: Extract<ChannelName, "feishu" | "dingtalk">,
    params: { public_id: string; secret?: string },
  ) {
    return rpc<ChannelMutationResponse>("channel.configure", { channel, ...params });
  },
  enableChannel(channel: ChannelName) {
    return rpc<ChannelMutationResponse>("channel.enable", { channel });
  },
  disableChannel(channel: ChannelName) {
    return rpc<ChannelMutationResponse>("channel.disable", { channel });
  },
  reconnectChannel(channel: ChannelName) {
    return rpc<ChannelMutationResponse>("channel.reconnect", { channel });
  },
  pairChannel(channel: Extract<ChannelName, "feishu" | "dingtalk">) {
    return rpc<ChannelMutationResponse>("channel.pair", { channel });
  },
  startWechatLogin() {
    return rpc<WechatLoginResponse>("channel.wechat.start");
  },
  getWechatLogin(loginId: string) {
    return rpc<WechatLoginResponse>("channel.wechat.status", { login_id: loginId });
  },
  verifyWechatLogin(loginId: string, code: string) {
    return rpc<WechatLoginResponse>("channel.wechat.verify", { login_id: loginId, code });
  },
  cancelWechatLogin(loginId: string) {
    return rpc<WechatLoginResponse>("channel.wechat.cancel", { login_id: loginId });
  },
  async upload(workspace: string, file: File): Promise<string> {
    const form = new FormData();
    form.set("workspace", workspace);
    form.set("file", file);
    const res = await fetch("/api/attachment.upload", {
      method: "POST",
      headers: { "X-Omni-Web": "1" },
      body: form,
    });
    const data = (await res.json()) as { ok?: boolean; uri?: string; error?: RpcError };
    if (!data.ok || !data.uri) {
      throw new ApiError(data.error?.code || "error", data.error?.message || "upload failed");
    }
    return data.uri;
  },
};

export type SseHandlers = {
  onToken?: (text: string) => void;
  onPartial?: (text: string) => void;
  onActivity?: (item: ActivityItem) => void;
  onAck?: (info: Record<string, unknown>) => void;
  onPresentation?: (info: Record<string, unknown>) => void;
  onDone?: (info: Record<string, unknown>) => void;
  onWorker?: (info: Record<string, unknown>) => void;
  onError?: (message: string, code?: string) => void;
};

export async function watchTask(
  workspace: string,
  taskId: string,
  afterSeq: number,
  handlers: SseHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch("/api", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Omni-Web": "1" },
    body: JSON.stringify({
      method: "task.watch",
      params: { workspace, task_id: taskId, after_seq: afterSeq },
    }),
    signal,
  });
  const contentType = res.headers.get("content-type") || "";
  if (!res.ok || !contentType.includes("text/event-stream")) {
    let code = res.ok ? "invalid_stream" : `http_${res.status}`;
    let message = res.ok
      ? "task watch returned a non-stream response"
      : `task watch failed (${res.status})`;
    try {
      const data = (await res.json()) as { error?: RpcError; message?: string };
      code = data.error?.code || code;
      message = data.error?.message || data.message || message;
    } catch {
      // Preserve the protocol-level fallback when the response is not JSON.
    }
    throw new ApiError(code, message);
  }
  if (!res.body) {
    throw new ApiError("empty_stream", "task watch returned an empty stream");
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const event = /(?:^|\n)event: ([^\n]+)/.exec(chunk)?.[1]?.trim() || "";
      const dataLine = chunk
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("");
      let data: Record<string, unknown> = {};
      try {
        data = JSON.parse(dataLine) as Record<string, unknown>;
      } catch {
        continue;
      }
      if (event === "token") handlers.onToken?.(String(data.text || ""));
      else if (event === "partial") handlers.onPartial?.(String(data.text || ""));
      else if (event === "activity") handlers.onActivity?.(data as ActivityItem);
      else if (event === "ack") handlers.onAck?.(data);
      else if (event === "presentation") handlers.onPresentation?.(data);
      else if (event === "done") handlers.onDone?.(data);
      else if (event === "worker") handlers.onWorker?.(data);
      else if (event === "error") {
        const err = data.error as RpcError | undefined;
        handlers.onError?.(err?.message || String(data.message || "turn failed"), err?.code);
      }
    }
  }
}
