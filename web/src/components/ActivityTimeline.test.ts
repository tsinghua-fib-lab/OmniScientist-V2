import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ActivityTimeline, timelineExpansionAfterLifecycleChange } from "./ActivityTimeline";

describe("ActivityTimeline", () => {
  it("follows lifecycle transitions without overwriting a manual toggle in the same phase", () => {
    expect(timelineExpansionAfterLifecycleChange(true, true, false)).toBe(false);
    expect(timelineExpansionAfterLifecycleChange(false, false, true)).toBe(true);
    expect(timelineExpansionAfterLifecycleChange(false, true, true)).toBe(false);
    expect(timelineExpansionAfterLifecycleChange(true, false, false)).toBe(true);
  });

  it("counts durable activity records rather than calling heterogeneous events steps", () => {
    const html = renderToStaticMarkup(
      createElement(ActivityTimeline, {
        items: [
          {
            task_id: "task-a",
            seq: 1,
            kind: "tool",
            phase: "start",
            status: "running",
            title: "Search papers",
            summary: "OpenAlex",
          },
        ],
        streaming: true,
        worker: "external",
      }),
    );

    expect(html).toContain("后台执行中");
    expect(html).toContain("1 条活动");
    expect(html).not.toContain("1 步");
    expect(html).toContain('<details class="tool-activity" open="">');
  });

  it("starts completed task activity collapsed", () => {
    const html = renderToStaticMarkup(
      createElement(ActivityTimeline, {
        items: [],
        streaming: false,
        status: "succeeded",
        alwaysVisible: true,
      }),
    );

    expect(html).toContain('<details class="tool-activity">');
    expect(html).not.toContain('<details class="tool-activity" open="">');
  });

  it("describes a broken watch as a sync interruption, not a dead worker", () => {
    const html = renderToStaticMarkup(
      createElement(ActivityTimeline, {
        items: [
          {
            task_id: "task-a",
            seq: 1,
            kind: "tool",
            phase: "done",
            status: "succeeded",
            title: "Search papers",
            summary: "OpenAlex",
          },
        ],
        streaming: false,
        worker: "lost",
      }),
    );

    expect(html).toContain("同步中断，以下是已落盘的过程");
    expect(html).not.toContain("执行进程已断开");
  });
});
