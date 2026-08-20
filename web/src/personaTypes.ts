export type ScientistPersona = {
  scientist_id: string;
  scientist_name: string;
  aliases: string[];
};

export type InvalidPersona = {
  directory: string;
  error: string;
};

export type PersonaSnapshot = {
  active: boolean;
  scientist_id: string;
  scientist_name: string;
  scanner: "project" | "home";
  writable: boolean;
  available: ScientistPersona[];
  invalid: InvalidPersona[];
  operation: PersonaOperation | null;
};

export type PersonaAction = "activate" | "switch" | "refresh" | "unload";

export type PersonaOperation = {
  task_id: string;
  status: string;
  action: PersonaAction;
  scientist_id: string;
};

export type PersonaStartRequest = {
  action: PersonaAction;
  scientist_id?: string;
  task_context?: string;
  force?: boolean;
};

export type PersonaStartResponse = {
  session_id: string;
  task_id: string;
  client_run_id: string;
  channel: string;
  kind: string;
};

export type PersonaStatusResponse = {
  task_id: string;
  task_status: string;
  skill_status?: string;
  outcome_code: string;
};

const ACTIVE_TASK_STATUSES = new Set([
  "pending",
  "queued",
  "running",
  "recovering",
  "awaiting_approval",
]);

export type PersonaOperationOutcome = "pending" | "succeeded" | "failed";

function snapshotMatchesRequest(
  snapshot: PersonaSnapshot,
  request: PersonaStartRequest,
): boolean {
  if (request.action === "unload") return !snapshot.active;
  return snapshot.active && snapshot.scientist_id === request.scientist_id;
}

function outcomeCodeMatches(
  request: PersonaStartRequest,
  resultCode: string,
): boolean {
  if (!resultCode) return true;
  if (request.action === "unload") {
    return ["unloaded", "already_inactive"].includes(resultCode);
  }
  return resultCode === "refreshed" || resultCode === "unchanged_task";
}

export function isPersonaProtocolTurn(input?: string): boolean {
  return Boolean(input?.trimStart().startsWith("$soulagent "));
}

export function personaOperationOutcome(
  taskStatus: string,
  snapshot: PersonaSnapshot,
  request: PersonaStartRequest,
  resultCode = "",
  skillStatus = "",
): PersonaOperationOutcome {
  const reached =
    snapshotMatchesRequest(snapshot, request) && outcomeCodeMatches(request, resultCode);
  if (skillStatus) {
    if (ACTIVE_TASK_STATUSES.has(skillStatus)) return "pending";
    if (["succeeded", "ok", "degraded"].includes(skillStatus)) {
      return reached ? "succeeded" : "failed";
    }
    return "failed";
  }
  if (!taskStatus || ACTIVE_TASK_STATUSES.has(taskStatus)) return "pending";
  if (taskStatus !== "succeeded") return "failed";
  return reached ? "succeeded" : "failed";
}
