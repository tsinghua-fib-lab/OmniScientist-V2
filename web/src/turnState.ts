import { isPersonaProtocolTurn } from "./personaTypes";
import type { ActivityItem, DraftState, Mode, Session, TimelineTurn, TurnState, WorkerState } from "./types";

const ACTIVE_TASK_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "recovering",
  "awaiting_approval",
]);

export function isActiveTask(status: string | undefined): boolean {
  return Boolean(status && ACTIVE_TASK_STATUSES.has(status));
}

export function latestFollowable(
  turns: TimelineTurn[],
  session?: Session | null,
): TimelineTurn | null {
  if (session?.latest_task_id) {
    const named = turns.find((turn) => turn.id === session.latest_task_id);
    if (named) {
      if (isPersonaProtocolTurn(named.user_input)) return null;
      return isActiveTask(named.status) ? named : null;
    }
    if (isActiveTask(session.latest_task_status)) {
      return {
        id: session.latest_task_id,
        status: session.latest_task_status,
      } as TimelineTurn;
    }
    return null;
  }
  const latest = turns[0];
  if (!latest) return null;
  if (isPersonaProtocolTurn(latest.user_input)) return null;
  return isActiveTask(latest.status) ? latest : null;
}

export function bucketKey(workspaceKey: string, sessionId: string): string {
  return `${workspaceKey}::${sessionId}`;
}

export function emptyDraft(mode: Mode = "auto"): DraftState {
  return { composer: "", mode, attachments: [] };
}

export function emptyTurn(input: {
  workspaceKey: string;
  sessionId: string;
  clientRunId?: string;
  taskId?: string;
}): TurnState {
  return {
    workspaceKey: input.workspaceKey,
    sessionId: input.sessionId,
    clientRunId: input.clientRunId || "",
    taskId: input.taskId || "",
    status: "idle",
    worker: "",
    partialText: "",
    activities: [],
    lastEventSeq: 0,
    error: "",
  };
}

export function mergeActivity(items: ActivityItem[], next: ActivityItem): ActivityItem[] {
  if (next.replace_key) {
    const index = items.findIndex((item) => item.replace_key === next.replace_key);
    if (index >= 0) {
      const copy = items.slice();
      copy[index] = next;
      return copy;
    }
  }
  const existing = items.findIndex((item) => item.seq === next.seq && item.task_id === next.task_id);
  if (existing >= 0) {
    const copy = items.slice();
    copy[existing] = next;
    return copy;
  }
  return [...items, next].sort((a, b) => a.seq - b.seq);
}

export function applyOwned<T extends { workspaceKey: string; sessionId: string; clientRunId: string }>(
  target: T,
  owner: { workspaceKey: string; sessionId: string; clientRunId: string },
): T | null {
  if (
    target.workspaceKey !== owner.workspaceKey ||
    target.sessionId !== owner.sessionId ||
    (owner.clientRunId && target.clientRunId && target.clientRunId !== owner.clientRunId)
  ) {
    return null;
  }
  return target;
}

export function applyToken(
  turn: TurnState,
  piece: string,
  owner: { workspaceKey: string; sessionId: string; clientRunId: string },
): TurnState | null {
  if (!applyOwned(turn, owner)) return null;
  return { ...turn, partialText: turn.partialText + piece, worker: "live", status: "running" };
}

export function applyPartial(
  turn: TurnState,
  text: string,
  owner: { workspaceKey: string; sessionId: string; clientRunId: string },
): TurnState | null {
  if (!applyOwned(turn, owner)) return null;
  return { ...turn, partialText: text, worker: turn.worker || "live" };
}

export function applyActivity(
  turn: TurnState,
  item: ActivityItem,
  owner: { workspaceKey: string; sessionId: string; clientRunId: string },
): TurnState | null {
  if (!applyOwned(turn, owner)) return null;
  return {
    ...turn,
    activities: mergeActivity(turn.activities, item),
    lastEventSeq: Math.max(turn.lastEventSeq, item.seq || 0),
    worker: turn.worker === "external" ? "external" : "live",
    status: turn.status === "idle" ? "running" : turn.status,
  };
}

export function finishTurn(turn: TurnState, worker: WorkerState = ""): TurnState {
  return { ...turn, status: "done", worker, partialText: "" };
}

export function sessionBusy(turn: TurnState | undefined): boolean {
  return turn?.status === "running";
}
