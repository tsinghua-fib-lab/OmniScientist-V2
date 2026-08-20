import { describe, expect, it } from "vitest";
import {
  applyActivity,
  applyToken,
  bucketKey,
  emptyTurn,
  mergeActivity,
  sessionBusy,
} from "./turnState";
import type { ActivityItem } from "./types";

const ownerA = { workspaceKey: "ws-a", sessionId: "sess-a", clientRunId: "run-a" };
const ownerB = { workspaceKey: "ws-a", sessionId: "sess-b", clientRunId: "run-b" };

function activity(seq: number, title = "tool"): ActivityItem {
  return {
    task_id: "task-a",
    seq,
    kind: "tool",
    phase: "react.tool.start",
    status: "running",
    title,
    summary: title,
  };
}

describe("session isolation", () => {
  it("keeps tokens on the sending session", () => {
    const turnA = emptyTurn(ownerA);
    const turnB = emptyTurn(ownerB);
    const nextA = applyToken(turnA, "hello", ownerA);
    const leaked = applyToken(turnB, "hello", ownerA);
    expect(nextA?.partialText).toBe("hello");
    expect(leaked).toBeNull();
    expect(turnB.partialText).toBe("");
  });

  it("ignores a stale run id after a newer send", () => {
    const current = emptyTurn({ ...ownerA, clientRunId: "run-new" });
    const stale = applyToken(current, "old", { ...ownerA, clientRunId: "run-old" });
    expect(stale).toBeNull();
  });

  it("does not treat another session as busy", () => {
    const running = { ...emptyTurn(ownerA), status: "running" as const };
    const idle = emptyTurn(ownerB);
    expect(sessionBusy(running)).toBe(true);
    expect(sessionBusy(idle)).toBe(false);
  });
});

describe("activity merge", () => {
  it("replaces plan.summary in place", () => {
    const first = { ...activity(1, "plan"), replace_key: "plan.summary" };
    const second = { ...activity(4, "plan approved"), replace_key: "plan.summary" };
    const merged = mergeActivity(mergeActivity([], first), second);
    expect(merged).toHaveLength(1);
    expect(merged[0].title).toBe("plan approved");
  });

  it("appends distinct seqs in order", () => {
    const items = mergeActivity(mergeActivity([], activity(2)), activity(1));
    expect(items.map((item) => item.seq)).toEqual([1, 2]);
  });

  it("applies activity only to the owning turn", () => {
    const turnB = emptyTurn(ownerB);
    expect(applyActivity(turnB, activity(1), ownerA)).toBeNull();
  });

  it("keeps an external worker external while replaying durable activity", () => {
    const external = {
      ...emptyTurn(ownerA),
      worker: "external" as const,
      status: "running" as const,
    };

    const next = applyActivity(external, activity(1), ownerA);

    expect(next?.worker).toBe("external");
    expect(next?.activities).toHaveLength(1);
  });
});

describe("keys", () => {
  it("namespaces session state by workspace", () => {
    expect(bucketKey("ws-a", "sess")).not.toBe(bucketKey("ws-b", "sess"));
  });
});
