export type ChannelName = "wechat" | "feishu" | "dingtalk";

export type ChannelRuntimeState =
  | "not_configured"
  | "disabled"
  | "disconnected"
  | "starting"
  | "running"
  | "degraded"
  | "unknown";

export type ChannelServiceState = {
  phase: string;
  detail?: string;
};

/** A deliberately redacted view of one channel. Secrets never belong in this type. */
export type ChannelInfo = {
  name: ChannelName;
  label?: string;
  enabled: boolean;
  configured: boolean;
  public_id?: string;
  secret_set: boolean;
  runtime_state: string;
  runtime_reason?: string;
  allowed_count: number;
  mode?: string;
  bot_url?: string;
  setup_url?: string;
};

export type WechatLoginState =
  | "waiting"
  | "scanned"
  | "verification_required"
  | "expired"
  | "succeeded"
  | "cancelled"
  | "error";

export type WechatLoginResponse = {
  login_id: string;
  state: WechatLoginState;
  qr_matrix?: boolean[][];
  expires_at?: string;
  message?: string;
  service_ready?: boolean;
  allowed_count?: number;
};

export type ChannelSummary = {
  configured: number;
  enabled: number;
  running: number;
  starting: number;
  attention: number;
};

export type PairingInfo = {
  code: string;
  command: string;
  expires_at: string;
  expires_in_seconds: number;
};

export type ChannelDescribeResponse = {
  channels: ChannelInfo[];
  service: ChannelServiceState;
  wechat_login?: WechatLoginResponse;
  restart_required?: boolean;
  notice?: string;
};

export type ChannelMutationResponse = {
  channel: ChannelInfo;
  pairing?: PairingInfo;
};
