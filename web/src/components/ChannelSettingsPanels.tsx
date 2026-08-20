import { useEffect, useState } from "react";
import { channelCopy, type ChannelCopy } from "../channelCopy";
import {
  channelRuntimeLabel,
  channelRuntimeReason,
  qrPath,
  safeHttpUrl,
} from "../channelStatus";
import type {
  ChannelInfo,
  ChannelRuntimeState,
  PairingInfo,
  WechatLoginResponse,
} from "../channelTypes";
import { IconCopy, IconExternalLink } from "../icons";
import type { LocalePreference } from "../uiPrefs";

function stateClass(state: ChannelRuntimeState): string {
  if (state === "running") return "good";
  if (state === "starting") return "pending";
  if (state === "disconnected" || state === "degraded") return "bad";
  if (state === "unknown") return "unknown";
  return "muted";
}

export function ChannelFacts({
  channel,
  state,
  servicePhase,
  locale,
}: {
  channel: ChannelInfo;
  state: ChannelRuntimeState;
  servicePhase: string;
  locale: LocalePreference;
}) {
  const copy = channelCopy(locale);
  return (
    <>
      <dl className="channel-facts">
        <div data-channel-fact="config">
          <dt>{copy.config}</dt>
          <dd>
            {channel.configured
              ? locale === "en"
                ? "Saved"
                : "已保存"
              : locale === "en"
                ? "Missing"
                : "未配置"}
          </dd>
        </div>
        <div data-channel-fact="runtime">
          <dt>{copy.runtime}</dt>
          <dd className={`channel-state ${stateClass(state)}`}>
            <span className="channel-status-dot" aria-hidden="true" />
            <span className="channel-state-label">{channelRuntimeLabel(state, locale)}</span>
          </dd>
        </div>
        <div data-channel-fact="access">
          <dt>{copy.access}</dt>
          <dd className="channel-fact-count">{channel.allowed_count || 0}</dd>
        </div>
        <div data-channel-fact="service">
          <dt>{copy.service}</dt>
          <dd>{servicePhase}</dd>
        </div>
      </dl>
      {channel.runtime_reason && ["disconnected", "degraded"].includes(state) ? (
        <p className="channel-runtime-reason">
          {channelRuntimeReason(channel.runtime_reason, locale)}
        </p>
      ) : null}
    </>
  );
}

export function WechatPanel({
  login,
  configured,
  runtimeState,
  reloginRequired,
  verificationCode,
  busy,
  locale,
  copy,
  onVerificationCode,
  onStart,
  onCancel,
  onVerify,
}: {
  login: WechatLoginResponse | null;
  configured: boolean;
  runtimeState: ChannelRuntimeState;
  reloginRequired: boolean;
  verificationCode: string;
  busy: boolean;
  locale: LocalePreference;
  copy: ChannelCopy;
  onVerificationCode: (value: string) => void;
  onStart: () => void;
  onCancel: () => void;
  onVerify: () => void;
}) {
  const live = Boolean(
    login && ["waiting", "scanned", "verification_required"].includes(login.state),
  );
  if (!login || login.state === "succeeded") {
    if (configured && !live) {
      const statusText = reloginRequired
        ? copy.relogin
        : runtimeState === "running"
          ? copy.ready
          : copy.activating;
      return (
        <div className="channel-connect-empty">
          <div className="channel-mark" aria-hidden="true">
            微
          </div>
          <p role="status">{statusText}</p>
          <button type="button" className="btn" disabled={busy} onClick={onStart}>
            {copy.rebindWechat}
          </button>
        </div>
      );
    }
    if (login?.state === "succeeded") {
      return (
        <div className="channel-connect-empty">
          <p role="status">{login.service_ready && runtimeState === "running" ? copy.ready : copy.activating}</p>
        </div>
      );
    }
    return (
      <div className="channel-connect-empty">
        <div className="channel-mark" aria-hidden="true">
          微
        </div>
        <p>{locale === "en" ? "Scan the WeChat QR code to connect." : "使用微信扫描二维码完成连接。"}</p>
        <button type="button" className="btn" disabled={busy} onClick={onStart}>
          {copy.connectWechat}
        </button>
      </div>
    );
  }
  const statusText =
    login.state === "waiting"
      ? copy.waiting
      : login.state === "scanned"
        ? copy.scanned
        : login.state === "verification_required"
          ? copy.verify
          : login.state === "expired"
            ? copy.expired
            : login.message || copy.loadFailed;
  return (
    <div className="wechat-login" aria-live="polite">
      {live && login.qr_matrix?.length ? (
        <WechatQr
          matrix={login.qr_matrix}
          label={locale === "en" ? "WeChat login QR code" : "微信登录二维码"}
        />
      ) : null}
      <div className="wechat-login-copy">
        <p className="channel-login-status" role="status">
          {statusText}
        </p>
        {login.expires_at ? (
          <p className="muted">
            {locale === "en" ? "Expires" : "有效期至"}: {login.expires_at}
          </p>
        ) : null}
        {login.state === "verification_required" ? (
          <div className="channel-inline-form">
            <label htmlFor="wechat-verification-code">{copy.verifyCode}</label>
            <input
              id="wechat-verification-code"
              className="settings-input"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={verificationCode}
              onChange={(event) => onVerificationCode(event.target.value)}
            />
            <button
              type="button"
              className="btn"
              disabled={busy || !verificationCode.trim()}
              onClick={onVerify}
            >
              {copy.submit}
            </button>
          </div>
        ) : null}
        <div className="settings-actions">
          {login.state === "expired" || login.state === "error" ? (
            <button type="button" className="btn" disabled={busy} onClick={onStart}>
              {copy.refreshQr}
            </button>
          ) : null}
          <button type="button" className="btn ghost" disabled={busy} onClick={onCancel}>
            {copy.cancel}
          </button>
        </div>
      </div>
    </div>
  );
}

export function StaticChannelPanel({
  name,
  channel,
  publicId: id,
  secret,
  pairing,
  busy,
  locale,
  copy,
  onPublicId,
  onSecret,
  onConfigure,
  onPair,
}: {
  name: "feishu" | "dingtalk";
  channel: ChannelInfo;
  publicId: string;
  secret: string;
  pairing: PairingInfo | null;
  busy: boolean;
  locale: LocalePreference;
  copy: ChannelCopy;
  onPublicId: (value: string) => void;
  onSecret: (value: string) => void;
  onConfigure: () => void;
  onPair: () => void;
}) {
  const setupUrl = safeHttpUrl(channel.setup_url);
  return (
    <div className="static-channel-form">
      <label className="settings-field">
        <span className="settings-label">
          {name === "dingtalk" ? copy.dingtalkId : copy.publicId}
        </span>
        <input
          className="settings-input"
          value={id}
          autoComplete="off"
          onChange={(event) => onPublicId(event.target.value)}
        />
      </label>
      <label className="settings-field">
        <span className="settings-label">
          {name === "dingtalk" ? copy.dingtalkSecret : copy.secret}
        </span>
        <input
          className="settings-input"
          type="password"
          autoComplete="new-password"
          value={secret}
          placeholder={channel.secret_set ? copy.keepSecret : copy.unsetSecret}
          onChange={(event) => onSecret(event.target.value)}
        />
      </label>
      <p className="channel-secret-note">{copy.savedLocal}</p>
      <div className="settings-actions">
        <button type="button" className="btn" disabled={busy} onClick={onConfigure}>
          {copy.saveConnect}
        </button>
        {channel.configured ? (
          <button type="button" className="btn ghost" disabled={busy} onClick={onPair}>
            {copy.generatePair}
          </button>
        ) : null}
        {setupUrl ? (
          <a className="btn ghost" href={setupUrl} target="_blank" rel="noreferrer">
            <IconExternalLink size={14} />
            {copy.openConsole}
          </a>
        ) : null}
      </div>
      {pairing ? <PairingNotice pairing={pairing} locale={locale} /> : null}
    </div>
  );
}

export function WechatQr({ matrix, label }: { matrix: boolean[][]; label: string }) {
  const size = Math.max(matrix.length, matrix[0]?.length || 0, 1);
  return (
    <svg
      className="wechat-qr"
      viewBox={`-4 -4 ${size + 8} ${size + 8}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect x="-4" y="-4" width={size + 8} height={size + 8} fill="#fff" />
      <path d={qrPath(matrix)} fill="#101114" />
    </svg>
  );
}

export function PairingNotice({
  pairing,
  locale,
}: {
  pairing: PairingInfo;
  locale: LocalePreference;
}) {
  const [copied, setCopied] = useState(false);
  const command = pairing.command || `/pair ${pairing.code}`;
  useEffect(() => setCopied(false), [command]);
  return (
    <aside className="pairing-notice" aria-label={locale === "en" ? "Pairing code" : "配对码"}>
      <div>
        <strong>{locale === "en" ? "Pair from the channel" : "在渠道中完成配对"}</strong>
        <p>
          {locale === "en"
            ? "Shown once. Send this command to the bot before it expires."
            : "仅显示一次，请在过期前把这条命令发送给机器人。"}
        </p>
      </div>
      <code>{command}</code>
      <button
        type="button"
        className="btn ghost"
        onClick={() => {
          void navigator.clipboard.writeText(command).then(() => setCopied(true));
        }}
      >
        <IconCopy size={14} />
        {copied ? (locale === "en" ? "Copied" : "已复制") : locale === "en" ? "Copy" : "复制"}
      </button>
      {pairing.expires_at ? (
        <small>
          {locale === "en" ? "Expires" : "有效期至"}: {pairing.expires_at}
        </small>
      ) : null}
    </aside>
  );
}
