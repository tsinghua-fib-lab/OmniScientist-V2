import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";

const snapshot = vi.hoisted(() => ({
  catalog: [
    {
      label: "omniscientist_v2",
      root: "/tmp/omniscientist_v2",
      project_dir: "/tmp/omniscientist_v2",
    },
  ],
  sessions: [],
  sessionId: null,
  workspace: {
    label: "omniscientist_v2",
    root: "/tmp/omniscientist_v2",
    project_dir: "/tmp/omniscientist_v2",
    writable: true,
  },
  channelFilter: "",
}));

vi.mock("../store", () => ({
  actions: {
    deleteSession: vi.fn(),
    newSession: vi.fn(),
    openPicker: vi.fn(),
    openSession: vi.fn(),
    refreshCatalog: vi.fn(),
    renameSession: vi.fn(),
    selectCatalog: vi.fn(),
    setChannelFilter: vi.fn(),
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
});
