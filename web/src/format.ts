import type { Session } from "./types";

export function relativeTime(value?: string | number | null): string {
  if (value == null || value === "") return "";
  const ms = typeof value === "number" ? (value < 1e12 ? value * 1000 : value) : Date.parse(value);
  if (!Number.isFinite(ms)) return "";
  const delta = Date.now() - ms;
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (delta < minute) return "刚刚";
  if (delta < hour) return `${Math.floor(delta / minute)} 分钟前`;
  if (delta < day) return `${Math.floor(delta / hour)} 小时前`;
  if (delta < 7 * day) return `${Math.floor(delta / day)} 天前`;
  return new Date(ms).toLocaleDateString();
}

export function shortId(id: string): string {
  return id.length > 10 ? id.slice(0, 8) : id;
}

export function displayFileName(path?: string, uri?: string): string {
  const filePath = (path || "").trim();
  const fallback = (uri || "").trim();
  const raw = filePath || fallback;
  if (!raw) return "";
  if (!filePath && fallback.startsWith("artifact://")) return fallback;
  const cleaned = raw.replace(/\\/g, "/").replace(/\/+$/, "");
  if (!cleaned.includes("/")) return raw;
  return cleaned.split("/").filter(Boolean).pop() || raw;
}

export function displayTitle(session: Session): string {
  const text = (session.display_title || session.title || "").replace(/\s+/g, " ").trim();
  // Keep the semantic title intact. Each surface owns its visual truncation:
  // the sidebar clamps to two lines while the top bar uses a one-line ellipsis.
  return text || "新会话";
}

export function workerLabel(session: Session): string {
  if (session.worker === "live") return "运行中";
  if (session.worker === "external") return "后台执行中";
  // This is execution ownership, not the connection state of WeChat/Feishu.
  // Keep channel connectivity in the dedicated channel status surface.
  if (session.worker === "lost") return "同步中断";
  if (session.worker === "interrupted") return "已中断";
  if (session.latest_task_status === "running" || session.latest_task_status === "recovering") {
    return "运行中";
  }
  if (session.latest_task_status === "awaiting_approval") return "待批准";
  return "";
}

export function activityLabel(item: {
  title?: string;
  phase?: string;
  kind?: string;
  summary?: string;
}): string {
  return item.title || item.phase || item.kind || item.summary || "活动";
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function prettyValue(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
