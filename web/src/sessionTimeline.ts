import type { ChatMessage, TaskExecution, TaskSummary, TimelineTurn } from "./types";

export type TranscriptItem =
  | { kind: "user"; id: string; message: ChatMessage }
  | { kind: "assistant"; id: string; message: ChatMessage }
  | { kind: "turn"; id: string; task: TimelineTurn };

function timestamp(value: string | null | undefined): number {
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(parsed) ? Number.MAX_SAFE_INTEGER : parsed;
}

function normalizeText(value: string): string {
  return value.trim();
}

function taskAnchor(task: TaskSummary, messages: ChatMessage[]): string | null {
  const created = timestamp(task.created_at || null);
  if (created === Number.MAX_SAFE_INTEGER) return task.created_at || null;
  const followingUser = messages.find((message) => {
    if (message.role !== "user") return false;
    const at = timestamp(message.created_at || null);
    return at >= created && at - created <= 5 * 60_000;
  });
  return followingUser?.created_at || task.created_at || null;
}

export function isResultMessage(message: ChatMessage): boolean {
  const meta = message.meta || {};
  const kind = String(meta.kind || message.content_type || "");
  return (
    kind === "task_result" ||
    kind === "workflow_result" ||
    /^\[Background skill execution completed\]/i.test(message.content)
  );
}

export function resultMatchIds(message: ChatMessage): string[] {
  const meta = message.meta || {};
  return [meta.object_id, meta.subtask_id, meta.workflow_run_id].filter(
    (value): value is string => typeof value === "string" && Boolean(value),
  );
}

/** @deprecated Use resultMatchIds. */
export function resultExecutionId(message: ChatMessage): string {
  return resultMatchIds(message)[0] || "";
}

function executionKeys(execution: TaskExecution): string[] {
  return [execution.id, execution.workflow_run_id].filter(
    (value): value is string => Boolean(value),
  );
}

function cloneTurns(turns: TimelineTurn[]): TimelineTurn[] {
  return turns
    .filter((turn) => (turn.kind || "turn") === "turn" && !turn.parent_task_id)
    .map((turn) => ({
      ...turn,
      executions: (turn.executions || []).map((execution) => ({ ...execution })),
    }))
    .sort((left, right) => timestamp(left.created_at) - timestamp(right.created_at));
}

function attachResult(turns: TimelineTurn[], message: ChatMessage): boolean {
  const ids = new Set(resultMatchIds(message));
  if (!ids.size) return false;
  for (const turn of turns) {
    for (const execution of turn.executions || []) {
      if (!executionKeys(execution).some((id) => ids.has(id))) continue;
      if (!execution.result_content) execution.result_content = message.content;
      return true;
    }
  }
  return false;
}

function takeTurnForUser(
  message: ChatMessage,
  remaining: TimelineTurn[],
  messages: ChatMessage[],
): TimelineTurn | null {
  const text = normalizeText(message.content);
  if (text) {
    const byInput = remaining.findIndex(
      (turn) => normalizeText(turn.user_input || "") === text,
    );
    if (byInput >= 0) return remaining.splice(byInput, 1)[0] || null;
  }
  const index = remaining.findIndex((turn) => {
    if (normalizeText(turn.user_input || "")) return false;
    return taskAnchor(turn, messages) === (message.created_at || null);
  });
  if (index < 0) return null;
  return remaining.splice(index, 1)[0] || null;
}

/** One turn = user → executions → answer. Matched results fold into executions. */
export function buildSessionTranscript(
  messages: ChatMessage[],
  turns: TimelineTurn[],
): TranscriptItem[] {
  const visible = messages.filter((message) => message.role === "user" || message.role === "assistant");
  const remaining = cloneTurns(turns);
  const items: TranscriptItem[] = [];

  for (const message of visible) {
    if (message.role === "user") {
      items.push({ kind: "user", id: message.id, message });
      const turn = takeTurnForUser(message, remaining, visible);
      if (turn) items.push({ kind: "turn", id: turn.id, task: turn });
      continue;
    }
    if (isResultMessage(message) && attachResult(remaining, message)) {
      continue;
    }
    if (isResultMessage(message)) {
      const attached = items.some(
        (item) => item.kind === "turn" && attachResult([item.task], message),
      );
      if (attached) continue;
    }
    items.push({ kind: "assistant", id: message.id, message });
  }

  for (const turn of remaining) {
    const at = timestamp(turn.created_at);
    const index = items.findIndex((item) => {
      const itemAt =
        item.kind === "turn" ? timestamp(item.task.created_at) : timestamp(item.message.created_at);
      return itemAt > at;
    });
    const entry: TranscriptItem = { kind: "turn", id: turn.id, task: turn };
    if (index < 0) items.push(entry);
    else items.splice(index, 0, entry);
  }

  return items;
}

/** @deprecated Use buildSessionTranscript. Kept for existing tests during the cutover. */
export function mergeSessionTimeline(
  messages: ChatMessage[],
  tasks: TaskSummary[],
): Array<{ kind: "message" | "task"; id: string; at: string | null; message?: ChatMessage; task?: TaskSummary }> {
  const items = buildSessionTranscript(messages, tasks);
  return items.map((item) =>
    item.kind === "turn"
      ? { kind: "task" as const, id: item.id, at: item.task.created_at || null, task: item.task }
      : { kind: "message" as const, id: item.id, at: item.message.created_at || null, message: item.message },
  );
}
