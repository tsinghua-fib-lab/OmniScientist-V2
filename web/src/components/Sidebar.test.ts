import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Session } from "../types";
import { Sidebar } from "./Sidebar";

const snapshot = vi.hoisted(() => ({
  catalog: [
    {
      label: "omniscientist_v2",
      root: "/tmp/omniscientist_v2",
      project_dir: "/tmp/omniscientist_v2",
    },
  ],
  hiddenWorkspaces: [
    {
      label: "hidden-lab",
      root: "/tmp/hidden-lab",
      project_dir: "/tmp/hidden-lab",
    },
  ],
  sessions: [],
  sessionResults: [] as Session[],
  sessionId: null,
  workspace: {
    label: "omniscientist_v2",
    root: "/tmp/omniscientist_v2",
    project_dir: "/tmp/omniscientist_v2",
    writable: true,
  },
  channelFilter: "",
  sessionScope: "workspace",
  sessionSort: "activity",
  sessionStatusFilter: "",
  sessionListLoading: false,
  sessionListError: "",
}));

vi.mock("../store", () => ({
  actions: {
    deleteSession: vi.fn(),
    deleteSessions: vi.fn(),
    hideWorkspaces: vi.fn(),
    unhideWorkspaces: vi.fn(),
    newSession: vi.fn(),
    openPicker: vi.fn(),
    openSession: vi.fn(),
    openSessionResult: vi.fn(),
    refreshCatalog: vi.fn(),
    renameSession: vi.fn(),
    selectCatalog: vi.fn(),
    setChannelFilter: vi.fn(),
    setSessionScope: vi.fn(),
    setSessionSort: vi.fn(),
    setSessionStatusFilter: vi.fn(),
  },
  useAppState: () => snapshot,
}));

describe("Sidebar", () => {
  it("places an adjustable separator between independently scrollable workspace and session regions", () => {
    const html = renderToStaticMarkup(createElement(Sidebar));
    const workspace = html.indexOf('id="workspace-section"');
    const separator = html.indexOf('aria-label="调整工作区与会话区域高度"');
    const sessions = html.indexOf('id="session-section"');

    expect(workspace).toBeGreaterThanOrEqual(0);
    expect(separator).toBeGreaterThan(workspace);
    expect(sessions).toBeGreaterThan(separator);
    expect(html).toContain('aria-controls="workspace-section session-section"');
    expect(html).toContain('aria-orientation="horizontal"');
    expect(html).toContain('--workspace-section-height:238px');
  });

  it("groups channel, settings, workspace, and trust status into two compact footer rows", () => {
    const html = renderToStaticMarkup(
      createElement(Sidebar, {
        channelSummary: {
          configured: 1,
          enabled: 1,
          running: 1,
          starting: 0,
          attention: 0,
        },
      }),
    );
    const actionsStart = html.indexOf('class="side-foot-row side-foot-actions"');
    const contextStart = html.indexOf('class="side-foot-row side-foot-context"');

    expect(html.match(/class="side-foot-row\b/g)).toHaveLength(2);
    expect(actionsStart).toBeGreaterThanOrEqual(0);
    expect(contextStart).toBeGreaterThan(actionsStart);
    const actionsRow = html.slice(actionsStart, contextStart);
    const contextRow = html.slice(contextStart);
    expect(actionsRow).toContain("channel-launch");
    expect(actionsRow).toContain("settings-launch");
    expect(contextRow).toContain("omniscientist_v2");
    expect(contextRow).toContain("trust-state writable");
  });

  it("exposes compact scope, sort, status, channel, and management controls", () => {
    const html = renderToStaticMarkup(createElement(Sidebar));

    expect(html).toContain('aria-label="会话范围"');
    expect(html).toContain('aria-label="会话排序"');
    expect(html).toContain('aria-label="会话状态"');
    expect(html).toContain('aria-label="按渠道筛选会话"');
    expect(html).toContain("当前工作区");
    expect(html).toContain("全部工作区");
    expect(html).toContain("最近完成");
    expect(html).toContain("有问题");
    expect(html).toContain("管理工作区");
    expect(html).toContain("管理会话");
    expect(html).toContain("已隐藏 1");
  });

  it("shows workspace identity and status for global session rows", () => {
    snapshot.sessionScope = "all";
    snapshot.sessionResults = [
      {
        id: "session-global",
        title: "Cross-workspace review",
        channel: "web",
        status: "active",
        status_group: "warning",
        workspace_label: "paper-lab",
        project_dir: "/tmp/paper-lab",
      },
    ];
    const html = renderToStaticMarkup(createElement(Sidebar));
    snapshot.sessionScope = "workspace";
    snapshot.sessionResults = [];

    expect(html).toContain("Cross-workspace review");
    expect(html).toContain("paper-lab");
    expect(html).toContain("有警告");
  });
});
