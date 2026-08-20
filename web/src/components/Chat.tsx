import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  activitiesForExecution,
  loadTaskActivityHistory,
  mergeTaskActivities,
} from "../activityHistory";
import { asRecord, displayTitle, relativeTime, shortId } from "../format";
import {
  IconActivity,
  IconArtifact,
  IconArrowDown,
  IconCheck,
  IconChevronDown,
  IconCopy,
  IconTask,
} from "../icons";
import { MarkdownBody } from "../markdown";
import { buildSessionTranscript, isResultMessage } from "../sessionTimeline";
import { useAppState } from "../store";
import type {
  PersonaAction,
  PersonaSnapshot,
  PersonaStartRequest,
} from "../personaTypes";
import type { LocalePreference } from "../uiPrefs";
import type { ActivityItem, ChatMessage, TaskExecution, TimelineTurn } from "../types";
import { ActivityList, ActivityTimeline } from "./ActivityTimeline";
import { PersonaQuickStart } from "./PersonaSettings";

export type TranscriptFollowMode = "following" | "paused";

const USER_SCROLL_INTENT_WINDOW_MS = 500;
const USER_SCROLL_EPSILON = 1;

export function followModeAfterTranscriptScroll(
  current: TranscriptFollowMode,
  previousScrollTop: number,
  currentScrollTop: number,
  userInitiated = true,
): TranscriptFollowMode {
  if (!userInitiated || current === "paused") return current;
  return currentScrollTop < previousScrollTop - USER_SCROLL_EPSILON ? "paused" : current;
}

export function scrollTranscriptToBottom(transcript: {
  scrollHeight: number;
  scrollTop: number;
}): void {
  transcript.scrollTop = transcript.scrollHeight;
}

export function fallbackTimelineIdentity(
  workspaceKey: string,
  sessionId: string,
  taskId: string,
): string {
  return JSON.stringify([workspaceKey, sessionId, taskId]);
}

function CopyButton({ text }: { text: string }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const copied = copyState === "copied";
  return (
    <button
      type="button"
      className="message-action"
      aria-label={copyState === "failed" ? "复制回复失败" : copied ? "已复制回复" : "复制回复"}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopyState("copied");
        } catch {
          setCopyState("failed");
        }
        window.setTimeout(() => setCopyState("idle"), 1200);
      }}
    >
      {copied ? <IconCheck size={14} /> : <IconCopy size={14} />}
      <span>{copyState === "failed" ? "复制失败" : copied ? "已复制" : "复制"}</span>
    </button>
  );
}

export function visibleUserMessage(content: string, locale: LocalePreference = "zh"): string {
  const prefix = "$soulagent ";
  if (!content.startsWith(prefix)) return content;
  try {
    const payload = JSON.parse(content.slice(prefix.length)) as Record<string, unknown>;
    if (payload.action === "unload") {
      return locale === "en" ? "Restore standard Omni scientist persona" : "恢复标准 Omni 学术人格";
    }
    const scientistId = typeof payload.scientist_id === "string" ? payload.scientist_id : "";
    const scientistName = scientistId
      .split("-")
      .filter(Boolean)
      .map((part) => part.charAt(0).toLocaleUpperCase() + part.slice(1))
      .join(" ");
    const rawInput = typeof payload.input === "string" ? payload.input : "";
    const task = rawInput.replace(/^Research task:\s*/, "");
    if (!scientistName) return content;
    if (!rawInput.startsWith("Research task:")) {
      return locale === "en"
        ? `Use ${scientistName} scientist persona for this folder`
        : `使用 ${scientistName} 学术人格（当前文件夹）`;
    }
    if (!task) return content;
    return locale === "en"
      ? `Use ${scientistName} scientist persona: ${task}`
      : `使用 ${scientistName} 学术人格：${task}`;
  } catch {
    return content;
  }
}

function metaText(meta: Record<string, unknown> | null, key: string): string {
  const value = meta?.[key];
  return typeof value === "string" ? value : "";
}

function resultIdentity(message: ChatMessage) {
  const meta = asRecord(message.meta);
  const kind = metaText(meta, "kind") || message.content_type || "";
  if (kind === "task_result") {
    const executionId =
      metaText(meta, "object_id") || metaText(meta, "subtask_id");
    return {
      kind,
      objectId: executionId,
      taskId: metaText(meta, "task_id"),
      title: metaText(meta, "skill") || message.name || "技能执行",
      status: metaText(meta, "status") || "completed",
      artifactCount: Array.isArray(meta?.artifacts) ? meta.artifacts.length : 0,
    };
  }
  if (kind === "workflow_result") {
    return {
      kind,
      objectId: metaText(meta, "workflow_run_id"),
      taskId: metaText(meta, "task_id"),
      title: message.name && message.name !== "workflow" ? message.name : "工作流",
      status: metaText(meta, "status") || "completed",
      artifactCount: Array.isArray(meta?.artifacts) ? meta.artifacts.length : 0,
    };
  }
  return null;
}

type ExecutionArtifactReference = {
  label: string;
  value: string;
};

type ParsedExecutionResult = {
  structured: boolean;
  summary: string;
  artifacts: ExecutionArtifactReference[];
  followUp: string;
};

const BACKGROUND_RESULT_PREFIX = /^\s*\[Background skill execution completed\][^\n]*\n?/i;
const ARTIFACTS_HEADING = /^\s*(?:\*\*)?Artifacts:?(?:\*\*)?\s*$/i;
const ARTIFACT_REFERENCE = /^\s*[-*]\s+(.+):\s+(.+?)\s*$/;

function parseExecutionResult(source: string): ParsedExecutionResult {
  const normalized = source.replace(/\r\n?/g, "\n").trim();
  const withoutEnvelope = normalized.replace(BACKGROUND_RESULT_PREFIX, "").trim();
  const hadEnvelope = withoutEnvelope !== normalized;
  const lines = withoutEnvelope.split("\n");
  const artifactsHeadingIndex = lines.findIndex((line) => ARTIFACTS_HEADING.test(line));

  if (artifactsHeadingIndex < 0) {
    return {
      structured: hadEnvelope,
      summary: hadEnvelope ? withoutEnvelope : normalized,
      artifacts: [],
      followUp: "",
    };
  }

  const artifacts: ExecutionArtifactReference[] = [];
  let cursor = artifactsHeadingIndex + 1;
  while (cursor < lines.length) {
    const line = lines[cursor];
    if (!line.trim()) {
      if (artifacts.length) break;
      cursor += 1;
      continue;
    }
    const reference = ARTIFACT_REFERENCE.exec(line);
    if (!reference) break;
    artifacts.push({ label: reference[1].trim(), value: reference[2].trim() });
    cursor += 1;
  }

  if (!artifacts.length) {
    return {
      structured: false,
      summary: normalized,
      artifacts: [],
      followUp: "",
    };
  }

  while (cursor < lines.length && !lines[cursor].trim()) cursor += 1;
  return {
    structured: true,
    summary: lines.slice(0, artifactsHeadingIndex).join("\n").trim(),
    artifacts,
    followUp: lines.slice(cursor).join("\n").trim(),
  };
}

function ExecutionResultContent({ source }: { source: string }) {
  const result = parseExecutionResult(source);
  if (!result.structured) {
    return (
      <div className="execution-result-content execution-result-content-raw">
        <MarkdownBody source={source} />
      </div>
    );
  }
  return (
    <div className="execution-result-content">
      {result.summary ? (
        <div className="execution-result-summary">
          <MarkdownBody source={result.summary} />
        </div>
      ) : null}
      {result.artifacts.length ? (
        <section
          className="execution-artifacts"
          aria-label={`产物引用，共 ${result.artifacts.length} 项`}
        >
          <div className="execution-artifacts-heading">
            <span className="execution-artifacts-title">
              <IconArtifact size={13} />
              <strong>产物引用</strong>
            </span>
            <span>{result.artifacts.length} 项</span>
          </div>
          <ul className="execution-artifact-list">
            {result.artifacts.map((artifact, index) => (
              <li key={`${artifact.label}-${index}`}>
                <span className="execution-artifact-label" title={artifact.label}>
                  {artifact.label}
                </span>
                <code title={artifact.value}>{artifact.value}</code>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {result.followUp ? (
        <details className="execution-result-follow-up">
          <summary>
            <span>后续操作</span>
            <IconChevronDown className="disclosure-icon" size={13} />
          </summary>
          <div>
            <MarkdownBody source={result.followUp} />
          </div>
        </details>
      ) : null}
    </div>
  );
}

function BackgroundResult({ message }: { message: ChatMessage }) {
  const result = resultIdentity(message);
  const legacy = /^\[Background skill execution completed\]/i.test(message.content);
  if (!result && !legacy) return <MarkdownBody source={message.content} />;
  const typeLabel = result?.kind === "workflow_result" ? "Workflow" : "Execution";
  const identity = result?.objectId ? `${typeLabel} ${shortId(result.objectId)}` : "未记录执行 ID";
  const taskIdentity = result?.taskId ? `Task ${shortId(result.taskId)}` : "";
  return (
    <details
      className="background-result execution-result"
      data-execution-id={result?.kind === "task_result" ? result.objectId || undefined : undefined}
      data-workflow-id={result?.kind === "workflow_result" ? result.objectId || undefined : undefined}
    >
      <summary>
        <span className="activity-icon complete">
          <IconCheck size={14} />
        </span>
        <span className="activity-copy">
          <strong>{result?.title || "后台执行结果"}</strong>
          <span className="execution-result-meta">
            {result ? [identity, taskIdentity, result.status].filter(Boolean).join(" · ") : "旧版结果 · 身份未校验"}
            {result?.artifactCount ? ` · ${result.artifactCount} 个产物` : ""}
          </span>
        </span>
        <IconChevronDown className="disclosure-icon" size={15} />
      </summary>
      <div className="background-result-body">
        <ExecutionResultContent source={message.content} />
      </div>
    </details>
  );
}

type ChatProps = {
  onOpenTaskArtifacts: (taskId: string, trigger: HTMLButtonElement) => void;
  locale?: LocalePreference;
  persona?: PersonaSnapshot | null;
  personaLoading?: boolean;
  personaBusy?: boolean;
  personaError?: string;
  personaNotice?: string;
  personaFolderPath?: string;
  pendingPersonaId?: string;
  pendingPersonaAction?: PersonaAction | "";
  onPersonaStart?: (request: PersonaStartRequest) => Promise<void>;
  onManagePersonas?: () => void;
};

function ExecutionRow({
  execution,
  activities,
  historyLoaded,
  historyLoading,
  historyError,
  onRequestHistory,
}: {
  execution: TaskExecution;
  activities: ActivityItem[];
  historyLoaded: boolean;
  historyLoading: boolean;
  historyError: string;
  onRequestHistory: () => void;
}) {
  const attempt = execution.step_attempt || execution.attempt || 1;
  const body = execution.result_content || "";
  const title = execution.skill_name || execution.skill || "技能执行";
  const executionActivities = activitiesForExecution(activities, execution);
  const summaryId = `execution-${execution.id}-summary`;
  const meta = (
    <>
      <span className={`activity-icon ${execution.status === "succeeded" ? "complete" : execution.status}`}>
        {execution.status === "succeeded" || execution.status === "completed" ? (
          <IconCheck size={14} />
        ) : (
          <IconActivity size={14} />
        )}
      </span>
      <span className="activity-copy">
        <strong>{title}</strong>
        <span className="execution-result-meta">
          {[`Execution ${shortId(execution.id)}`, execution.status, attempt > 1 ? `attempt ${attempt}` : ""]
            .filter(Boolean)
            .join(" · ")}
          {execution.artifact_count ? ` · ${execution.artifact_count} 个产物` : ""}
        </span>
      </span>
    </>
  );
  return (
    <li
      className="turn-execution"
      data-execution-id={execution.id}
      data-workflow-id={execution.workflow_run_id || undefined}
    >
      <details
        className="background-result execution-result execution-inspector"
        onToggle={(event) => {
          if (event.currentTarget.open) onRequestHistory();
        }}
      >
        <summary
          id={summaryId}
          aria-label={`查看 ${title} execution ${shortId(execution.id)} 的执行过程`}
        >
          {meta}
          <IconChevronDown className="disclosure-icon" size={15} />
        </summary>
        <div
          className="background-result-body execution-process-body"
          role="region"
          aria-labelledby={summaryId}
        >
          <div className="execution-process-heading">
            <strong>执行过程</strong>
            <span>
              {executionActivities.length
                ? `${executionActivities.length} 条活动`
                : historyLoading
                  ? "正在加载"
                  : historyLoaded
                    ? "暂无活动记录"
                    : "展开后加载"}
            </span>
          </div>
          <ActivityList
            items={executionActivities}
            loading={historyLoading}
            loaded={historyLoaded}
            error={historyError}
            onRetry={onRequestHistory}
            emptyText="该 execution 暂无细分活动记录"
          />
          {body ? (
            <div className="execution-result-body">
              <div className="execution-result-heading">
                <span className="execution-result-heading-icon">
                  <IconArtifact size={12} />
                </span>
                <strong>执行结果</strong>
              </div>
              <ExecutionResultContent source={body} />
            </div>
          ) : null}
          {execution.error ? (
            <div className="execution-inline-error" role="alert">
              {execution.error}
            </div>
          ) : null}
        </div>
      </details>
    </li>
  );
}

function TurnBlock({
  task,
  live,
  activities,
  streaming,
  worker,
  onOpenArtifacts,
  workspaceKey,
}: {
  task: TimelineTurn;
  live: boolean;
  activities: ActivityItem[];
  streaming: boolean;
  worker?: string;
  onOpenArtifacts: ChatProps["onOpenTaskArtifacts"];
  workspaceKey: string;
}) {
  const executions = task.executions || [];
  const [durableActivities, setDurableActivities] = useState<ActivityItem[]>([]);
  const [historyState, setHistoryState] = useState<"idle" | "loading" | "loaded" | "error">(
    "idle",
  );
  const [historyError, setHistoryError] = useState("");
  const requestRef = useRef(0);
  const historyStateRef = useRef(historyState);
  const previousLiveRef = useRef(live);

  useEffect(() => {
    historyStateRef.current = historyState;
  }, [historyState]);

  useEffect(() => {
    requestRef.current += 1;
    historyStateRef.current = "idle";
    setDurableActivities([]);
    setHistoryState("idle");
    setHistoryError("");
    return () => {
      requestRef.current += 1;
    };
  }, [task.id, workspaceKey]);

  const loadHistory = useCallback((force = false) => {
    if (
      !workspaceKey ||
      historyStateRef.current === "loading" ||
      (!force && historyStateRef.current === "loaded")
    ) {
      return;
    }
    const requestId = ++requestRef.current;
    historyStateRef.current = "loading";
    setHistoryState("loading");
    setHistoryError("");
    void loadTaskActivityHistory(workspaceKey, task.id)
      .then((items) => {
        if (requestRef.current !== requestId) return;
        historyStateRef.current = "loaded";
        setDurableActivities(items);
        setHistoryState("loaded");
      })
      .catch((error: unknown) => {
        if (requestRef.current !== requestId) return;
        historyStateRef.current = "error";
        setHistoryState("error");
        setHistoryError(error instanceof Error ? error.message : String(error));
      });
  }, [task.id, workspaceKey]);

  useEffect(() => {
    const wasLive = previousLiveRef.current;
    previousLiveRef.current = live;
    if (wasLive && !live) loadHistory(true);
  }, [live, loadHistory]);

  const taskActivities = useMemo(
    () => mergeTaskActivities(durableActivities, live ? activities : []),
    [activities, durableActivities, live],
  );
  const historyLoaded = historyState === "loaded";
  const historyLoading = historyState === "loading";
  return (
    <div className="task-run-block" data-task-id={task.id}>
      <div className="task-marker">
        <span className={`task-marker-icon ${task.status}`} aria-hidden="true">
          <IconTask size={16} />
        </span>
        <span className="task-marker-copy">
          <strong>{task.title || task.summary || `Turn ${shortId(task.id)}`}</strong>
          <span>
            Task {shortId(task.id)} · {task.status || "unknown"}
            {executions.length ? ` · ${executions.length} execution` : ""}
          </span>
        </span>
        <button
          type="button"
          className="task-marker-action"
          aria-controls="workspace-inspector"
          aria-label={`查看任务 ${shortId(task.id)} 的产物`}
          onClick={(event) => onOpenArtifacts(task.id, event.currentTarget)}
        >
          <IconArtifact size={15} />
          查看产物
        </button>
      </div>
      {executions.length > 0 && (
        <ul className="turn-execution-list">
          {executions.map((execution) => (
            <ExecutionRow
              key={execution.id}
              execution={execution}
              activities={taskActivities}
              historyLoaded={historyLoaded}
              historyLoading={historyLoading}
              historyError={historyError}
              onRequestHistory={loadHistory}
            />
          ))}
        </ul>
      )}
      <ActivityTimeline
        items={taskActivities}
        streaming={live && streaming}
        worker={live ? worker : ""}
        status={task.status}
        alwaysVisible
        loading={historyLoading}
        loaded={historyLoaded}
        error={historyError}
        onOpen={loadHistory}
        onRetry={loadHistory}
        ariaLabel={`查看任务 ${shortId(task.id)} 的执行过程`}
      />
    </div>
  );
}

export function Chat({
  onOpenTaskArtifacts,
  locale = "zh",
  persona = null,
  personaLoading = false,
  personaBusy = false,
  personaError,
  personaNotice,
  personaFolderPath = "",
  pendingPersonaId,
  pendingPersonaAction,
  onPersonaStart = async () => undefined,
  onManagePersonas = () => undefined,
}: ChatProps) {
  const {
    messages,
    streamingText,
    activities,
    streaming,
    currentTurn,
    workspace,
    sessionId,
    sessionOpenRevision,
    sessions,
    sessionTurns,
    sessionTasks,
  } = useAppState();
  const session = sessions.find((s) => s.id === sessionId);
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const followModeRef = useRef<TranscriptFollowMode>("following");
  const lastScrollTopRef = useRef(0);
  const userScrollIntentUntilRef = useRef(0);
  const pointerScrollingRef = useRef(false);
  const [showJump, setShowJump] = useState(false);
  const visible = messages.filter((m) => m.role === "user" || m.role === "assistant");
  const timeline = buildSessionTranscript(visible, sessionTurns.length ? sessionTurns : sessionTasks);
  const activityTaskId = currentTurn?.taskId || "";
  const activitiesAttached = Boolean(
    activityTaskId && timeline.some((item) => item.kind === "turn" && item.id === activityTaskId),
  );
  const workspaceKey = workspace?.project_dir || workspace?.open_path || "";
  const fallbackTaskId =
    activityTaskId || activities[activities.length - 1]?.task_id || "";

  const setFollowMode = useCallback((mode: TranscriptFollowMode) => {
    if (followModeRef.current === mode) return;
    followModeRef.current = mode;
    setShowJump(mode === "paused");
  }, []);

  const scrollToLatest = useCallback(() => {
    const transcript = scrollRef.current;
    if (!transcript) return;
    scrollTranscriptToBottom(transcript);
    lastScrollTopRef.current = transcript.scrollTop;
  }, []);

  const markUserScrollIntent = useCallback(() => {
    userScrollIntentUntilRef.current = Date.now() + USER_SCROLL_INTENT_WINDOW_MS;
  }, []);

  useEffect(() => {
    userScrollIntentUntilRef.current = 0;
    setFollowMode("following");
    scrollToLatest();
  }, [
    scrollToLatest,
    sessionId,
    sessionOpenRevision,
    setFollowMode,
    workspaceKey,
  ]);

  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(() => {
      if (followModeRef.current === "following") scrollToLatest();
    });
    observer.observe(content);
    const transcript = scrollRef.current;
    if (transcript) observer.observe(transcript);
    return () => observer.disconnect();
  }, [scrollToLatest, sessionId]);

  useEffect(() => {
    if (typeof ResizeObserver === "undefined" && followModeRef.current === "following") {
      scrollToLatest();
    }
  }, [activities.length, scrollToLatest, streamingText, timeline.length]);

  const jumpToLatest = () => {
    userScrollIntentUntilRef.current = 0;
    setFollowMode("following");
    scrollToLatest();
  };

  return (
    <div className="transcript-shell">
      <div
        className="transcript"
        ref={scrollRef}
        data-testid="transcript"
        onWheelCapture={markUserScrollIntent}
        onTouchMoveCapture={markUserScrollIntent}
        onKeyDownCapture={(event) => {
          const scrollsUp =
            ["ArrowUp", "PageUp", "Home"].includes(event.key) ||
            (event.key === " " && event.shiftKey);
          if (scrollsUp) markUserScrollIntent();
        }}
        onPointerDownCapture={(event) => {
          if (event.button !== 1 && event.target !== event.currentTarget) return;
          pointerScrollingRef.current = true;
          markUserScrollIntent();
        }}
        onPointerMoveCapture={() => {
          if (pointerScrollingRef.current) markUserScrollIntent();
        }}
        onPointerUpCapture={() => {
          pointerScrollingRef.current = false;
        }}
        onPointerCancelCapture={() => {
          pointerScrollingRef.current = false;
        }}
        onClickCapture={(event) => {
          const target = event.target;
          if (!(target instanceof Element)) return;
          const summary = target.closest("summary");
          const details = summary?.closest("details") as HTMLDetailsElement | null;
          if (details && !details.open) setFollowMode("paused");
        }}
        onScroll={() => {
          const el = scrollRef.current;
          if (!el) return;
          const previousScrollTop = lastScrollTopRef.current;
          lastScrollTopRef.current = el.scrollTop;
          setFollowMode(
            followModeAfterTranscriptScroll(
              followModeRef.current,
              previousScrollTop,
              el.scrollTop,
              Date.now() <= userScrollIntentUntilRef.current,
            ),
          );
        }}
      >
        <div className="thread" ref={contentRef}>
          {session && (
            <div className="thread-context" title={`${workspace?.label} · ${displayTitle(session)}`}>
              {workspace?.label} · {displayTitle(session)}
              {session.latest_task_id ? ` · Task ${shortId(session.latest_task_id)}` : ""}
              {` · Session ${shortId(session.id)}`}
            </div>
          )}
          {timeline.length === 0 && !streamingText && (
            <div className="thread-empty">
              <div className="thread-empty-intro">
                <IconActivity size={18} />
                <strong>{locale === "en" ? "Start a research conversation" : "开始一条新会话"}</strong>
                <span>
                  {locale === "en"
                    ? "Markdown, tables, code, and local file attachments are supported."
                    : "支持 Markdown、表格、代码块与本地文件附件。"}
                </span>
              </div>
              <PersonaQuickStart
                key={`persona-${workspaceKey}-${sessionId || "new"}`}
                locale={locale}
                snapshot={persona}
                loading={personaLoading}
                busy={personaBusy}
                error={personaError}
                notice={personaNotice}
                folderPath={personaFolderPath}
                pendingScientistId={pendingPersonaId}
                pendingAction={pendingPersonaAction}
                onStart={onPersonaStart}
                onManage={onManagePersonas}
              />
            </div>
          )}
          {timeline.map((item) => {
            if (item.kind === "turn") {
              return (
                <TurnBlock
                  key={`turn-${item.id}`}
                  task={item.task}
                  live={item.id === activityTaskId}
                  activities={activities}
                  streaming={streaming}
                  worker={currentTurn?.worker}
                  onOpenArtifacts={onOpenTaskArtifacts}
                  workspaceKey={workspaceKey}
                />
              );
            }
            const message = item.message;
            return item.kind === "user" ? (
              <div key={message.id} className="turn turn-user">
                {visibleUserMessage(message.content, locale)}
              </div>
            ) : (
              <div key={message.id} className="turn turn-assistant">
                {isResultMessage(message) ? (
                  <BackgroundResult message={message} />
                ) : (
                  <MarkdownBody source={message.content} />
                )}
                <div className="turn-actions">
                  <CopyButton text={message.content} />
                  {message.created_at && <span>{relativeTime(message.created_at)}</span>}
                </div>
              </div>
            );
          })}
          {!activitiesAttached ? (
            <ActivityTimeline
              key={fallbackTimelineIdentity(
                workspaceKey,
                sessionId || "",
                fallbackTaskId,
              )}
              items={activities}
              streaming={streaming}
              worker={currentTurn?.worker}
            />
          ) : null}
          {streamingText && (
            <div className="turn turn-assistant" aria-live="polite" aria-busy="true">
              <MarkdownBody source={streamingText} streaming />
            </div>
          )}
        </div>
      </div>
      {showJump && (
        <button type="button" className="jump-latest" onClick={jumpToLatest}>
          <IconArrowDown size={16} />
          回到最新消息
        </button>
      )}
    </div>
  );
}
