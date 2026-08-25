import type { Session, SessionStatusGroup } from "./types";

const STATUS_LABELS: Record<SessionStatusGroup, string> = {
  running: "执行中",
  needs_attention: "待处理",
  completed: "已完成",
  warning: "有警告",
  error: "执行报错",
  cancelled: "已取消",
  empty: "暂无任务",
};

export function sessionStatusGroup(session: Session): SessionStatusGroup {
  if (session.worker === "live" || session.worker === "external") return "running";
  if (session.worker === "lost") return "warning";
  if (session.worker === "interrupted") return "error";
  if (session.status_group) return session.status_group;
  const status = String(session.latest_task_status || "");
  if (status === "running" || status === "recovering") return "running";
  if (status === "awaiting_approval" || status === "needs_input") return "needs_attention";
  if (status === "succeeded") return "completed";
  if (status === "degraded") return "warning";
  if (status === "failed" || status === "interrupted") return "error";
  if (status === "cancelled") return "cancelled";
  return "empty";
}

export function sessionStatusLabel(session: Session): string {
  if (session.worker === "external") return "后台执行中";
  if (session.worker === "lost") return "同步中断";
  if (session.worker === "interrupted") return "已中断";
  if (session.latest_task_status === "awaiting_approval") return "待批准";
  if (session.latest_task_status === "needs_input") return "待输入";
  return STATUS_LABELS[sessionStatusGroup(session)];
}

export function sessionCatalogError(errors: Array<{ message?: string }> | undefined): string {
  if (!errors?.length) return "";
  if (errors.length === 1) return errors[0]?.message || "部分工作区无法读取";
  return `${errors.length} 个工作区暂时无法读取`;
}
