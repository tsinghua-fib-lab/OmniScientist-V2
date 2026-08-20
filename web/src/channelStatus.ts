import type {
  ChannelDescribeResponse,
  ChannelInfo,
  ChannelRuntimeState,
  ChannelServiceState,
  ChannelSummary,
} from "./channelTypes";
import type { LocalePreference } from "./uiPrefs";

const SERVICE_DOWN = new Set(["down", "stale", "stopping", "unhealthy"]);
const RUNTIME_STATES = new Set<ChannelRuntimeState>([
  "not_configured",
  "disabled",
  "disconnected",
  "starting",
  "running",
  "degraded",
]);

export function normalizeChannelRuntime(
  channel: ChannelInfo,
  service?: ChannelServiceState,
): ChannelRuntimeState {
  if (!channel.configured) return "not_configured";
  if (!channel.enabled) return "disabled";
  const phase = String(service?.phase || "").toLowerCase();
  if (SERVICE_DOWN.has(phase)) return "disconnected";
  if (phase === "starting") return "starting";
  const state = String(channel.runtime_state || "").toLowerCase();
  if (state === "connected") return "running";
  if (RUNTIME_STATES.has(state as ChannelRuntimeState)) return state as ChannelRuntimeState;
  return "disconnected";
}

export function channelRuntimeLabel(
  state: ChannelRuntimeState,
  locale: LocalePreference,
): string {
  const zh: Record<ChannelRuntimeState, string> = {
    not_configured: "未配置",
    disabled: "已停用",
    disconnected: "已断开",
    starting: "正在启动",
    running: "适配器运行中",
    degraded: "运行异常",
    unknown: "状态暂不可用",
  };
  const en: Record<ChannelRuntimeState, string> = {
    not_configured: "Not configured",
    disabled: "Disabled",
    disconnected: "Disconnected",
    starting: "Starting",
    running: "Adapter running",
    degraded: "Runtime error",
    unknown: "Status unavailable",
  };
  return (locale === "en" ? en : zh)[state];
}

export function channelRuntimeReason(reason: string, locale: LocalePreference): string {
  const normalized = reason.trim();
  if (!normalized || locale === "en") return normalized;
  const missingDependency = /^Missing optional dependency ([a-z0-9_.-]+)\.$/i.exec(normalized);
  if (missingDependency) return `缺少可选依赖 ${missingDependency[1]}。`;
  const known: Record<string, string> = {
    "Configuration is incomplete.": "渠道配置不完整。",
    "Channel is disabled.": "渠道已停用。",
    "Home service is starting.": "后台服务正在启动。",
    "Home service is not connected.": "后台服务未连接。",
    "Channel adapter is running.": "渠道适配器正在运行。",
    "Channel adapter is starting.": "渠道适配器正在启动。",
    "Channel adapter is not running.": "渠道适配器未运行。",
    "Missing optional dependency.": "缺少可选依赖。",
    "Waiting before retry.": "正在等待重试。",
    "Another Omni service owns this channel.": "另一个 Omni 服务正在管理该渠道。",
    "Channel adapter exited; reconnect to retry.": "渠道适配器已退出，请重试启动。",
    "Channel adapter exited; retry start.": "渠道适配器已退出，请重试启动。",
    "WeChat login expired; scan the QR code again.": "微信登录已过期，请重新扫码。",
    "Channel runtime reported an error.": "渠道运行状态异常。",
  };
  return known[normalized] || "渠道运行状态异常。";
}

export function channelServiceLabel(phase: string, locale: LocalePreference): string {
  const normalized = phase.trim().toLowerCase();
  const labels = locale === "en"
    ? {
        ready: "Running",
        starting: "Starting",
        down: "Not running",
        stale: "Status unavailable",
        unhealthy: "Runtime error",
        stopping: "Stopping",
      }
    : {
        ready: "运行中",
        starting: "正在启动",
        down: "未运行",
        stale: "状态不可用",
        unhealthy: "状态异常",
        stopping: "正在停止",
      };
  return labels[normalized as keyof typeof labels] || (locale === "en" ? "Unknown" : "未知");
}

export function channelSummary(data: ChannelDescribeResponse | null): ChannelSummary {
  const result: ChannelSummary = {
    configured: 0,
    enabled: 0,
    running: 0,
    starting: 0,
    attention: 0,
  };
  if (!data) return result;
  for (const channel of data.channels) {
    if (channel.configured) result.configured += 1;
    if (!channel.enabled) continue;
    result.enabled += 1;
    const state = normalizeChannelRuntime(channel, data.service);
    if (state === "running") result.running += 1;
    else if (state === "starting") result.starting += 1;
    else if (state === "disconnected" || state === "degraded") result.attention += 1;
  }
  return result;
}

export function channelSummaryLabel(
  summary: ChannelSummary,
  locale: LocalePreference,
): string {
  if (summary.configured === 0) return locale === "en" ? "Set up channels" : "配置消息渠道";
  if (summary.enabled === 0) return locale === "en" ? "Channels disabled" : "消息渠道已停用";
  if (summary.attention > 0) {
    return locale === "en"
      ? `${summary.attention} channel issue${summary.attention === 1 ? "" : "s"}`
      : `${summary.attention} 个渠道连接异常`;
  }
  if (summary.starting > 0) {
    return locale === "en"
      ? `${summary.starting} channel${summary.starting === 1 ? "" : "s"} starting`
      : `${summary.starting} 个渠道正在启动`;
  }
  return locale === "en"
    ? `${summary.running} channel adapter${summary.running === 1 ? "" : "s"} running`
    : `${summary.running} 个渠道适配器运行中`;
}

export function channelStatusUnavailableLabel(locale: LocalePreference): string {
  return locale === "en" ? "Channel status unavailable" : "渠道状态暂不可用";
}

export function qrPath(matrix: boolean[][]): string {
  return matrix
    .flatMap((row, y) =>
      row.flatMap((filled, x) => (filled ? [`M${x} ${y}h1v1h-1z`] : [])),
    )
    .join("");
}

export function safeHttpUrl(value: string | undefined): string {
  if (!value) return "";
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : "";
  } catch {
    return "";
  }
}
