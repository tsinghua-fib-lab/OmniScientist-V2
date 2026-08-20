import { describe, expect, it } from "vitest";
import { displayFileName, displayTitle, relativeTime, workerLabel } from "./format";

describe("relativeTime", () => {
  it("treats a UTC Z timestamp as just now", () => {
    const now = new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
    expect(relativeTime(now)).toBe("刚刚");
  });
});

describe("displayFileName", () => {
  it("keeps the basename and drops the parent directories", () => {
    expect(
      displayFileName(
        "/Users/antonio/work/omniscientist_v2/artifacts/deck.pptx",
        "artifact://9a18b",
      ),
    ).toBe("deck.pptx");
  });

  it("falls back to the uri when no path is present", () => {
    expect(displayFileName("", "artifact://9a18b")).toBe("artifact://9a18b");
  });
});

describe("displayTitle", () => {
  it("keeps the complete display title for contextual CSS truncation", () => {
    const longTitle = "调研隐空间干预".repeat(8);
    expect(
      displayTitle({
        id: "2c27e514xxxx",
        title: "",
        display_title: longTitle,
        channel: "web",
        status: "active",
      }),
    ).toBe(longTitle);
    expect(
      displayTitle({
        id: "abc",
        title: "",
        display_title: "",
        channel: "web",
        status: "active",
      }),
    ).toBe("新会话");
  });

  it("normalizes whitespace without discarding title content", () => {
    expect(
      displayTitle({
        id: "abc",
        title: "",
        display_title: "  完整分析\n总结这篇论文  ",
        channel: "web",
        status: "active",
      }),
    ).toBe("完整分析 总结这篇论文");
  });
});

describe("workerLabel", () => {
  const session = {
    id: "session-a",
    title: "Research",
    channel: "wechat",
    status: "active",
    latest_task_status: "running",
  };

  it("describes cross-process work without implying the WeChat connection is down", () => {
    expect(workerLabel({ ...session, worker: "external" })).toBe("后台执行中");
    expect(workerLabel({ ...session, worker: "lost" })).toBe("同步中断");
    expect(workerLabel({ ...session, worker: "external" })).not.toContain("断开");
  });
});
