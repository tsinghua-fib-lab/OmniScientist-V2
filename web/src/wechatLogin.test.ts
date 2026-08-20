import { describe, expect, it } from "vitest";
import {
  mergeWechatLogin,
  wechatLoginIsLive,
  wechatLoginPollKey,
  wechatNeedsRelogin,
} from "./wechatLogin";
import type { WechatLoginResponse } from "./channelTypes";

const waiting: WechatLoginResponse = {
  login_id: "login-1",
  state: "waiting",
  qr_matrix: [[true, false]],
  expires_at: "2026-08-20T12:00:00Z",
};

describe("wechat login state", () => {
  it("keeps one poll key across waiting and scanned so parent rerenders do not reset polling", () => {
    expect(wechatLoginPollKey(waiting)).toBe("login-1");
    expect(wechatLoginPollKey({ ...waiting, state: "scanned" })).toBe("login-1");
    expect(wechatLoginPollKey({ ...waiting, state: "succeeded" })).toBe("");
    expect(wechatLoginIsLive("waiting")).toBe(true);
    expect(wechatLoginIsLive("scanned")).toBe(true);
    expect(wechatLoginIsLive("succeeded")).toBe(false);
  });

  it("drops the QR matrix and expiry once login reaches a terminal state", () => {
    const succeeded = mergeWechatLogin(waiting, {
      login_id: "login-1",
      state: "succeeded",
      qr_matrix: [[true]],
      expires_at: "2026-08-20T12:00:00Z",
      service_ready: true,
      allowed_count: 1,
    });
    expect(succeeded).toEqual({
      login_id: "login-1",
      state: "succeeded",
      message: undefined,
      service_ready: true,
      allowed_count: 1,
    });
    expect(succeeded.qr_matrix).toBeUndefined();
  });

  it("keeps the current QR while the backend reports scanned without a new matrix", () => {
    const scanned = mergeWechatLogin(waiting, {
      login_id: "login-1",
      state: "scanned",
    });
    expect(scanned.qr_matrix).toEqual([[true, false]]);
    expect(scanned.state).toBe("scanned");
  });

  it("treats an expired WeChat token as a relogin, not a running adapter", () => {
    expect(
      wechatNeedsRelogin({
        name: "wechat",
        enabled: true,
        configured: true,
        secret_set: true,
        runtime_state: "degraded",
        runtime_reason: "WeChat login expired; scan the QR code again.",
        allowed_count: 1,
      }),
    ).toBe(true);
  });
});
