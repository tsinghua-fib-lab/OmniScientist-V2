import { describe, expect, it } from "vitest";
import { sessionCatalogError, sessionStatusGroup, sessionStatusLabel } from "./sessionManagement";
import type { Session } from "./types";

const base: Session = { id: "s", title: "", channel: "web", status: "active" };

describe("session management projection", () => {
  it("keeps attention, warning, error, cancellation, and completion distinct", () => {
    expect(sessionStatusGroup({ ...base, latest_task_status: "needs_input" })).toBe(
      "needs_attention",
    );
    expect(sessionStatusGroup({ ...base, latest_task_status: "degraded" })).toBe("warning");
    expect(sessionStatusGroup({ ...base, latest_task_status: "failed" })).toBe("error");
    expect(sessionStatusGroup({ ...base, latest_task_status: "cancelled" })).toBe("cancelled");
    expect(sessionStatusGroup({ ...base, latest_task_status: "succeeded" })).toBe("completed");
    expect(sessionStatusLabel({ ...base, status_group: "warning" })).toBe("有警告");
    expect(sessionStatusLabel({ ...base, worker: "external" })).toBe("后台执行中");
    expect(sessionStatusLabel({ ...base, worker: "lost" })).toBe("同步中断");
    expect(sessionStatusGroup({ ...base, worker: "interrupted" })).toBe("error");
  });

  it("summarizes partial catalog failures without hiding successful rows", () => {
    expect(sessionCatalogError([{ message: "alpha unavailable" }])).toBe("alpha unavailable");
    expect(sessionCatalogError([{ message: "a" }, { message: "b" }])).toBe(
      "2 个工作区暂时无法读取",
    );
  });
});
