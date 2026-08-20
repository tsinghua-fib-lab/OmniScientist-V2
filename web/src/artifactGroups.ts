import type { Artifact, TaskSummary } from "./types";

export type ArtifactTaskGroup = {
  taskId: string;
  task: TaskSummary | null;
  artifacts: Artifact[];
  executions: ArtifactExecutionGroup[];
};

export type ArtifactExecutionGroup = {
  key: string;
  executionId: string;
  workflowRunId: string;
  artifacts: Artifact[];
  deliverables: Artifact[];
  supportFiles: Artifact[];
};

function timestamp(value?: string | null): number {
  const parsed = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(parsed) ? 0 : parsed;
}

function presentationRank(artifact: Artifact): number {
  switch (artifact.presentation_role) {
    case "attachment":
      return 1;
    case "support":
      return 2;
    case "process":
      return 3;
    case "primary":
    default:
      return 0;
  }
}

function isSupportFile(artifact: Artifact): boolean {
  return artifact.presentation_role === "support" || artifact.presentation_role === "process";
}

/** Group artifacts by canonical producing Task, then execution/workflow identity. */
export function groupArtifactsByTask(
  artifacts: Artifact[],
  tasks: TaskSummary[],
  focusedTaskId = "",
): ArtifactTaskGroup[] {
  const tasksById = new Map(tasks.map((task) => [task.id, task]));
  const grouped = new Map<string, Artifact[]>();

  for (const artifact of artifacts) {
    const taskId = artifact.task_id || "";
    const group = grouped.get(taskId) || [];
    group.push(artifact);
    grouped.set(taskId, group);
  }

  if (focusedTaskId && !grouped.has(focusedTaskId)) grouped.set(focusedTaskId, []);

  return [...grouped.entries()]
    .map(([taskId, rows]) => {
      const sorted = [...rows].sort(
        (left, right) => timestamp(left.created_at) - timestamp(right.created_at),
      );
      const executionRows = new Map<string, Artifact[]>();
      for (const artifact of sorted) {
        const key = artifact.subtask_id
          ? `execution:${artifact.subtask_id}`
          : artifact.workflow_run_id
            ? `workflow:${artifact.workflow_run_id}`
            : "task";
        const bucket = executionRows.get(key) || [];
        bucket.push(artifact);
        executionRows.set(key, bucket);
      }
      return {
        taskId,
        task: taskId ? tasksById.get(taskId) || null : null,
        artifacts: sorted,
        executions: [...executionRows.entries()].map(([key, executionArtifacts]) => {
          const ordered = [...executionArtifacts].sort(
            (left, right) =>
              presentationRank(left) - presentationRank(right) ||
              timestamp(left.created_at) - timestamp(right.created_at),
          );
          return {
            key,
            executionId: ordered[0]?.subtask_id || "",
            workflowRunId: ordered[0]?.workflow_run_id || "",
            artifacts: ordered,
            deliverables: ordered.filter((artifact) => !isSupportFile(artifact)),
            supportFiles: ordered.filter(isSupportFile),
          };
        }),
      };
    })
    .sort((left, right) => {
      if (!left.taskId) return 1;
      if (!right.taskId) return -1;
      const leftTime = timestamp(left.task?.created_at || left.artifacts.at(-1)?.created_at);
      const rightTime = timestamp(right.task?.created_at || right.artifacts.at(-1)?.created_at);
      return rightTime - leftTime || left.taskId.localeCompare(right.taskId);
    });
}

/** Merge an exact Task refresh without duplicating artifacts already in the session cache. */
export function mergeArtifacts(current: Artifact[], incoming: Artifact[]): Artifact[] {
  const byId = new Map(current.map((artifact) => [artifact.id, artifact]));
  for (const artifact of incoming) byId.set(artifact.id, artifact);
  return [...byId.values()];
}
