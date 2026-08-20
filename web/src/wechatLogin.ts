import type { ChannelInfo, WechatLoginResponse, WechatLoginState } from "./channelTypes";

const LIVE_STATES: ReadonlySet<WechatLoginState> = new Set([
  "waiting",
  "scanned",
  "verification_required",
]);

const TERMINAL_STATES: ReadonlySet<WechatLoginState> = new Set([
  "succeeded",
  "expired",
  "error",
]);

export function wechatLoginIsLive(state?: WechatLoginState): boolean {
  return Boolean(state && LIVE_STATES.has(state));
}

export function wechatLoginPollKey(login: WechatLoginResponse | null): string {
  if (!login || !wechatLoginIsLive(login.state)) return "";
  return login.login_id;
}

export function mergeWechatLogin(
  current: WechatLoginResponse | null,
  next: WechatLoginResponse,
): WechatLoginResponse {
  if (TERMINAL_STATES.has(next.state)) {
    return {
      login_id: next.login_id,
      state: next.state,
      message: next.message,
      service_ready: next.service_ready,
      allowed_count: next.allowed_count,
    };
  }
  return {
    ...(current || { login_id: next.login_id, state: next.state }),
    ...next,
    qr_matrix: next.qr_matrix || current?.qr_matrix,
    expires_at: next.expires_at || current?.expires_at,
  };
}

export function wechatNeedsRelogin(channel: ChannelInfo): boolean {
  const reason = (channel.runtime_reason || "").toLowerCase();
  return reason.includes("login expired") || reason.includes("scan the qr");
}
