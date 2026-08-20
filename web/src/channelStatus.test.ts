import { describe, expect, it } from "vitest";
import {
  channelRuntimeLabel,
  channelRuntimeReason,
  channelServiceLabel,
  channelStatusUnavailableLabel,
  channelSummary,
  channelSummaryLabel,
  normalizeChannelRuntime,
  qrPath,
  safeHttpUrl,
} from "./channelStatus";
import type { ChannelDescribeResponse } from "./channelTypes";

const channels: ChannelDescribeResponse = {
  service: { phase: "ready" },
  channels: [
    {
      name: "wechat",
      enabled: true,
      configured: true,
      secret_set: true,
      runtime_state: "running",
      runtime_reason: "ok",
      allowed_count: 1,
    },
    {
      name: "feishu",
      enabled: true,
      configured: true,
      secret_set: true,
      runtime_state: "degraded",
      runtime_reason: "adapter stopped",
      allowed_count: 0,
    },
    {
      name: "dingtalk",
      enabled: false,
      configured: false,
      secret_set: false,
      runtime_state: "not_configured",
      runtime_reason: "",
      allowed_count: 0,
    },
  ],
};

describe("channel status", () => {
  it("keeps running distinct from a real-time connection claim", () => {
    expect(normalizeChannelRuntime(channels.channels[0], channels.service)).toBe("running");
    expect(channelRuntimeLabel("running", "zh")).toBe("适配器运行中");
    expect(channelRuntimeLabel("running", "en")).toBe("Adapter running");
    expect(channelRuntimeLabel("unknown", "zh")).toBe("状态暂不可用");
  });

  it("localizes home-service phases without leaking backend enum values", () => {
    expect(channelServiceLabel("ready", "zh")).toBe("运行中");
    expect(channelServiceLabel("down", "zh")).toBe("未运行");
    expect(channelServiceLabel("ready", "en")).toBe("Running");
    expect(channelServiceLabel("down", "en")).toBe("Not running");
  });

  it("localizes sanitized runtime reasons", () => {
    expect(channelRuntimeReason("Home service is not connected.", "zh")).toBe("后台服务未连接。");
    expect(channelRuntimeReason("Missing optional dependency dingtalk-stream.", "zh")).toBe(
      "缺少可选依赖 dingtalk-stream。",
    );
    expect(channelRuntimeReason("Home service is not connected.", "en")).toBe(
      "Home service is not connected.",
    );
    expect(channelRuntimeReason("Channel adapter exited; retry start.", "zh")).toBe(
      "渠道适配器已退出，请重试启动。",
    );
    expect(channelRuntimeReason("WeChat login expired; scan the QR code again.", "zh")).toBe(
      "微信登录已过期，请重新扫码。",
    );
  });

  it("summarizes enabled channels and highlights runtime failures", () => {
    expect(channelSummary(channels)).toEqual({
      configured: 2,
      enabled: 2,
      running: 1,
      starting: 0,
      attention: 1,
    });
  });

  it("uses operational wording rather than claiming a live provider handshake", () => {
    const summary = channelSummary(channels);
    expect(channelSummaryLabel(summary, "zh")).toBe("1 个渠道连接异常");
    expect(channelSummaryLabel({ ...summary, attention: 0 }, "en")).toBe("1 channel adapter running");
    expect(channelStatusUnavailableLabel("zh")).toBe("渠道状态暂不可用");
    expect(channelStatusUnavailableLabel("en")).toBe("Channel status unavailable");
    expect(channelSummaryLabel({ configured: 0, enabled: 0, running: 0, starting: 0, attention: 0 }, "zh"))
      .toBe("配置消息渠道");
    expect(channelSummaryLabel({ configured: 1, enabled: 0, running: 0, starting: 0, attention: 0 }, "zh"))
      .toBe("消息渠道已停用");
  });

  it("treats a stopped home service as disconnected", () => {
    expect(
      normalizeChannelRuntime(channels.channels[0], { phase: "down" }),
    ).toBe("disconnected");
  });
});

describe("wechat QR rendering", () => {
  it("builds one crisp SVG path without embedding secret source data", () => {
    expect(
      qrPath([
        [true, false],
        [false, true],
      ]),
    ).toBe("M0 0h1v1h-1zM1 1h1v1h-1z");
  });

  it("allows only http links", () => {
    expect(safeHttpUrl("https://example.com/qr")).toBe("https://example.com/qr");
    expect(safeHttpUrl("javascript:alert(1)")).toBe("");
  });
});
