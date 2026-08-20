import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { CHANNEL_LABELS, channelCopy } from "../channelCopy";
import {
  channelRuntimeLabel,
  channelServiceLabel,
  normalizeChannelRuntime,
} from "../channelStatus";
import type {
  ChannelDescribeResponse,
  ChannelInfo,
  ChannelName,
  ChannelRuntimeState,
  PairingInfo,
  WechatLoginResponse,
} from "../channelTypes";
import { IconRefresh } from "../icons";
import type { LocalePreference } from "../uiPrefs";
import { mergeWechatLogin, wechatLoginIsLive, wechatLoginPollKey, wechatNeedsRelogin } from "../wechatLogin";
import { ChannelFacts, StaticChannelPanel, WechatPanel } from "./ChannelSettingsPanels";

export { PairingNotice, WechatQr } from "./ChannelSettingsPanels";

type ChannelSettingsProps = {
  locale: LocalePreference;
  initialData?: ChannelDescribeResponse;
  initialChannel?: ChannelName;
  onChanged?: (data: ChannelDescribeResponse) => void;
};

const CHANNEL_NAMES: ChannelName[] = ["wechat", "feishu", "dingtalk"];

const EMPTY_CHANNELS: Record<ChannelName, ChannelInfo> = {
  wechat: {
    name: "wechat",
    enabled: false,
    configured: false,
    secret_set: false,
    runtime_state: "not_configured",
    allowed_count: 0,
  },
  feishu: {
    name: "feishu",
    enabled: false,
    configured: false,
    secret_set: false,
    runtime_state: "not_configured",
    allowed_count: 0,
  },
  dingtalk: {
    name: "dingtalk",
    enabled: false,
    configured: false,
    secret_set: false,
    runtime_state: "not_configured",
    allowed_count: 0,
  },
};

function publicId(channel: ChannelInfo): string {
  return channel.public_id || "";
}

function stateClass(state: ChannelRuntimeState): string {
  if (state === "running") return "good";
  if (state === "starting") return "pending";
  if (state === "disconnected" || state === "degraded") return "bad";
  if (state === "unknown") return "unknown";
  return "muted";
}

export function ChannelSettings({
  locale,
  initialData,
  initialChannel = "wechat",
  onChanged,
}: ChannelSettingsProps) {
  const copy = channelCopy(locale);
  const [data, setData] = useState<ChannelDescribeResponse | null>(initialData || null);
  const [selected, setSelected] = useState<ChannelName>(initialChannel);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [statusAvailable, setStatusAvailable] = useState(true);
  const [pairing, setPairing] = useState<PairingInfo | null>(null);
  const [wechat, setWechat] = useState<WechatLoginResponse | null>(initialData?.wechat_login || null);
  const [verificationCode, setVerificationCode] = useState("");
  const loginIdRef = useRef("");
  const channels = useMemo(() => {
    const next = { ...EMPTY_CHANNELS };
    for (const channel of data?.channels || []) next[channel.name] = channel;
    return next;
  }, [data]);
  const [publicIds, setPublicIds] = useState<Record<"feishu" | "dingtalk", string>>({
    feishu: publicId(initialData?.channels.find((item) => item.name === "feishu") || EMPTY_CHANNELS.feishu),
    dingtalk: publicId(
      initialData?.channels.find((item) => item.name === "dingtalk") || EMPTY_CHANNELS.dingtalk,
    ),
  });
  const [secrets, setSecrets] = useState<Record<"feishu" | "dingtalk", string>>({
    feishu: "",
    dingtalk: "",
  });
  const seededIds = useRef(new Set<ChannelName>());

  const applyData = useCallback(
    (next: ChannelDescribeResponse) => {
      setData(next);
      const login = next.wechat_login;
      if (login?.login_id) {
        setWechat((current) => {
          if (current && current.login_id === login.login_id) {
            return mergeWechatLogin(current, login);
          }
          if (current && wechatLoginIsLive(current.state)) return current;
          return mergeWechatLogin(null, login);
        });
        if (!loginIdRef.current) loginIdRef.current = login.login_id;
      }
      setPublicIds((current) => {
        const updated = { ...current };
        for (const name of ["feishu", "dingtalk"] as const) {
          if (seededIds.current.has(name)) continue;
          const item = next.channels.find((channel) => channel.name === name);
          updated[name] = item ? publicId(item) : "";
          seededIds.current.add(name);
        }
        return updated;
      });
      onChanged?.(next);
    },
    [onChanged],
  );

  const reload = useCallback(
    async (quiet = false) => {
      try {
        const next = await api.describeChannels();
        applyData(next);
        setStatusAvailable(true);
        if (next.notice) setNotice(next.notice);
        if (!quiet) setError("");
        return next;
      } catch (err) {
        setStatusAvailable(false);
        if (!quiet) setError(err instanceof Error ? err.message : copy.loadFailed);
        return null;
      }
    },
    [applyData, copy.loadFailed],
  );

  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const copyRef = useRef(copy);
  copyRef.current = copy;

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(true), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const loginPollKey = wechatLoginPollKey(wechat);
  useEffect(() => {
    if (!loginPollKey) return;
    let cancelled = false;
    let timer = 0;
    let inFlight = false;
    const poll = async () => {
      if (cancelled || inFlight) return;
      inFlight = true;
      try {
        const next = await api.getWechatLogin(loginPollKey);
        if (cancelled) return;
        setWechat((current) => (current ? mergeWechatLogin(current, next) : next));
        if (next.state === "succeeded") {
          setNotice(next.service_ready ? copyRef.current.ready : copyRef.current.activating);
          await reloadRef.current(true);
          return;
        }
        if (wechatLoginIsLive(next.state)) {
          timer = window.setTimeout(poll, 1400);
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.code === "login_not_found") {
          loginIdRef.current = "";
          setWechat(null);
          return;
        }
        timer = window.setTimeout(poll, 1400);
      } finally {
        inFlight = false;
      }
    };
    timer = window.setTimeout(poll, 400);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [loginPollKey]);

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (err) {
      setError(err instanceof ApiError || err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const toggle = (channel: ChannelInfo) =>
    run(`${channel.name}:toggle`, async () => {
      if (!channel.enabled && !channel.configured) throw new Error(copy.noConfig);
      if (channel.enabled) await api.disableChannel(channel.name);
      else await api.enableChannel(channel.name);
      await reload(true);
    });

  const reconnect = (channel: ChannelInfo) =>
    run(`${channel.name}:reconnect`, async () => {
      await api.reconnectChannel(channel.name);
      await reload(true);
    });

  const startWechat = () =>
    run("wechat:start", async () => {
      if (loginIdRef.current) {
        await api.cancelWechatLogin(loginIdRef.current).catch(() => undefined);
      }
      const next = await api.startWechatLogin();
      loginIdRef.current = next.login_id;
      setVerificationCode("");
      setWechat(next);
    });

  const cancelWechat = () =>
    run("wechat:cancel", async () => {
      if (wechat?.login_id) await api.cancelWechatLogin(wechat.login_id);
      loginIdRef.current = "";
      setWechat(null);
      setVerificationCode("");
    });

  const verifyWechat = () =>
    run("wechat:verify", async () => {
      if (!wechat?.login_id) return;
      const next = await api.verifyWechatLogin(wechat.login_id, verificationCode.trim());
      setWechat((current) => mergeWechatLogin(current, next));
      setVerificationCode("");
      if (next.state === "succeeded") {
        setNotice(next.service_ready ? copy.ready : copy.activating);
        await reload(true);
      }
    });

  const configure = (name: "feishu" | "dingtalk") =>
    run(`${name}:save`, async () => {
      const id = publicIds[name].trim();
      const secret = secrets[name].trim();
      if (!id) throw new Error(copy.requiredId);
      if (!secret && !channels[name].secret_set) throw new Error(copy.requiredSecret);
      const result = await api.configureChannel(name, {
        public_id: id,
        ...(secret ? { secret } : {}),
      });
      setSecrets((current) => ({ ...current, [name]: "" }));
      setPairing(result.pairing || null);
      setNotice(copy.succeeded);
      await reload(true);
    });

  const createPairing = (name: "feishu" | "dingtalk") =>
    run(`${name}:pair`, async () => {
      const result = await api.pairChannel(name);
      setPairing(result.pairing || null);
    });

  const selectedChannel = channels[selected];
  const selectedState = statusAvailable
    ? normalizeChannelRuntime(selectedChannel, data?.service)
    : "unknown";
  const reloginRequired = wechatNeedsRelogin(selectedChannel);

  return (
    <div className="channel-settings">
      <div className="channel-settings-intro">
        <p className="muted">{copy.intro}</p>
        <span className={`channel-service ${
          !statusAvailable ? "warning" : data?.service.phase === "ready" ? "good" : "bad"
        }`}>
          <span className="channel-status-dot" aria-hidden="true" />
          {copy.service}:{" "}
          {!statusAvailable
            ? copy.statusUnavailable
            : data
              ? channelServiceLabel(data.service.phase, locale)
              : "—"}
        </span>
      </div>
      {error ? <p className="banner error" role="alert">{error}</p> : null}
      {!statusAvailable ? <p className="banner" role="status">{copy.statusUnavailable}</p> : null}
      {notice ? <p className="banner" role="status">{notice}</p> : null}
      {!data ? <p className="muted">{copy.loading}</p> : null}
      <div className="channel-settings-shell">
        <nav className="channel-list" aria-label={locale === "en" ? "Message channels" : "消息渠道"}>
          {CHANNEL_NAMES.map((name) => {
            const channel = channels[name];
            const state = statusAvailable
              ? normalizeChannelRuntime(channel, data?.service)
              : "unknown";
            const label = CHANNEL_LABELS[name][locale] || channel.label || name;
            return (
              <button
                key={name}
                type="button"
                className={selected === name ? "active" : ""}
                aria-current={selected === name ? "page" : undefined}
                onClick={() => {
                  setSelected(name);
                  setPairing(null);
                  setError("");
                  setNotice("");
                  if (
                    name === "wechat" &&
                    data &&
                    !wechat &&
                    !busy &&
                    !channel.configured &&
                    normalizeChannelRuntime(channel, data?.service) !== "running"
                  ) {
                    void startWechat();
                  }
                }}
              >
                <span className="channel-list-title">{label}</span>
                <span className={`channel-state ${stateClass(state)}`}>
                  <span className="channel-status-dot" aria-hidden="true" />
                  {channelRuntimeLabel(state, locale)}
                </span>
              </button>
            );
          })}
        </nav>
        <section className="channel-detail" aria-labelledby={`channel-${selected}-title`}>
          <header className="channel-detail-head">
            <div className="channel-detail-titlebar">
              <h3 id={`channel-${selected}-title`}>
                {CHANNEL_LABELS[selected][locale] || selectedChannel.label || selected}
              </h3>
              {selectedChannel.configured ? (
                <div className="channel-head-actions">
                  <button
                    type="button"
                    className="btn ghost"
                    disabled={Boolean(busy)}
                    onClick={() => void toggle(selectedChannel)}
                  >
                    {selectedChannel.enabled ? copy.disable : copy.enable}
                  </button>
                  <button
                    type="button"
                    className="btn ghost"
                    disabled={Boolean(busy)}
                    onClick={() => void reconnect(selectedChannel)}
                  >
                    <IconRefresh size={14} />
                    {copy.reconnect}
                  </button>
                </div>
              ) : null}
            </div>
            <ChannelFacts
              channel={selectedChannel}
              state={selectedState}
              servicePhase={data ? channelServiceLabel(data.service.phase, locale) : "—"}
              locale={locale}
            />
          </header>
          {selected === "wechat" ? (
            <WechatPanel
              login={wechat}
              configured={selectedChannel.configured}
              runtimeState={selectedState}
              reloginRequired={reloginRequired}
              verificationCode={verificationCode}
              busy={Boolean(busy)}
              locale={locale}
              copy={copy}
              onVerificationCode={setVerificationCode}
              onStart={() => void startWechat()}
              onCancel={() => void cancelWechat()}
              onVerify={() => void verifyWechat()}
            />
          ) : (
            <StaticChannelPanel
              name={selected}
              channel={selectedChannel}
              publicId={publicIds[selected]}
              secret={secrets[selected]}
              pairing={pairing}
              busy={Boolean(busy)}
              locale={locale}
              copy={copy}
              onPublicId={(value) => setPublicIds((current) => ({ ...current, [selected]: value }))}
              onSecret={(value) => setSecrets((current) => ({ ...current, [selected]: value }))}
              onConfigure={() => void configure(selected)}
              onPair={() => void createPairing(selected)}
            />
          )}
          {selectedChannel.enabled && ["disconnected", "degraded"].includes(selectedState) && !reloginRequired ? (
            <p className="channel-runtime-hint" role="status">{copy.serviceDown}</p>
          ) : null}
        </section>
      </div>
    </div>
  );
}
