import { describe, expect, it } from "vitest";
import { groupArtifactsByTask, mergeArtifacts } from "./artifactGroups";
import type { Artifact, TaskSummary } from "./types";

const tasks: TaskSummary[] = [
  {
    id: "task-new",
    session_id: "session-a",
    parent_task_id: "",
    channel: "cli",
    status: "succeeded",
    kind: "turn",
    title: "Newest task",
    summary: "",
    error: "",
    created_at: "2026-08-19T10:00:00Z",
  },
  {
    id: "task-old",
    session_id: "session-a",
    parent_task_id: "",
    channel: "cli",
    status: "succeeded",
    kind: "turn",
    title: "Older task",
    summary: "",
    error: "",
    created_at: "2026-08-18T10:00:00Z",
  },
];

const artifacts: Artifact[] = [
  {
    id: "artifact-new-a",
    session_id: "session-a",
    task_id: "task-new",
    title: "Same title",
    kind: "report",
    uri: "artifact://artifact-new-a",
    path: "/tmp/new-a.md",
    mime: "text/markdown",
    subtask_id: "execution-a",
    workflow_run_id: "workflow-a",
    created_at: "2026-08-19T10:02:00Z",
  },
  {
    id: "artifact-old",
    session_id: "session-a",
    task_id: "task-old",
    title: "Same title",
    kind: "report",
    uri: "artifact://artifact-old",
    path: "/tmp/old.md",
    mime: "text/markdown",
    created_at: "2026-08-18T10:02:00Z",
  },
  {
    id: "artifact-new-b",
    session_id: "session-a",
    task_id: "task-new",
    title: "Figure",
    kind: "figure",
    uri: "artifact://artifact-new-b",
    path: "/tmp/new-b.png",
    mime: "image/png",
    subtask_id: "execution-b",
    workflow_run_id: "workflow-a",
    created_at: "2026-08-19T10:03:00Z",
  },
  {
    id: "artifact-orphan",
    session_id: "session-a",
    task_id: "",
    title: "Legacy file",
    kind: "file",
    uri: "artifact://artifact-orphan",
    path: "/tmp/orphan.txt",
    mime: "text/plain",
    created_at: "2026-08-17T10:00:00Z",
  },
];

describe("artifact task grouping", () => {
  it("uses canonical task ids and keeps unassigned artifacts visible", () => {
    const groups = groupArtifactsByTask(artifacts, tasks);

    expect(groups.map((group) => group.taskId)).toEqual([
      "task-new",
      "task-old",
      "",
    ]);
    expect(groups[0].artifacts.map((artifact) => artifact.id)).toEqual([
      "artifact-new-a",
      "artifact-new-b",
    ]);
    expect(groups[0].executions.map((execution) => execution.executionId)).toEqual([
      "execution-a",
      "execution-b",
    ]);
    expect(groups[1].artifacts.map((artifact) => artifact.id)).toEqual([
      "artifact-old",
    ]);
    expect(groups[2].artifacts.map((artifact) => artifact.id)).toEqual([
      "artifact-orphan",
    ]);
    expect(groups.flatMap((group) => group.artifacts)).toHaveLength(artifacts.length);
  });

  it("groups task artifacts by their canonical execution and preserves task-level output", () => {
    const taskLevel = {
      ...artifacts[0],
      id: "artifact-task-level",
      subtask_id: "",
      workflow_run_id: "",
      created_at: "2026-08-19T10:04:00Z",
    };
    const sameExecution = {
      ...artifacts[0],
      id: "artifact-new-c",
      created_at: "2026-08-19T10:05:00Z",
    };

    const [group] = groupArtifactsByTask(
      [artifacts[0], artifacts[2], taskLevel, sameExecution],
      tasks,
    );

    expect(group.executions.map((execution) => execution.key)).toEqual([
      "execution:execution-a",
      "execution:execution-b",
      "task",
    ]);
    expect(group.executions[0].artifacts.map((artifact) => artifact.id)).toEqual([
      "artifact-new-a",
      "artifact-new-c",
    ]);
    expect(group.executions[2].artifacts).toEqual([taskLevel]);
  });

  it("orders deliverables before support files inside one execution", () => {
    const base = artifacts[0];
    const rows: Artifact[] = [
      {
        ...base,
        id: "support",
        title: "Manifest",
        presentation_role: "support",
        created_at: "2026-08-19T10:01:00Z",
      },
      {
        ...base,
        id: "process",
        title: "DOT source",
        presentation_role: "process",
        created_at: "2026-08-19T10:02:00Z",
      },
      {
        ...base,
        id: "attachment",
        title: "Source bundle",
        presentation_role: "attachment",
        created_at: "2026-08-19T10:03:00Z",
      },
      {
        ...base,
        id: "primary",
        title: "Research report",
        presentation_role: "primary",
        created_at: "2026-08-19T10:04:00Z",
      },
    ];

    const [group] = groupArtifactsByTask(rows, tasks);
    const [execution] = group.executions;

    expect(execution.artifacts.map((artifact) => artifact.id)).toEqual([
      "primary",
      "attachment",
      "support",
      "process",
    ]);
    expect(execution.deliverables.map((artifact) => artifact.id)).toEqual([
      "primary",
      "attachment",
    ]);
    expect(execution.supportFiles.map((artifact) => artifact.id)).toEqual([
      "support",
      "process",
    ]);
  });

  it("keeps a focused task visible when it has no artifacts", () => {
    const groups = groupArtifactsByTask([], tasks, "task-old");

    expect(groups).toHaveLength(1);
    expect(groups[0].taskId).toBe("task-old");
    expect(groups[0].task?.title).toBe("Older task");
    expect(groups[0].artifacts).toEqual([]);
  });

  it("merges task-specific refreshes by artifact id without duplicates", () => {
    const updated = { ...artifacts[0], title: "Updated title" };
    const merged = mergeArtifacts(artifacts, [updated]);

    expect(merged).toHaveLength(artifacts.length);
    expect(merged.find((artifact) => artifact.id === updated.id)?.title).toBe(
      "Updated title",
    );
  });
});
