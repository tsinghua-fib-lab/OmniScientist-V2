import { useEffect, useRef, useState } from "react";
import { activityLabel } from "../format";
import { IconActivity, IconCheck, IconChevronDown } from "../icons";
import type { ActivityItem } from "../types";

export function timelineExpansionAfterLifecycleChange(
  currentExpanded: boolean,
  previousAutoExpanded: boolean,
  nextAutoExpanded: boolean,
): boolean {
  return previousAutoExpanded === nextAutoExpanded ? currentExpanded : nextAutoExpanded;
}

export function ActivityList({
  items,
  loading = false,
  loaded = false,
  error = "",
  onRetry,
  emptyText = "暂无活动记录",
}: {
  items: ActivityItem[];
  loading?: boolean;
  loaded?: boolean;
  error?: string;
  onRetry?: () => void;
  emptyText?: string;
}) {
  return (
    <ol className="tool-list activity-timeline" aria-busy={loading || undefined}>
      {items.map((item) => (
        <li key={`${item.task_id}-${item.seq}`} className="tool activity-item">
          <span className="tool-dot" />
          <div className="activity-copy">
            <strong>{activityLabel(item)}</strong>
            {item.summary && <span>{item.summary}</span>}
            {(item.safe_args || item.safe_result || item.error) && (
              <details className="activity-diag">
                <summary>详情</summary>
                {item.safe_args ? <pre>{item.safe_args}</pre> : null}
                {item.safe_result ? <pre>{item.safe_result}</pre> : null}
                {item.error ? <pre className="activity-error">{item.error}</pre> : null}
              </details>
            )}
          </div>
        </li>
      ))}
      {loading ? (
        <li className="activity-state" role="status">
          <IconActivity size={13} />
          正在同步完整执行记录…
        </li>
      ) : null}
      {error ? (
        <li className="activity-state activity-state-error" role="alert">
          <span>执行记录加载失败：{error}</span>
          {onRetry ? (
            <button type="button" onClick={onRetry}>
              重试
            </button>
          ) : null}
        </li>
      ) : null}
      {!items.length && !loading && !error && loaded ? (
        <li className="activity-state">{emptyText}</li>
      ) : null}
      {!items.length && !loading && !error && !loaded ? (
        <li className="activity-state">展开后加载执行记录</li>
      ) : null}
    </ol>
  );
}

export function ActivityTimeline({
  items,
  streaming,
  worker,
  status = "",
  alwaysVisible = false,
  loading = false,
  loaded = false,
  error = "",
  onOpen,
  onRetry,
  ariaLabel,
}: {
  items: ActivityItem[];
  streaming: boolean;
  worker?: string;
  status?: string;
  alwaysVisible?: boolean;
  loading?: boolean;
  loaded?: boolean;
  error?: string;
  onOpen?: () => void;
  onRetry?: () => void;
  ariaLabel?: string;
}) {
  const taskActive = ["pending", "queued", "running", "recovering", "awaiting_approval"].includes(
    status,
  );
  const active = streaming || worker === "external" || taskActive;
  const unsuccessful =
    worker === "lost" || ["failed", "cancelled", "interrupted"].includes(status);
  const autoExpanded = active || worker === "lost";
  const [expanded, setExpanded] = useState(autoExpanded);
  const previousAutoExpandedRef = useRef(autoExpanded);

  useEffect(() => {
    const previousAutoExpanded = previousAutoExpandedRef.current;
    previousAutoExpandedRef.current = autoExpanded;
    setExpanded((currentExpanded) =>
      timelineExpansionAfterLifecycleChange(
        currentExpanded,
        previousAutoExpanded,
        autoExpanded,
      ),
    );
  }, [autoExpanded]);

  if (items.length === 0 && !streaming && !alwaysVisible) return null;
  const label =
    worker === "lost"
      ? "同步中断，以下是已落盘的过程"
      : worker === "external"
        ? "后台执行中"
        : streaming || taskActive
          ? "正在执行"
          : status === "failed"
            ? "执行失败"
            : status === "cancelled" || status === "interrupted"
              ? "执行已中断"
              : "执行完成";
  const detail = items.length
    ? `${items.length} 条活动`
    : loading
      ? "正在加载"
      : loaded
        ? "暂无活动记录"
        : "查看执行过程";
  return (
    <details
      className="tool-activity"
      open={expanded}
      onToggle={(event) => {
        const open = event.currentTarget.open;
        setExpanded(open);
        if (open) onOpen?.();
      }}
    >
      <summary aria-label={ariaLabel}>
        <span
          className={`activity-icon${active ? " running" : unsuccessful ? " failed" : " complete"}`}
        >
          {active || unsuccessful ? <IconActivity size={14} /> : <IconCheck size={14} />}
        </span>
        <span>
          {label} · {detail}
        </span>
        <IconChevronDown className="disclosure-icon" size={15} />
      </summary>
      <ActivityList
        items={items}
        loading={loading}
        loaded={loaded}
        error={error}
        onRetry={onRetry}
      />
    </details>
  );
}
