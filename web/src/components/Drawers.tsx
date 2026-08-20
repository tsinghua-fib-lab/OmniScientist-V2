import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { groupArtifactsByTask, type ArtifactTaskGroup } from "../artifactGroups";
import { asRecord, displayFileName, prettyValue, shortId } from "../format";
import { trapFocus } from "../focus";
import {
  IconArtifact,
  IconChevronDown,
  IconClose,
  IconCost,
  IconMaximize,
  IconMinimize,
  IconNote,
  IconPanelRightClose,
  IconRom,
  IconTask,
} from "../icons";
import { MarkdownBody } from "../markdown";
import { actions, useAppState } from "../store";
import { CostView, NotebookView, RomView } from "./InspectorDataViews";
import type {
  Artifact,
  Drawer,
  TaskDetail,
  TaskEvent,
  TaskExecution,
  TaskStep,
  TaskSummary,
  TaskWorkflow,
} from "../types";

const TABS: { id: Exclude<Drawer, "none">; label: string }[] = [
  { id: "task", label: "任务" },
  { id: "artifact", label: "产物" },
  { id: "rom", label: "ROM" },
  { id: "notebook", label: "笔记" },
  { id: "cost", label: "费用" },
];

function inspectorScopeLabel(sessionId: string | null, count?: number) {
  const scope = sessionId ? "当前会话" : "当前工作区";
  return count ? `${scope} · ${count} 个` : scope;
}

const TAB_ICONS = {
  task: IconTask,
  artifact: IconArtifact,
  rom: IconRom,
  notebook: IconNote,
  cost: IconCost,
} as const;

type InspectorTabsProps = {
  active?: Drawer;
  className?: string;
  onSelect: (drawer: Exclude<Drawer, "none">, trigger: HTMLButtonElement) => void;
};

export function InspectorTabs({ active = "none", className = "", onSelect }: InspectorTabsProps) {
  return (
    <nav className={`inspector-tabs ${className}`.trim()} aria-label="工作区详情">
      {TABS.map((tab) => {
        const Icon = TAB_ICONS[tab.id];
        const selected = active === tab.id;
        return (
          <button
            key={tab.id}
            type="button"
            data-inspector-tab={tab.id}
            className="icon-btn"
            aria-pressed={selected}
            aria-controls="workspace-inspector"
            aria-label={`${selected ? "当前" : "打开"}${tab.label}`}
            title={tab.label}
            onClick={(event) => onSelect(tab.id, event.currentTarget)}
          >
            <Icon size={16} />
            <span>{tab.label}</span>
          </button>
        );
      })}
    </nav>
  );
}

function TaskRow({
  task,
  active,
  controlsId,
  triggerId,
}: {
  task: TaskSummary;
  active: boolean;
  controlsId: string;
  triggerId: string;
}) {
  return (
    <button
      id={triggerId}
      type="button"
      className={`item panel-item${active ? " active" : ""}`}
      aria-current={active ? "true" : undefined}
      aria-expanded={active}
      aria-controls={active ? controlsId : undefined}
      onClick={() => void actions.showTask(task.id)}
    >
      <span className="item-leading" aria-hidden="true">
        <IconTask size={16} />
      </span>
      <span className="item-copy">
        <span className="title">{task.title || task.kind || task.id.slice(0, 8)}</span>
        <span className="meta">
          <span className={`status ${task.status}`}>{task.status}</span>
          {task.summary ? <span className="panel-item-sub">{task.summary}</span> : null}
        </span>
      </span>
      <span className="panel-disclosure" aria-hidden="true">
        <IconChevronDown size={15} />
      </span>
    </button>
  );
}

function ArtifactRow({
  artifact,
  active,
  controlsId,
  triggerId,
}: {
  artifact: Artifact;
  active: boolean;
  controlsId: string;
  triggerId: string;
}) {
  const fileName = displayFileName(artifact.path, artifact.uri);
  const title = artifact.title || artifact.kind || fileName;
  return (
    <button
      id={triggerId}
      type="button"
      className={`item panel-item${active ? " active" : ""}`}
      aria-expanded={active}
      aria-controls={active ? controlsId : undefined}
      onClick={() => void actions.showArtifact(artifact.id)}
    >
      <span className="item-leading" aria-hidden="true">
        <IconArtifact size={16} />
      </span>
      <span className="item-copy">
        <span className="title" title={title}>
          {title}
        </span>
        <span className="meta">
          {artifact.kind ? <span className="badge">{artifact.kind}</span> : null}
          {fileName ? <span className="panel-item-sub panel-item-path">{fileName}</span> : null}
        </span>
      </span>
      <span className="panel-disclosure" aria-hidden="true">
        <IconChevronDown size={15} />
      </span>
    </button>
  );
}

function ArtifactInlineDetail({
  artifact,
  controlsId,
  triggerId,
  loading,
  error,
}: {
  artifact: Artifact;
  controlsId: string;
  triggerId: string;
  loading: boolean;
  error: string;
}) {
  const location = artifact.path || artifact.uri;
  return (
    <section
      id={controlsId}
      className="panel-detail artifact-detail-inline"
      role="region"
      aria-labelledby={triggerId}
      aria-busy={loading || undefined}
    >
      {loading && (
        <div className="artifact-detail-loading" role="status">
          <span className="artifact-loading-line wide" aria-hidden="true" />
          <span className="artifact-loading-line" aria-hidden="true" />
          <span className="sr-only">正在加载产物内容</span>
        </div>
      )}
      {!loading && error && (
        <div className="artifact-detail-error" role="alert">
          <strong>无法加载产物内容</strong>
          <span>{error}</span>
        </div>
      )}
      {!loading && !error && artifact.preview && <MarkdownBody source={artifact.preview} />}
      {!loading && !error && !artifact.preview && (
        <div className="artifact-no-preview muted">
          <strong>该产物暂无文本预览</strong>
          {location && <span>{location}</span>}
        </div>
      )}
    </section>
  );
}

function ArtifactEntry({
  artifact,
  detail,
  loading,
  error,
}: {
  artifact: Artifact;
  detail: Artifact | null;
  loading: boolean;
  error: string;
}) {
  const active = detail?.id === artifact.id;
  const controlsId = useId();
  const triggerId = useId();
  const entryRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!active) return;
    const frame = window.requestAnimationFrame(() => {
      const entry = entryRef.current;
      const panelBody = entry?.closest<HTMLElement>(".panel-body");
      const detailElement = entry?.querySelector<HTMLElement>(".artifact-detail-inline");
      if (!entry || !panelBody || !detailElement) return;
      if (
        detailElement.getBoundingClientRect().top >
        panelBody.getBoundingClientRect().bottom - 72
      ) {
        const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        entry.scrollIntoView({ block: "start", behavior: reducedMotion ? "auto" : "smooth" });
      }
    });
    return () => window.cancelAnimationFrame(frame);
  }, [active]);

  return (
    <div ref={entryRef} className={`artifact-entry${active ? " expanded" : ""}`}>
      <ArtifactRow
        artifact={artifact}
        active={active}
        controlsId={controlsId}
        triggerId={triggerId}
      />
      {active && detail && (
        <ArtifactInlineDetail
          artifact={detail}
          controlsId={controlsId}
          triggerId={triggerId}
          loading={loading}
          error={error}
        />
      )}
    </div>
  );
}

function ArtifactExecutionSection({
  executionId,
  workflowRunId,
  artifacts,
  deliverables,
  supportFiles,
  detail,
  loading,
  error,
}: {
  executionId: string;
  workflowRunId: string;
  artifacts: Artifact[];
  deliverables: Artifact[];
  supportFiles: Artifact[];
  detail: Artifact | null;
  loading: boolean;
  error: string;
}) {
  const label = executionId
    ? `技能执行 ${shortId(executionId)}`
    : workflowRunId
      ? `工作流 ${shortId(workflowRunId)}`
      : "Task 级产物";
  const supportSelected = Boolean(
    detail && supportFiles.some((artifact) => artifact.id === detail.id),
  );
  const [supportOpen, setSupportOpen] = useState(supportSelected);
  useEffect(() => {
    if (supportSelected) setSupportOpen(true);
  }, [supportSelected]);

  const entries = (rows: Artifact[]) =>
    rows.map((artifact) => (
      <ArtifactEntry
        key={artifact.id}
        artifact={artifact}
        detail={detail?.id === artifact.id ? detail : null}
        loading={detail?.id === artifact.id && loading}
        error={detail?.id === artifact.id ? error : ""}
      />
    ));
  return (
    <section
      className="artifact-execution-group"
      data-execution-id={executionId || undefined}
      data-workflow-id={workflowRunId || undefined}
    >
      <div className="artifact-execution-head">
        <span>{label}</span>
        {executionId && workflowRunId ? <small>工作流 {shortId(workflowRunId)}</small> : null}
        <small>{artifacts.length} 个产物</small>
      </div>
      <div className="artifact-execution-content">
        {deliverables.length ? (
          <section className="artifact-role-group deliverables" aria-label="交付物">
            <div className="artifact-role-head">
              <strong>交付物</strong>
              <span>{deliverables.length} 个</span>
            </div>
            <div className="artifact-role-content">{entries(deliverables)}</div>
          </section>
        ) : null}
        {supportFiles.length ? (
          <section className="artifact-role-group support" aria-label="支持文件">
            <button
              type="button"
              className="artifact-support-toggle"
              aria-expanded={supportOpen}
              onClick={() => setSupportOpen((value) => !value)}
            >
              <span>
                <strong>支持文件</strong>
                <small>输入、清单与过程文件</small>
              </span>
              <span>{supportFiles.length} 个</span>
              <IconChevronDown size={14} aria-hidden="true" />
            </button>
            {supportOpen ? (
              <div className="artifact-role-content support-content">
                {entries(supportFiles)}
              </div>
            ) : null}
          </section>
        ) : null}
      </div>
    </section>
  );
}

function ArtifactTaskSection({
  group,
  detail,
  loading,
  error,
  focused,
  defaultOpen,
}: {
  group: ArtifactTaskGroup;
  detail: Artifact | null;
  loading: boolean;
  error: string;
  focused: boolean;
  defaultOpen: boolean;
}) {
  const contentId = useId();
  const containsSelection = Boolean(
    detail && group.artifacts.some((artifact) => artifact.id === detail.id),
  );
  const [open, setOpen] = useState(focused || containsSelection || defaultOpen);
  useEffect(() => {
    if (focused || containsSelection) setOpen(true);
  }, [containsSelection, focused]);

  const title = group.taskId
    ? group.task?.title || `Task ${shortId(group.taskId)}`
    : "历史 / 未归属产物";
  return (
    <section
      className={`artifact-task-group${focused ? " focused" : ""}`}
      aria-label={title}
      data-task-id={group.taskId || undefined}
    >
      <button
        type="button"
        className="artifact-task-head"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="artifact-task-copy">
          <strong>{title}</strong>
          <span>
            {group.taskId ? `Task ${shortId(group.taskId)}` : "兼容历史数据"}
            {group.task?.status ? ` · ${group.task.status}` : ""}
          </span>
        </span>
        <span className="artifact-task-count">{group.artifacts.length} 个</span>
        <IconChevronDown className="artifact-task-chevron" size={16} aria-hidden="true" />
      </button>
      {open && (
        <div id={contentId} className="artifact-task-content">
          {group.artifacts.length === 0 && (
            <div className="artifact-task-empty">该任务暂无产物</div>
          )}
          {group.executions.map((execution) => (
            <ArtifactExecutionSection
              key={execution.key}
              executionId={execution.executionId}
              workflowRunId={execution.workflowRunId}
              artifacts={execution.artifacts}
              deliverables={execution.deliverables}
              supportFiles={execution.supportFiles}
              detail={detail}
              loading={loading}
              error={error}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.map((item) => asRecord(item)).filter((item): item is Record<string, unknown> => Boolean(item))
    : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function normalizeWorkflow(row: Record<string, unknown>): TaskWorkflow | null {
  const id = text(row.id);
  if (!id) return null;
  return {
    ...row,
    id,
    status: text(row.status),
    title: text(row.title),
    goal: text(row.goal),
    current_step_id: text(row.current_step_id),
    summary: text(row.summary),
    error: text(row.error),
    attempt: numberValue(row.attempt),
  } as TaskWorkflow;
}

function normalizeStep(row: Record<string, unknown>): TaskStep | null {
  const id = text(row.id);
  if (!id) return null;
  return {
    ...row,
    id,
    workflow_run_id: text(row.workflow_run_id),
    status: text(row.status),
    name: text(row.name),
    step_key: text(row.step_key),
    skill_name: text(row.skill_name),
    position: numberValue(row.position),
    current_execution_id: text(row.current_execution_id),
    execution_ids: Array.isArray(row.execution_ids)
      ? row.execution_ids.filter((item): item is string => typeof item === "string")
      : [],
    summary: text(row.summary),
    error: text(row.error),
    warning: text(row.warning),
  } as TaskStep;
}

function normalizeExecution(row: Record<string, unknown>): TaskExecution | null {
  const id = text(row.id);
  if (!id) return null;
  return {
    ...row,
    id,
    status: text(row.status),
    task_id: text(row.task_id),
    workflow_run_id: text(row.workflow_run_id),
    workflow_step_id: text(row.workflow_step_id),
    skill_name: text(row.skill_name),
    skill: text(row.skill),
    attempt: numberValue(row.attempt),
    step_attempt: numberValue(row.step_attempt),
    summary: text(row.summary),
    error: text(row.error),
  } as TaskExecution;
}

function mergeObjects<T extends { id: string }>(rows: T[]): T[] {
  const byId = new Map<string, T>();
  for (const row of rows) {
    const current = byId.get(row.id);
    byId.set(row.id, current ? { ...current, ...row } : row);
  }
  return [...byId.values()];
}

function normalizeTaskDetail(value: unknown): TaskDetail | null {
  const detail = asRecord(value);
  const taskRow = asRecord(detail?.task);
  const taskId = text(taskRow?.id);
  if (!detail || !taskRow || !taskId) return null;

  const workflowRows = recordArray(detail.workflows);
  const workflows = workflowRows
    .map(normalizeWorkflow)
    .filter((row): row is TaskWorkflow => Boolean(row));
  const nestedStepRows = workflowRows.flatMap((row) => recordArray(row.steps));
  const steps = mergeObjects(
    [...recordArray(detail.steps), ...nestedStepRows]
      .map(normalizeStep)
      .filter((row): row is TaskStep => Boolean(row)),
  ).sort((left, right) => (left.position || 0) - (right.position || 0));

  const nestedExecutions = [
    ...workflowRows.flatMap((row) => recordArray(row.executions)),
    ...[...recordArray(detail.steps), ...nestedStepRows].flatMap((row) => recordArray(row.executions)),
  ];
  const executions = mergeObjects(
    [
      ...recordArray(detail.direct_executions),
      ...recordArray(detail.subtasks),
      ...recordArray(detail.executions),
      ...nestedExecutions,
    ]
      .map(normalizeExecution)
      .filter((row): row is TaskExecution => Boolean(row)),
  );

  return {
    task: {
      ...taskRow,
      id: taskId,
      session_id: text(taskRow.session_id),
      parent_task_id: text(taskRow.parent_task_id),
      channel: text(taskRow.channel),
      status: text(taskRow.status),
      kind: text(taskRow.kind),
      title: text(taskRow.title),
      summary: text(taskRow.summary),
      error: text(taskRow.error),
    } as TaskSummary,
    workflows,
    steps,
    executions,
    children: recordArray(detail.children) as unknown as TaskSummary[],
    events: recordArray(detail.events) as TaskEvent[],
  };
}

function DetailSection({
  title,
  count,
  children,
}: {
  title: string;
  count: number;
  children: ReactNode;
}) {
  if (!count) return null;
  return (
    <section className="task-object-section">
      <h3>
        <span>{title}</span>
        <small>{count}</small>
      </h3>
      <div className="task-object-list">{children}</div>
    </section>
  );
}

function objectSummary(...values: Array<string | undefined>): string {
  return values.find((value) => value?.trim()) || "";
}

function TaskDetailView({ value }: { value: unknown }) {
  const detail = normalizeTaskDetail(value);
  if (!detail) return <RecordList value={value} />;
  const { task } = detail;
  const events = detail.events || [];
  return (
    <article className="task-detail-view">
      <header className="task-detail-summary">
        <div>
          <span className="object-kind">Task</span>
          <strong>{task.title || `Task ${shortId(task.id)}`}</strong>
        </div>
        <span className={`status ${task.status}`}>{task.status || "unknown"}</span>
        <code>{task.id}</code>
        {task.summary || task.error ? <p>{task.error || task.summary}</p> : null}
      </header>

      <DetailSection title="工作流" count={detail.workflows?.length || 0}>
        {detail.workflows?.map((workflow) => (
          <div key={workflow.id} className="task-object-row" data-workflow-id={workflow.id}>
            <div>
              <strong>{workflow.title || workflow.goal || `Workflow ${shortId(workflow.id)}`}</strong>
              <code>Workflow {workflow.id}</code>
            </div>
            <span className={`status ${workflow.status}`}>{workflow.status || "unknown"}</span>
            {objectSummary(workflow.error, workflow.summary) ? (
              <p>{objectSummary(workflow.error, workflow.summary)}</p>
            ) : null}
          </div>
        ))}
      </DetailSection>

      <DetailSection title="工作流步骤" count={detail.steps?.length || 0}>
        {detail.steps?.map((step) => (
          <div key={step.id} className="task-object-row" data-step-id={step.id}>
            <div>
              <strong>
                {step.position ? `${step.position}. ` : ""}
                {step.name || step.step_key || step.skill_name || `Step ${shortId(step.id)}`}
              </strong>
              <code>
                Step {step.id}
                {step.current_execution_id ? ` · current ${shortId(step.current_execution_id)}` : ""}
              </code>
            </div>
            <span className={`status ${step.status}`}>{step.status || "unknown"}</span>
            {objectSummary(step.error, step.warning, step.summary) ? (
              <p>{objectSummary(step.error, step.warning, step.summary)}</p>
            ) : null}
          </div>
        ))}
      </DetailSection>

      <DetailSection title="技能执行" count={detail.executions?.length || 0}>
        {detail.executions?.map((execution) => {
          const attempt = execution.step_attempt || execution.attempt || 1;
          return (
            <div
              key={execution.id}
              className="task-object-row execution-row"
              data-execution-id={execution.id}
            >
              <div>
                <strong>{execution.skill_name || execution.skill || "技能执行"}</strong>
                <code>Execution {execution.id}</code>
              </div>
              <span className={`status ${execution.status}`}>{execution.status || "unknown"}</span>
              <span className="object-meta">
                {execution.workflow_step_id
                  ? `Step ${shortId(execution.workflow_step_id)}${
                      execution.workflow_run_id
                        ? ` · Workflow ${shortId(execution.workflow_run_id)}`
                        : ""
                    }`
                  : execution.workflow_run_id
                    ? `Workflow ${shortId(execution.workflow_run_id)}`
                    : "直接执行"}
                {` · 第 ${attempt} 次尝试`}
              </span>
              {objectSummary(execution.error, execution.summary) ? (
                <p>{objectSummary(execution.error, execution.summary)}</p>
              ) : null}
            </div>
          );
        })}
      </DetailSection>

      <DetailSection title="子任务" count={detail.children?.length || 0}>
        {detail.children?.map((child) => (
          <div key={child.id} className="task-object-row" data-child-task-id={child.id}>
            <div>
              <strong>{child.title || child.kind || `Task ${shortId(child.id)}`}</strong>
              <code>Task {child.id}</code>
            </div>
            <span className={`status ${child.status}`}>{child.status || "unknown"}</span>
            {objectSummary(child.error, child.summary) ? (
              <p>{objectSummary(child.error, child.summary)}</p>
            ) : null}
          </div>
        ))}
      </DetailSection>

      <DetailSection title="最近活动" count={events.length}>
        {events.slice(-20).map((event, index) => (
          <div key={event.id || `${event.seq || index}`} className="task-event-row">
            <span className="task-event-dot" aria-hidden="true" />
            <div>
              <strong>
                {event.title || event.name || event.event_type || event.phase || event.kind || "活动"}
              </strong>
              {event.summary ? <p>{event.summary}</p> : null}
            </div>
            {event.status ? <span className={`status ${event.status}`}>{event.status}</span> : null}
          </div>
        ))}
      </DetailSection>
    </article>
  );
}

function TaskEntry({
  task,
  detail,
  active,
  loading,
  error,
}: {
  task: TaskSummary;
  detail: unknown;
  active: boolean;
  loading: boolean;
  error: string;
}) {
  const controlsId = useId();
  const triggerId = useId();
  return (
    <div className={`task-entry${active ? " expanded" : ""}`}>
      <TaskRow
        task={task}
        active={active}
        controlsId={controlsId}
        triggerId={triggerId}
      />
      {active ? (
        <section
          id={controlsId}
          className="panel-detail task-detail-inline"
          role="region"
          aria-labelledby={triggerId}
          aria-busy={loading || undefined}
        >
          {loading ? (
            <div className="task-detail-state" role="status">
              正在加载任务详情…
            </div>
          ) : error ? (
            <div className="task-detail-state error" role="alert">
              {error}
            </div>
          ) : (
            <TaskDetailView value={detail} />
          )}
        </section>
      ) : null}
    </div>
  );
}

function RecordList({ value }: { value: unknown }) {
  const rec = asRecord(value);
  if (!rec) {
    return <div className="empty">{value == null ? "暂无数据" : prettyValue(value)}</div>;
  }
  const entries = Object.entries(rec).filter(([key]) => key !== "ok");
  if (entries.length === 0) return <div className="empty">暂无数据</div>;
  return (
    <dl className="kv">
      {entries.map(([key, item]) => (
        <span key={key} style={{ display: "contents" }}>
          <dt>{key}</dt>
          <dd>{prettyValue(item)}</dd>
        </span>
      ))}
    </dl>
  );
}

type DrawersProps = {
  overlay?: boolean;
  fullscreen?: boolean;
  hidden?: boolean;
  onClose?: () => void;
  onToggleFullscreen?: () => void;
};

export function Drawers({
  overlay = false,
  fullscreen = false,
  hidden = false,
  onClose,
  onToggleFullscreen,
}: DrawersProps) {
  const snap = useAppState();
  const headingId = useId();
  const artifactTasks = snap.sessionId ? snap.sessionTasks : snap.tasks;
  const artifactGroups = useMemo(
    () => groupArtifactsByTask(snap.artifacts, artifactTasks, snap.artifactTaskId),
    [artifactTasks, snap.artifactTaskId, snap.artifacts],
  );
  const selectedTaskId = snap.taskSelectedId;
  if (snap.drawer === "none") return null;
  const title = TABS.find((tab) => tab.id === snap.drawer)?.label || "";
  const focusedTask =
    snap.sessionTasks.find((task) => task.id === snap.artifactTaskId) ||
    snap.tasks.find((task) => task.id === snap.artifactTaskId);
  const artifactContext = snap.artifactTaskId
    ? `Task ${shortId(snap.artifactTaskId)} · ${snap.artifacts.length} 个`
    : inspectorScopeLabel(snap.sessionId, snap.artifacts.length);
  return (
    <aside
      id="workspace-inspector"
      className={`panel${fullscreen ? " is-fullscreen" : ""}`}
      aria-labelledby={headingId}
      aria-hidden={hidden ? true : undefined}
      aria-modal={overlay ? true : undefined}
      role={overlay ? "dialog" : undefined}
      data-testid="inspector-panel"
      tabIndex={overlay ? -1 : undefined}
      onKeyDown={overlay ? trapFocus : undefined}
      inert={hidden ? "" : undefined}
      hidden={hidden}
    >
      <div className="panel-toolbar">
        <InspectorTabs
          active={snap.drawer}
          className="panel-tabs"
          onSelect={(next) => {
            if (next !== snap.drawer) void actions.openDrawer(next);
          }}
        />
        <div className="panel-head-actions">
          <button
            type="button"
            className="icon-btn square panel-maximize"
            aria-label={fullscreen ? "恢复检查器" : "最大化检查器"}
            aria-pressed={fullscreen}
            title={fullscreen ? "恢复检查器" : "最大化检查器"}
            onClick={onToggleFullscreen}
          >
            {fullscreen ? <IconMinimize size={16} /> : <IconMaximize size={16} />}
          </button>
          <button
            type="button"
            className="icon-btn square"
            aria-label={overlay ? "关闭检查器" : "收起检查器"}
            title={overlay ? "关闭检查器" : "收起检查器"}
            onClick={onClose ?? (() => void actions.openDrawer("none"))}
          >
            {overlay ? <IconClose size={16} /> : <IconPanelRightClose size={16} />}
          </button>
        </div>
      </div>
      <div className="panel-head">
        <div>
          <strong id={headingId}>{title}</strong>
          <span>
            {snap.drawer === "artifact"
              ? artifactContext
              : snap.drawer === "task"
                ? inspectorScopeLabel(snap.sessionId, snap.tasks.length)
                : inspectorScopeLabel(snap.sessionId)}
          </span>
        </div>
      </div>
      <div className="panel-body">
        {!snap.workspace && <div className="empty">打开工作区后显示该 store 的任务与产物。</div>}
        {snap.drawer === "task" && (
          <>
            {snap.tasks.length === 0 && <div className="empty">暂无任务</div>}
            {snap.tasks.length > 0 && (
              <div className="panel-list">
                {snap.tasks.map((task) => (
                  <TaskEntry
                    key={task.id}
                    task={task}
                    detail={snap.taskDetail}
                    active={selectedTaskId === task.id}
                    loading={selectedTaskId === task.id && snap.taskDetailLoading}
                    error={selectedTaskId === task.id ? snap.taskDetailError : ""}
                  />
                ))}
              </div>
            )}
          </>
        )}
        {snap.drawer === "artifact" && (
          <>
            {snap.artifactTaskId && (
              <div className="artifact-scope" role="status">
                <span>
                  <strong>{focusedTask?.title || `Task ${shortId(snap.artifactTaskId)}`}</strong>
                  <small>仅显示该任务产物</small>
                </span>
                <button type="button" onClick={() => void actions.showAllArtifacts()}>
                  {snap.sessionId ? "返回当前会话产物" : "返回当前工作区产物"}
                </button>
              </div>
            )}
            {snap.artifactListLoading && (
              <div className="artifact-list-state" role="status">
                正在加载产物…
              </div>
            )}
            {!snap.artifactListLoading && snap.artifactListError && (
              <div className="artifact-list-state error" role="alert">
                {snap.artifactListError}
              </div>
            )}
            {!snap.artifactListLoading && !snap.artifactListError && artifactGroups.length === 0 && (
              <div className="empty">
                {snap.artifactTaskId ? "该任务暂无产物" : "暂无产物"}
              </div>
            )}
            {!snap.artifactListLoading && !snap.artifactListError && artifactGroups.length > 0 && (
              <div
                className={`artifact-groups${snap.artifactTaskId ? " task-scoped" : ""}`}
              >
                {artifactGroups.map((group, index) => (
                  <ArtifactTaskSection
                    key={group.taskId || "unassigned"}
                    group={group}
                    detail={snap.artifactDetail}
                    loading={snap.artifactLoading}
                    error={snap.artifactError}
                    focused={Boolean(snap.artifactTaskId && group.taskId === snap.artifactTaskId)}
                    defaultOpen={index === 0}
                  />
                ))}
              </div>
            )}
          </>
        )}
        {snap.drawer === "rom" && <RomView value={snap.rom} />}
        {snap.drawer === "notebook" && <NotebookView value={snap.notebook} />}
        {snap.drawer === "cost" && <CostView value={snap.cost} />}
      </div>
    </aside>
  );
}

export { TABS };
