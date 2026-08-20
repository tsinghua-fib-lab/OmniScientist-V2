import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ChannelDescribeResponse } from "../channelTypes";
import { channelCopy } from "../channelCopy";
import { ChannelSettings, PairingNotice, WechatQr } from "./ChannelSettings";
import { WechatPanel } from "./ChannelSettingsPanels";

const initialData: ChannelDescribeResponse = {
  service: { phase: "ready" },
  channels: [
    {
      name: "wechat",
      label: "微信",
      enabled: false,
      configured: false,
      secret_set: false,
      runtime_state: "not_configured",
      runtime_reason: "",
      allowed_count: 0,
    },
    {
      name: "feishu",
      label: "飞书",
      enabled: true,
      configured: true,
      public_id: "cli_app_public",
      secret_set: true,
      runtime_state: "running",
      runtime_reason: "ok",
      allowed_count: 1,
    },
    {
      name: "dingtalk",
      label: "钉钉",
      enabled: false,
      configured: false,
      secret_set: false,
      runtime_state: "not_configured",
      runtime_reason: "",
      allowed_count: 0,
    },
  ],
};

describe("ChannelSettings", () => {
  it("renders an operational channel list without returning saved secrets", () => {
    const html = renderToStaticMarkup(
      createElement(ChannelSettings, { locale: "zh", initialData, initialChannel: "feishu" }),
    );

    expect(html).toContain("微信");
    expect(html).toContain("飞书");
    expect(html).toContain("钉钉");
    expect(html).toContain("cli_app_public");
    expect(html).toContain("已保存，留空保持不变");
    expect(html).toContain('type="password"');
    expect(html).not.toContain("app-secret-value");
    expect(html).toContain("适配器运行中");
    expect(html).toContain("重试启动");
    expect(html).not.toContain("已连接");
    expect(html).not.toContain("重新连接");
    expect(html).toContain("secrets.toml");
    expect(html).not.toContain("权限 0600");
  });

  it("keeps runtime and authorized-user facts in a dedicated status row", () => {
    const html = renderToStaticMarkup(
      createElement(ChannelSettings, { locale: "zh", initialData, initialChannel: "feishu" }),
    );

    expect(html).toContain('class="channel-detail-titlebar"');
    expect(html).toContain('data-channel-fact="runtime"');
    expect(html).toContain('data-channel-fact="access"');
    expect(html.indexOf('class="channel-facts"')).toBeGreaterThan(
      html.indexOf('class="channel-detail-titlebar"'),
    );
  });

  it("tells WeChat users to scan a QR code without mentioning the old bridge", () => {
    const html = renderToStaticMarkup(
      createElement(ChannelSettings, { locale: "zh", initialData, initialChannel: "wechat" }),
    );

    expect(html).toContain("使用微信扫描二维码完成连接");
    expect(html).not.toContain(":8088");
    expect(html).not.toContain("自建桥");
    expect(html).not.toContain("官方微信 ClawBot");
  });

  it("shows the connected WeChat page after a successful login", () => {
    const html = renderToStaticMarkup(
      createElement(WechatPanel, {
        login: {
          login_id: "login-1",
          state: "succeeded",
          qr_matrix: [[true, false]],
          expires_at: "2026-08-20T12:00:00Z",
          service_ready: true,
        },
        configured: true,
        runtimeState: "running",
        reloginRequired: false,
        verificationCode: "",
        busy: false,
        locale: "zh",
        copy: channelCopy("zh"),
        onVerificationCode() {},
        onStart() {},
        onCancel() {},
        onVerify() {},
      }),
    );

    expect(html).toContain("微信连接成功，现在可以在微信中发送消息");
    expect(html).toContain("重新扫码");
    expect(html).not.toContain("wechat-qr");
    expect(html).not.toContain("待扫描");
    expect(html).not.toContain(":8088");
  });

  it("shows an expired WeChat login instead of a green running adapter", () => {
    const html = renderToStaticMarkup(
      createElement(ChannelSettings, {
        locale: "zh",
        initialChannel: "wechat",
        initialData: {
          service: { phase: "ready" },
          channels: [
            {
              name: "wechat",
              label: "微信",
              enabled: true,
              configured: true,
              secret_set: true,
              runtime_state: "degraded",
              runtime_reason: "WeChat login expired; scan the QR code again.",
              allowed_count: 1,
            },
            initialData.channels[1],
            initialData.channels[2],
          ],
        },
      }),
    );

    expect(html).toContain("运行异常");
    expect(html).toContain("微信登录已过期，请重新扫码");
  });

  it("renders QR modules locally as an accessible SVG", () => {
    const html = renderToStaticMarkup(
      createElement(WechatQr, {
        matrix: [
          [true, false],
          [false, true],
        ],
        label: "微信登录二维码",
      }),
    );

    expect(html).toContain('aria-label="微信登录二维码"');
    expect(html).toContain('shape-rendering="crispEdges"');
    expect(html).toContain("M0 0h1v1h-1zM1 1h1v1h-1z");
  });

  it("marks a pairing code as one-time and copyable", () => {
    const html = renderToStaticMarkup(
      createElement(PairingNotice, {
        pairing: {
          code: "654321",
          command: "/pair 654321",
          expires_at: "2026-08-20T12:00:00Z",
          expires_in_seconds: 600,
        },
        locale: "zh",
      }),
    );

    expect(html).toContain("/pair 654321");
    expect(html).toContain("仅显示一次");
    expect(html).toContain("复制");
  });
});
