import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Artifact, Drawer, TaskSummary } from "../types";
import { Drawers } from "./Drawers";

type TestSnapshot = {
  workspace: { project_dir: string };
  sessionId: string;
  drawer: Exclude<Drawer, "none">;
  artifacts: Artifact[];
  artifactTaskId: string;
  artifactListLoading: boolean;
  artifactListError: string;
  artifactDetail: Artifact | null;
  artifactLoading: boolean;
  artifactError: string;
  tasks: TaskSummary[];
  sessionTasks: TaskSummary[];
  taskSelectedId: string;
  taskDetail: Record<string, unknown> | null;
  taskDetailLoading: boolean;
  taskDetailError: string;
  rom: Record<string, unknown> | null;
  notebook: string;
  cost: Record<string, unknown> | null;
};

const snapshot = vi.hoisted(() => ({
  workspace: { project_dir: "/tmp/project" },
  sessionId: "session-a",
  drawer: "artifact",
  artifacts: [
    {
      id: "selected",
      session_id: "session-a",
      task_id: "task-a",
      title: "Selected artifact",
      kind: "document",
      uri: "artifact://selected",
      path: "/tmp/selected.md",
      mime: "text/markdown",
    },
    {
      id: "following",
      session_id: "session-a",
      task_id: "task-a",
      title: "Following artifact",
      kind: "document",
      uri: "artifact://following",
      path: "/tmp/following.md",
      mime: "text/markdown",
    },
    {
      id: "legacy",
      session_id: "session-a",
      task_id: "",
      title: "Legacy artifact",
      kind: "document",
      uri: "artifact://legacy",
      path: "/tmp/legacy.md",
      mime: "text/markdown",
    },
  ],
  artifactTaskId: "",
  artifactListLoading: false,
  artifactListError: "",
  artifactDetail: {
    id: "selected",
    session_id: "session-a",
    task_id: "task-a",
    title: "Selected artifact",
    kind: "document",
    uri: "artifact://selected",
    path: "/tmp/selected.md",
    mime: "text/markdown",
    preview: "## Inline detail heading",
  },
  artifactLoading: false,
  artifactError: "",
  tasks: [],
  sessionTasks: [
    {
      id: "task-a",
      session_id: "session-a",
      parent_task_id: "",
      channel: "cli",
      status: "succeeded",
      kind: "turn",
      title: "Research task",
      summary: "",
      error: "",
      created_at: "2026-08-19T10:00:00Z",
    },
  ],
  taskSelectedId: "",
  taskDetail: null,
  taskDetailLoading: false,
  taskDetailError: "",
  rom: null,
  notebook: "",
  cost: null,
}) as TestSnapshot);

vi.mock("../store", () => ({
  actions: {
    openDrawer: vi.fn(),
    showArtifact: vi.fn(),
    showTask: vi.fn(),
    showAllArtifacts: vi.fn(),
  },
  useAppState: () => snapshot,
}));

describe("artifact drawer", () => {
  beforeEach(() => {
    snapshot.drawer = "artifact";
    snapshot.artifactDetail = {
      id: "selected",
      session_id: "session-a",
      task_id: "task-a",
      title: "Selected artifact",
      kind: "document",
      uri: "artifact://selected",
      path: "/tmp/selected.md",
      mime: "text/markdown",
      preview: "## Inline detail heading",
    };
    snapshot.artifactLoading = false;
    snapshot.artifactError = "";
    snapshot.artifactTaskId = "";
    snapshot.artifactListLoading = false;
    snapshot.artifactListError = "";
    snapshot.sessionId = "session-a";
    snapshot.taskSelectedId = "";
    snapshot.taskDetail = null;
    snapshot.taskDetailLoading = false;
    snapshot.taskDetailError = "";
    delete snapshot.artifacts[0].subtask_id;
    delete snapshot.artifacts[0].workflow_run_id;
    delete snapshot.artifacts[0].presentation_role;
    delete snapshot.artifacts[1].subtask_id;
    delete snapshot.artifacts[1].workflow_run_id;
    delete snapshot.artifacts[1].presentation_role;
  });

  it("renders the selected artifact detail before the following artifact", () => {
    const html = renderToStaticMarkup(createElement(Drawers));
    const selected = html.indexOf("Selected artifact");
    const detail = html.indexOf("Inline detail heading");
    const following = html.indexOf("Following artifact");

    expect(selected).toBeGreaterThanOrEqual(0);
    expect(detail).toBeGreaterThan(selected);
    expect(following).toBeGreaterThan(detail);
    expect(html.match(/role="region"/g)).toHaveLength(1);
    expect(html).toContain('aria-expanded="true"');
  });

  it("renders loading feedback in the selected artifact position", () => {
    snapshot.artifactDetail = snapshot.artifacts[0];
    snapshot.artifactLoading = true;

    const html = renderToStaticMarkup(createElement(Drawers));
    const selected = html.indexOf("Selected artifact");
    const loading = html.indexOf("正在加载产物内容");
    const following = html.indexOf("Following artifact");

    expect(loading).toBeGreaterThan(selected);
    expect(following).toBeGreaterThan(loading);
    expect(html).toContain('aria-busy="true"');
  });

  it("does not render an orphan detail that is absent from the list", () => {
    snapshot.artifactDetail = {
      id: "missing",
      session_id: "session-a",
      task_id: "task-a",
      title: "Missing artifact",
      kind: "document",
      uri: "artifact://missing",
      path: "/tmp/missing.md",
      mime: "text/markdown",
      preview: "Orphan detail",
    };

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).not.toContain("Orphan detail");
    expect(html).not.toContain('role="region"');
  });

  it("exposes a reversible fullscreen inspector state", () => {
    const html = renderToStaticMarkup(createElement(Drawers, { fullscreen: true }));

    expect(html).toContain("panel is-fullscreen");
    expect(html).toContain('aria-label="恢复检查器"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).toContain('aria-label="收起检查器"');
  });

  it("keeps all inspector destinations available while artifact content is open", () => {
    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html.match(/aria-controls="workspace-inspector"/g)).toHaveLength(5);
    for (const tab of ["task", "artifact", "rom", "notebook", "cost"]) {
      expect(html).toContain(`data-inspector-tab="${tab}"`);
    }
    expect(html).toContain("当前产物");
    expect(html).toContain("打开任务");
    expect(html).toContain("打开ROM");
  });

  it("nests artifacts under canonical Task groups and keeps legacy artifacts", () => {
    const html = renderToStaticMarkup(createElement(Drawers));
    const task = html.indexOf("Research task");
    const selected = html.indexOf("Selected artifact");
    const legacyGroup = html.indexOf("历史 / 未归属产物");

    expect(task).toBeGreaterThanOrEqual(0);
    expect(selected).toBeGreaterThan(task);
    expect(legacyGroup).toBeGreaterThan(selected);
    expect(html).toContain('data-task-id="task-a"');
  });

  it("separates the artifact scope navigation from the focused Task group", () => {
    snapshot.artifactTaskId = "task-a";

    const html = renderToStaticMarkup(createElement(Drawers));
    const scopeRegion = html.indexOf('class="artifact-scope"');
    const returnControl = html.indexOf("返回当前会话产物");
    const scopedGroups = html.indexOf('class="artifact-groups task-scoped"');
    const taskGroup = html.indexOf('data-task-id="task-a"');

    expect(scopeRegion).toBeGreaterThanOrEqual(0);
    expect(returnControl).toBeGreaterThan(scopeRegion);
    expect(scopedGroups).toBeGreaterThan(returnControl);
    expect(taskGroup).toBeGreaterThan(scopedGroups);

    snapshot.artifactTaskId = "";
    const allArtifactsHtml = renderToStaticMarkup(createElement(Drawers));
    expect(allArtifactsHtml).not.toContain("artifact-groups task-scoped");
  });

  it("returns to workspace artifacts when no session is selected", () => {
    snapshot.sessionId = "";
    snapshot.artifactTaskId = "task-a";

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("当前工作区");
    expect(html).toContain("返回当前工作区产物");
    expect(html).not.toContain("返回当前会话产物");
  });

  it("shows producing executions inside a Task group", () => {
    snapshot.artifacts[0].subtask_id = "execution-abcdefgh";
    snapshot.artifacts[0].workflow_run_id = "workflow-abcdefgh";

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("技能执行 executio");
    expect(html).toContain("工作流 workflow");
    delete snapshot.artifacts[0].subtask_id;
    delete snapshot.artifacts[0].workflow_run_id;
  });

  it("keeps support files collapsed beneath deliverables by default", () => {
    snapshot.artifacts[0].subtask_id = "execution-abcdefgh";
    snapshot.artifacts[0].presentation_role = "primary";
    snapshot.artifacts[1].subtask_id = "execution-abcdefgh";
    snapshot.artifacts[1].presentation_role = "support";
    snapshot.artifactDetail = snapshot.artifacts[0];

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("交付物");
    expect(html).toContain("Selected artifact");
    expect(html).toContain("支持文件");
    expect(html).toContain("1 个");
    expect(html).not.toContain("Following artifact");
    expect(html).toContain('aria-expanded="false"');
  });

  it("opens a selected support file inline without moving it to the drawer tail", () => {
    snapshot.artifacts[0].subtask_id = "execution-abcdefgh";
    snapshot.artifacts[0].presentation_role = "primary";
    snapshot.artifacts[1].subtask_id = "execution-abcdefgh";
    snapshot.artifacts[1].presentation_role = "support";
    snapshot.artifactDetail = {
      ...snapshot.artifacts[1],
      preview: "## Support details",
    };

    const html = renderToStaticMarkup(createElement(Drawers));
    const support = html.indexOf("Following artifact");
    const detail = html.indexOf("Support details");

    expect(html).toContain('aria-label="支持文件"');
    expect(html).toContain('aria-expanded="true"');
    expect(support).toBeGreaterThanOrEqual(0);
    expect(detail).toBeGreaterThan(support);
    expect(html.match(/role="region"/g)).toHaveLength(1);
  });
});

describe("task drawer", () => {
  beforeEach(() => {
    snapshot.drawer = "task";
    snapshot.sessionId = "session-a";
    snapshot.tasks = [snapshot.sessionTasks[0]];
    snapshot.taskSelectedId = snapshot.sessionTasks[0].id;
    snapshot.taskDetailLoading = false;
    snapshot.taskDetailError = "";
    snapshot.taskDetail = {
      task: snapshot.sessionTasks[0],
      workflows: [
        { id: "workflow-123456789", status: "succeeded", title: "Publication workflow" },
      ],
      steps: [
        {
          id: "step-123456789",
          workflow_run_id: "workflow-123456789",
          name: "draft",
          status: "succeeded",
          position: 1,
          current_execution_id: "execution-retry-2",
          execution_ids: ["execution-retry-1", "execution-retry-2"],
        },
      ],
      executions: [
        {
          id: "execution-direct",
          skill_name: "research-pptx",
          status: "succeeded",
          workflow_run_id: "",
          workflow_step_id: "",
          step_attempt: 1,
          summary: "Deck ready",
        },
        {
          id: "execution-retry-1",
          skill_name: "scientific-writing",
          status: "failed",
          workflow_run_id: "workflow-123456789",
          workflow_step_id: "step-123456789",
          step_attempt: 1,
          error: "provider timeout",
        },
        {
          id: "execution-retry-2",
          skill_name: "scientific-writing",
          status: "succeeded",
          workflow_run_id: "workflow-123456789",
          workflow_step_id: "step-123456789",
          step_attempt: 2,
          summary: "Draft ready",
        },
      ],
      direct_executions: [
        {
          id: "execution-direct",
          skill_name: "research-pptx",
          status: "succeeded",
          step_attempt: 1,
        },
      ],
      children: [
        {
          id: "child-123456789",
          status: "succeeded",
          kind: "child",
          title: "Validate citations",
        },
      ],
      events: [
        {
          id: "event-1",
          event_type: "workflow.completed",
          status: "succeeded",
          name: "publication",
          summary: "All deliverables ready",
          created_at: "2026-08-19T10:10:00Z",
        },
      ],
    };
  });

  it("labels the task list as session-scoped when a session is selected", () => {
    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("当前会话 · 1 个");
    expect(html).not.toContain("当前工作区");
  });

  it("labels the task list as workspace-scoped when no session is selected", () => {
    snapshot.sessionId = "";
    snapshot.tasks = [
      snapshot.sessionTasks[0],
      { ...snapshot.sessionTasks[0], id: "task-b", session_id: "session-b" },
    ];

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("当前工作区 · 2 个");
    expect(html).not.toContain("当前会话");
  });

  it("uses the nested task id for active selection and renders the CLI object hierarchy", () => {
    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain('aria-current="true"');
    const workflows = html.indexOf("工作流");
    const steps = html.indexOf("工作流步骤");
    const executions = html.indexOf("技能执行");
    const children = html.indexOf("子任务");
    const activity = html.indexOf("最近活动");
    expect(workflows).toBeGreaterThanOrEqual(0);
    expect(steps).toBeGreaterThan(workflows);
    expect(executions).toBeGreaterThan(steps);
    expect(children).toBeGreaterThan(executions);
    expect(activity).toBeGreaterThan(children);
  });

  it("renders the selected Task detail inline before the following Task", () => {
    snapshot.tasks = [
      snapshot.sessionTasks[0],
      {
        ...snapshot.sessionTasks[0],
        id: "task-b",
        title: "Following task",
      },
    ];

    const html = renderToStaticMarkup(createElement(Drawers));
    const selected = html.indexOf("Research task");
    const detail = html.indexOf("Publication workflow");
    const following = html.indexOf("Following task");

    expect(selected).toBeGreaterThanOrEqual(0);
    expect(detail).toBeGreaterThan(selected);
    expect(following).toBeGreaterThan(detail);
    expect(html.match(/class="task-entry expanded"/g)).toHaveLength(1);
    expect(html.match(/role="region"/g)).toHaveLength(1);
    expect(html).toContain('aria-expanded="true"');
    expect(html).toContain('class="panel-detail task-detail-inline"');
  });

  it("keeps the selected Task row open while its detail is loading", () => {
    snapshot.taskDetail = null;
    snapshot.taskDetailLoading = true;

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("正在加载任务详情");
    expect(html).toContain('aria-busy="true"');
    expect(html).toContain('class="task-entry expanded"');
  });

  it("does not render a stale Task detail that is absent from the current list", () => {
    snapshot.taskSelectedId = "missing-task";
    snapshot.taskDetail = {
      ...(snapshot.taskDetail || {}),
      task: { ...snapshot.sessionTasks[0], id: "missing-task", title: "Missing task" },
    };

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).not.toContain("Missing task");
    expect(html).not.toContain("Publication workflow");
    expect(html).not.toContain('class="task-entry expanded"');
    expect(html).not.toContain('role="region"');
  });

  it("shows direct and workflow execution attempts once with stable identities", () => {
    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html.match(/data-execution-id="execution-direct"/g)).toHaveLength(1);
    expect(html.match(/Execution execution-direct/g)).toHaveLength(1);
    expect(html).toContain("execution-retry-1");
    expect(html).toContain("execution-retry-2");
    expect(html).toContain("第 1 次尝试");
    expect(html).toContain("第 2 次尝试");
    expect(html).toContain("provider timeout");
    expect(html).toContain("Draft ready");
  });
});

describe("structured inspector views", () => {
  beforeEach(() => {
    snapshot.sessionId = "session-a";
    snapshot.taskSelectedId = "";
    snapshot.taskDetail = null;
    snapshot.taskDetailLoading = false;
    snapshot.taskDetailError = "";
    snapshot.rom = {
      scope: "session",
      session_id: "session-a",
      counts: {
        sources: 45,
        chunks: 2,
        citations: 3,
        hypotheses: 1,
        claims: 1,
        evidence: 4,
        runs: 1,
      },
      hypotheses: [
        {
          id: "hypothesis-123456789",
          statement: "Retrieval improves factual grounding",
          status: "active",
          confidence: 0.82,
          updated_at: "2026-08-19T10:00:00Z",
        },
      ],
      claims: [
        {
          id: "claim-123456789",
          text: "Reranking improves evidence relevance",
          polarity: "assert",
          confidence: 0.84,
          hypothesis_id: "hypothesis-123456789",
        },
      ],
      sources: [
        {
          id: "source-123456789",
          title: "Attention Is All You Need",
          arxiv_id: "1706.03762",
          year: 2017,
          venue: "NeurIPS",
        },
      ],
      runs: [{ id: "run-123456789", title: "RAG review", status: "succeeded" }],
    };
    snapshot.notebook = "# Lab Notebook\n\n## Finding\n\n- Evidence is indexed.";
    snapshot.cost = {
      scope: "session",
      session_id: "session-a",
      prompt_tokens: 1200,
      completion_tokens: 300,
      total_tokens: 1500,
      cost_usd: 0.012345,
      calls: 3,
      estimated_calls: 1,
      tasks: [
        {
          task_id: "task-a",
          task_ids: ["task-a", "task-child"],
          prompt_tokens: 1200,
          completion_tokens: 300,
          total_tokens: 1500,
          cost_usd: 0.012345,
          calls: 3,
          estimated_calls: 1,
          components: {
            planner: {
              calls: 1,
              prompt_tokens: 400,
              completion_tokens: 100,
              total_tokens: 500,
              cost_usd: 0.004,
            },
          },
        },
      ],
    };
  });

  it("renders ROM as a readable summary with a raw JSON entry point", () => {
    snapshot.drawer = "rom";

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("研究记忆概览");
    expect(html).toContain("Retrieval improves factual grounding");
    expect(html).toContain("Reranking improves evidence relevance");
    expect(html).toContain("Attention Is All You Need");
    expect(html).toContain("RAG review");
    expect(html).toContain("显示 1 / 共 45");
    expect(html).toContain("原始 JSON");
    expect(html).toContain('aria-label="ROM 展示方式"');
    expect(html).toContain('aria-pressed="true"');
    expect(html).not.toContain('class="inspector-raw"');
  });

  it("renders cost totals and Task component breakdowns with raw JSON available", () => {
    snapshot.drawer = "cost";

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("费用概览");
    expect(html).toContain("$0.012345");
    expect(html).toContain("1,500");
    expect(html).toContain("Task task-a");
    expect(html).toContain("planner");
    expect(html).toContain("原始 JSON");
    expect(html).toContain('aria-label="费用展示方式"');
  });

  it("renders notebook Markdown by default and retains a source view", () => {
    snapshot.drawer = "notebook";

    const html = renderToStaticMarkup(createElement(Drawers));

    expect(html).toContain("Lab Notebook");
    expect(html).toContain("Finding");
    expect(html).toContain("Evidence is indexed.");
    expect(html).toContain("原始内容");
    expect(html).toContain('aria-label="笔记展示方式"');
    expect(html).not.toContain('class="inspector-raw"');
  });
});
