import { describe, expect, it } from "vitest";
import { buildSessionTranscript } from "./sessionTimeline";
import type { ChatMessage, TimelineTurn } from "./types";

const messages: ChatMessage[] = [
  {
    id: "message-user",
    role: "user",
    content: "start research",
    created_at: "2026-08-19T10:00:01Z",
  },
  {
    id: "message-assistant",
    role: "assistant",
    content: "finished",
    created_at: "2026-08-19T10:00:04Z",
  },
];

const task: TimelineTurn = {
  id: "task-a",
  session_id: "session-a",
  parent_task_id: "",
  channel: "cli",
  status: "succeeded",
  kind: "turn",
  title: "Research task",
  summary: "finished",
  error: "",
  created_at: "2026-08-19T10:00:00Z",
  finished_at: "2026-08-19T10:00:05Z",
  executions: [
    {
      id: "execution-a",
      skill_name: "scientific-writing",
      status: "succeeded",
      artifact_count: 1,
    },
  ],
};

describe("session transcript", () => {
  it("places executions between the user request and the assistant answer", () => {
    const timeline = buildSessionTranscript(messages, [task]);

    expect(timeline.map((item) => `${item.kind}:${item.id}`)).toEqual([
      "user:message-user",
      "turn:task-a",
      "assistant:message-assistant",
    ]);
    expect(timeline[1].kind === "turn" && timeline[1].task.executions?.[0]?.id).toBe("execution-a");
  });

  it("hides a task-result message that already has an execution row", () => {
    const result: ChatMessage = {
      id: "result-message",
      role: "assistant",
      content: "[Background skill execution completed] scientific-writing\nDone.",
      content_type: "task_result",
      meta: {
        kind: "task_result",
        object_id: "execution-a",
        subtask_id: "execution-a",
        task_id: "task-a",
      },
      created_at: "2026-08-19T10:00:03Z",
    };
    const timeline = buildSessionTranscript(
      [messages[0], result, messages[1]],
      [task],
    );

    expect(timeline.map((item) => item.kind)).toEqual(["user", "turn", "assistant"]);
    expect(timeline.some((item) => item.id === "result-message")).toBe(false);
    expect(timeline[1].kind === "turn" && timeline[1].task.executions?.[0]?.result_content).toBe(
      result.content,
    );
  });

  it("folds a workflow result onto the execution that shares its run id", () => {
    const workflow: ChatMessage = {
      id: "workflow-result",
      role: "assistant",
      content: "[Workflow succeeded] `wf-1`\nSlides ready.",
      content_type: "workflow_result",
      meta: {
        kind: "workflow_result",
        workflow_run_id: "wf-1",
        task_id: "task-a",
      },
      created_at: "2026-08-19T10:00:03Z",
    };
    const withWorkflow: TimelineTurn = {
      ...task,
      executions: [{ ...task.executions![0], workflow_run_id: "wf-1" }],
    };
    const timeline = buildSessionTranscript([messages[0], workflow, messages[1]], [withWorkflow]);
    expect(timeline.some((item) => item.id === "workflow-result")).toBe(false);
    expect(timeline[1].kind === "turn" && timeline[1].task.executions?.[0]?.result_content).toBe(
      workflow.content,
    );
  });

  it("binds concurrent turns by user_input instead of the first nearby user line", () => {
    const first: ChatMessage = {
      id: "user-cli",
      role: "user",
      content: "from cli",
      created_at: "2026-08-19T10:00:01Z",
    };
    const second: ChatMessage = {
      id: "user-wechat",
      role: "user",
      content: "from wechat",
      created_at: "2026-08-19T10:00:02Z",
    };
    const cli: TimelineTurn = {
      ...task,
      id: "task-cli",
      user_input: "from cli",
      created_at: "2026-08-19T10:00:00Z",
    };
    const wechat: TimelineTurn = {
      ...task,
      id: "task-wechat",
      user_input: "from wechat",
      created_at: "2026-08-19T10:00:00Z",
    };
    const timeline = buildSessionTranscript([first, second], [wechat, cli]);
    expect(timeline.map((item) => `${item.kind}:${item.id}`)).toEqual([
      "user:user-cli",
      "turn:task-cli",
      "user:user-wechat",
      "turn:task-wechat",
    ]);
  });

  it("keeps an unmatched legacy result as assistant content", () => {
    const legacy: ChatMessage = {
      id: "legacy-result",
      role: "assistant",
      content: "[Background skill execution completed] guessed-skill\nGenerated output.",
      created_at: "2026-08-19T10:00:03Z",
    };
    const timeline = buildSessionTranscript([messages[0], legacy], [task]);
    expect(timeline.map((item) => item.id)).toContain("legacy-result");
  });

  it("excludes child tasks from the transcript", () => {
    const child: TimelineTurn = {
      ...task,
      id: "task-child",
      parent_task_id: task.id,
    };
    const timeline = buildSessionTranscript(messages, [child, task]);
    expect(timeline.filter((item) => item.kind === "turn").map((item) => item.id)).toEqual([
      "task-a",
    ]);
  });
});
