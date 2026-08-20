import { describe, expect, it } from "vitest";
import {
  activitiesForExecution,
  loadTaskActivityHistory,
  mergeTaskActivities,
} from "./activityHistory";
import type { ActivityItem, TaskExecution } from "./types";

function activity(seq: number, overrides: Partial<ActivityItem> = {}): ActivityItem {
  return {
    task_id: "task-a",
    seq,
    kind: "event",
    phase: "subtask.progress",
    status: "running",
    title: `activity-${seq}`,
    summary: "",
    ...overrides,
  };
}

describe("task activity history", () => {
  it("merges durable and live records without duplicating the same sequence", () => {
    const merged = mergeTaskActivities(
      [activity(1), activity(2, { title: "old" })],
      [activity(2, { title: "new" }), activity(3)],
    );

    expect(merged.map((item) => [item.seq, item.title])).toEqual([
      [1, "activity-1"],
      [2, "new"],
      [3, "activity-3"],
    ]);
  });

  it("keeps replacement summaries at their latest chronological position", () => {
    const merged = mergeTaskActivities(
      [
        activity(2, { replace_key: "plan.summary", title: "old plan" }),
        activity(3),
      ],
      [activity(4, { replace_key: "plan.summary", title: "new plan" })],
    );

    expect(merged.map((item) => [item.seq, item.title])).toEqual([
      [3, "activity-3"],
      [4, "new plan"],
    ]);
  });

  it("isolates execution activity by durable execution identity", () => {
    const execution: TaskExecution = {
      id: "execution-a",
      workflow_run_id: "workflow-1",
      workflow_step_id: "step-1",
      status: "running",
    };
    const items = [
      activity(1, { subtask_id: "execution-a" }),
      activity(2, { subtask_id: "execution-b", workflow_step_id: "step-1" }),
      activity(3),
    ];

    expect(activitiesForExecution(items, execution).map((item) => item.seq)).toEqual([1]);
  });

  it("loads every page instead of truncating long task histories", async () => {
    const first = Array.from({ length: 500 }, (_, index) => activity(index + 1));
    const calls: number[] = [];
    const result = await loadTaskActivityHistory(
      "workspace-a",
      "task-a",
      async (_workspace, _taskId, afterSeq, limit) => {
        calls.push(afterSeq);
        expect(limit).toBe(500);
        return afterSeq === 0
          ? { events: first, last_seq: 500 }
          : { events: [activity(501)], last_seq: 501 };
      },
    );

    expect(calls).toEqual([0, 500]);
    expect(result).toHaveLength(501);
    expect(result.at(-1)?.seq).toBe(501);
  });
});
