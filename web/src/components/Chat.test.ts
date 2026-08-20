import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  Chat,
  fallbackTimelineIdentity,
  followModeAfterTranscriptScroll,
  scrollTranscriptToBottom,
  visibleUserMessage,
} from "./Chat";

describe("transcript follow behavior", () => {
  it("pauses after even a small user-initiated upward scroll", () => {
    expect(followModeAfterTranscriptScroll("following", 500, 498)).toBe("paused");
    expect(followModeAfterTranscriptScroll("following", 500, 501)).toBe("following");
  });

  it("scrolls only the transcript container to its latest content", () => {
    const transcript = { scrollHeight: 900, scrollTop: 120 };

    scrollTranscriptToBottom(transcript);

    expect(transcript.scrollTop).toBe(900);
  });

  it("keeps an explicit pause until the user asks to follow again", () => {
    expect(
      followModeAfterTranscriptScroll("paused", 600, 500),
    ).toBe("paused");
  });

  it("does not treat layout-driven scroll events as user intent", () => {
    expect(
      followModeAfterTranscriptScroll(
        "following",
        600,
        500,
        false,
      ),
    ).toBe("following");
  });

  it("isolates fallback activity state by workspace, session, and task", () => {
    const base = fallbackTimelineIdentity("workspace-a", "session-a", "task-a");

    expect(fallbackTimelineIdentity("workspace-b", "session-a", "task-a")).not.toBe(base);
    expect(fallbackTimelineIdentity("workspace-a", "session-b", "task-a")).not.toBe(base);
    expect(fallbackTimelineIdentity("workspace-a", "session-a", "task-b")).not.toBe(base);
  });
});

describe("persona protocol presentation", () => {
  it("keeps the normal SoulAgent turn auditable without exposing its transport JSON", () => {
    const message =
      '$soulagent {"input":"Research task: 研究长期记忆","action":"activate","scientist_id":"fengli-xu"}';
    expect(visibleUserMessage(message)).toBe("使用 Fengli Xu 学术人格：研究长期记忆");
    expect(visibleUserMessage(message, "en")).toBe(
      "Use Fengli Xu scientist persona: 研究长期记忆",
    );
    expect(
      visibleUserMessage(
        '$soulagent {"input":"think like Fengli Xu","action":"activate","scientist_id":"fengli-xu"}',
      ),
    ).toBe("使用 Fengli Xu 学术人格（当前文件夹）");
    expect(visibleUserMessage("普通科研问题")).toBe("普通科研问题");
  });
});

const snapshot = vi.hoisted(() => ({
  messages: [
    {
      id: "user-message",
      role: "user",
      content: "prepare the report",
      created_at: "2026-08-19T10:00:01Z",
    },
    {
      id: "assistant-message",
      role: "assistant",
      content: "report finished",
      created_at: "2026-08-19T10:00:04Z",
    },
  ] as Array<Record<string, unknown>>,
  sessionTurns: [] as Array<Record<string, unknown>>,
  sessionTasks: [
    {
      id: "1234567890abcdef",
      session_id: "session-a",
      parent_task_id: "",
      channel: "web",
      status: "succeeded",
      kind: "turn",
      title: "Prepare report",
      summary: "",
      error: "",
      created_at: "2026-08-19T10:00:00Z",
      finished_at: "2026-08-19T10:00:05Z",
    },
  ] as Array<Record<string, unknown>>,
  activities: [] as Array<Record<string, unknown>>,
  streaming: false,
  currentTurn: null as Record<string, unknown> | null,
}));

vi.mock("../store", () => ({
  useAppState: () => ({
    messages: snapshot.messages,
    sessionTurns: snapshot.sessionTurns,
    sessionTasks: snapshot.sessionTasks,
    streamingText: "",
    activities: snapshot.activities,
    streaming: snapshot.streaming,
    currentTurn: snapshot.currentTurn,
    workspace: { label: "research" },
    sessionId: "session-a",
    sessions: [{ id: "session-a", title: "Session", channel: "web", status: "active" }],
  }),
}));

describe("conversation Task navigation", () => {
  beforeEach(() => {
    snapshot.messages = [
      {
        id: "user-message",
        role: "user",
        content: "prepare the report",
        created_at: "2026-08-19T10:00:01Z",
      },
      {
        id: "assistant-message",
        role: "assistant",
        content: "report finished",
        created_at: "2026-08-19T10:00:04Z",
      },
    ];
    snapshot.sessionTurns = [];
    snapshot.sessionTasks = [
      {
        id: "1234567890abcdef",
        session_id: "session-a",
        parent_task_id: "",
        channel: "web",
        status: "succeeded",
        kind: "turn",
        title: "Prepare report",
        summary: "",
        error: "",
        created_at: "2026-08-19T10:00:00Z",
        finished_at: "2026-08-19T10:00:05Z",
      },
    ];
    snapshot.activities = [];
    snapshot.streaming = false;
    snapshot.currentTurn = null;
  });

  it("renders a durable Task marker after the finished turn with an artifact action", () => {
    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html.indexOf("prepare the report")).toBeLessThan(html.indexOf("Prepare report"));
    expect(html.indexOf("Prepare report")).toBeLessThan(html.indexOf("report finished"));
    expect(html).toContain("Task 12345678 · succeeded");
    expect(html).toContain("Prepare report");
    expect(html).toContain('aria-controls="workspace-inspector"');
    expect(html).toContain('aria-label="查看任务 12345678 的产物"');
    expect(html).toContain('aria-label="查看任务 12345678 的执行过程"');
    expect(html).toContain("执行完成 · 查看执行过程");
  });

  it("lists executions between the user request and the answer", () => {
    snapshot.sessionTurns = [
      {
        ...snapshot.sessionTasks[0],
        executions: [
          {
            id: "execution-abcdef1234",
            skill_name: "scientific-figure",
            status: "succeeded",
            artifact_count: 1,
            result_content: "Figure rendered to artifacts/figure.png",
          },
        ],
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html.indexOf("prepare the report")).toBeLessThan(html.indexOf("scientific-figure"));
    expect(html.indexOf("scientific-figure")).toBeLessThan(html.indexOf("report finished"));
    expect(html).toContain('data-execution-id="execution-abcdef1234"');
    expect(html).toContain("Figure rendered to artifacts/figure.png");
    expect(html).toContain('aria-label="查看 scientific-figure execution executio 的执行过程"');
    expect(html).toContain("执行过程");
  });

  it("presents execution artifacts as compact result metadata", () => {
    snapshot.sessionTurns = [
      {
        ...snapshot.sessionTasks[0],
        executions: [
          {
            id: "execution-abcdef1234",
            skill_name: "scientific-figure",
            status: "succeeded",
            artifact_count: 2,
            result_content: [
              "[Background skill execution completed] scientific-figure (task `12345678`, execution `executio`)",
              "Generated an auditable figure.",
              "",
              "Artifacts:",
              "- evidence_refs: artifact://figure",
              "- figure: manifest: /tmp/manifest.json (artifact://manifest)",
              "",
              "To continue from these artifacts, inspect the task with `/task show 12345678`.",
            ].join("\n"),
          },
        ],
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html).toContain('class="execution-result-heading"');
    expect(html).toContain("执行结果");
    expect(html).toContain("产物引用");
    expect(html).toContain("2 项");
    expect(html).toContain('class="execution-artifact-list"');
    expect(html).toContain("evidence_refs");
    expect(html).toContain("figure: manifest");
    expect(html).toContain("artifact://figure");
    expect(html).toContain("后续操作");
    expect(html).toContain("/task show 12345678");
    expect(html).not.toContain("[Background skill execution completed]");
    expect(html).not.toContain("Artifacts:");
  });

  it("keeps rich execution markdown inside the compact result scope", () => {
    snapshot.sessionTurns = [
      {
        ...snapshot.sessionTasks[0],
        executions: [
          {
            id: "execution-abcdef1234",
            skill_name: "research-pptx",
            status: "succeeded",
            result_content: [
              "## Render details",
              "",
              "| output | status |",
              "| --- | --- |",
              "| deck.pptx | ready |",
              "",
              "```text",
              "render complete",
              "```",
            ].join("\n"),
          },
        ],
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html).toContain('class="execution-result-content execution-result-content-raw"');
    expect(html).toContain("<h2>Render details</h2>");
    expect(html).toContain('class="md-table"');
    expect(html).toContain('class="md-code"');
  });

  it("keeps every execution inspectable without requiring a result message", () => {
    snapshot.sessionTurns = [
      {
        ...snapshot.sessionTasks[0],
        status: "running",
        executions: [
          {
            id: "execution-running-1234",
            skill_name: "research-pptx",
            status: "running",
          },
          {
            id: "execution-pending-5678",
            skill_name: "scientific-figure",
            status: "pending",
          },
        ],
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html).toContain('aria-label="查看 research-pptx execution executio 的执行过程"');
    expect(html).toContain('aria-label="查看 scientific-figure execution executio 的执行过程"');
    expect(html.match(/execution-process-body/g)).toHaveLength(2);
    expect(html).toContain("正在执行 · 查看执行过程");
  });

  it("nests live activity under its canonical Task marker instead of the transcript tail", () => {
    snapshot.sessionTasks = [{ ...snapshot.sessionTasks[0], status: "running", finished_at: null }];
    snapshot.streaming = true;
    snapshot.currentTurn = {
      taskId: "1234567890abcdef",
      worker: "live",
    };
    snapshot.activities = [
      {
        task_id: "1234567890abcdef",
        seq: 1,
        kind: "tool",
        phase: "start",
        status: "running",
        title: "Search papers",
        summary: "OpenAlex",
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html.indexOf("Prepare report")).toBeLessThan(html.indexOf("Search papers"));
    expect(html.indexOf("Search papers")).toBeLessThan(html.indexOf("report finished"));
    expect(html.match(/Search papers/g)).toHaveLength(1);
  });

  it("does not leak the current turn activity into historical Task blocks", () => {
    snapshot.messages = [
      ...snapshot.messages,
      {
        id: "user-message-2",
        role: "user",
        content: "second request",
        created_at: "2026-08-19T10:01:01Z",
      },
      {
        id: "assistant-message-2",
        role: "assistant",
        content: "working on it",
        created_at: "2026-08-19T10:01:04Z",
      },
    ];
    snapshot.sessionTurns = [
      { ...snapshot.sessionTasks[0], user_input: "prepare the report" },
      {
        ...snapshot.sessionTasks[0],
        id: "fedcba0987654321",
        status: "running",
        user_input: "second request",
        created_at: "2026-08-19T10:01:00Z",
      },
    ];
    snapshot.streaming = true;
    snapshot.currentTurn = { taskId: "fedcba0987654321", worker: "live" };
    snapshot.activities = [
      {
        task_id: "fedcba0987654321",
        seq: 1,
        kind: "tool",
        phase: "start",
        status: "running",
        title: "Only current activity",
        summary: "",
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html.match(/Only current activity/g)).toHaveLength(1);
  });

  it("uses durable task-result identity instead of guessing an execution from prose", () => {
    snapshot.messages = [
      {
        id: "result-message",
        role: "assistant",
        content: "[Background skill execution completed] misleading-name\nGenerated a figure.",
        content_type: "task_result",
        name: "scientific-figure",
        meta: {
          kind: "task_result",
          task_id: "task-1234567890",
          object_kind: "skill_execution",
          object_id: "execution-abcdef1234",
          subtask_id: "execution-abcdef1234",
          skill: "scientific-figure",
          status: "succeeded",
          artifacts: ["artifact://figure"],
        },
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html).toContain('data-execution-id="execution-abcdef1234"');
    expect(html).toContain("scientific-figure");
    expect(html).toContain("Execution executio");
    expect(html).toContain("Task task-123");
    expect(html).not.toContain("<strong>misleading-name</strong>");
  });

  it("labels legacy background prose as an unverified result instead of inventing identity", () => {
    snapshot.messages = [
      {
        id: "legacy-result",
        role: "assistant",
        content: "[Background skill execution completed] guessed-skill\nGenerated output.",
      },
    ];

    const html = renderToStaticMarkup(
      createElement(Chat, { onOpenTaskArtifacts: vi.fn() }),
    );

    expect(html).toContain("后台执行结果");
    expect(html).not.toContain("<strong>guessed-skill</strong>");
  });
});
