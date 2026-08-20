import { describe, expect, it } from "vitest";
import {
  DEFAULT_PANE_LAYOUT,
  PANEL_MAX_WIDTH,
  SIDEBAR_MAX_WIDTH,
  maxPanelWidth,
  maxSidebarWidth,
  parsePaneLayout,
} from "./paneLayout";

describe("pane layout preferences", () => {
  it("falls back safely when persisted layout data is missing or malformed", () => {
    expect(parsePaneLayout(null)).toEqual(DEFAULT_PANE_LAYOUT);
    expect(parsePaneLayout("not-json")).toEqual(DEFAULT_PANE_LAYOUT);
    expect(parsePaneLayout(JSON.stringify([]))).toEqual(DEFAULT_PANE_LAYOUT);
  });

  it("accepts supported preferences and clamps stale widths", () => {
    expect(
      parsePaneLayout(
        JSON.stringify({
          sidebarWidth: 9_999,
          panelWidth: 9_999,
          sidebarCollapsed: true,
        }),
      ),
    ).toEqual({
      sidebarWidth: SIDEBAR_MAX_WIDTH,
      panelWidth: PANEL_MAX_WIDTH,
      sidebarCollapsed: true,
    });
  });

  it("reserves usable space for the center column at the desktop breakpoint", () => {
    expect(maxSidebarWidth(1280, 352, true)).toBe(408);
    expect(maxPanelWidth(1280, 288, true)).toBe(472);
  });

  it("allows either side pane to use its normal maximum when the other pane is absent", () => {
    expect(maxSidebarWidth(1440, 352, false)).toBe(SIDEBAR_MAX_WIDTH);
    expect(maxPanelWidth(1440, 288, false)).toBe(PANEL_MAX_WIDTH);
  });
});
