export type PaneLayoutPreferences = {
  sidebarWidth: number;
  panelWidth: number;
  sidebarCollapsed: boolean;
};

export const PANE_LAYOUT_STORAGE_KEY = "omni.web.pane-layout.v1";
export const SIDEBAR_MIN_WIDTH = 232;
export const SIDEBAR_MAX_WIDTH = 440;
export const PANEL_MIN_WIDTH = 320;
export const PANEL_MAX_WIDTH = 720;
export const MAIN_MIN_WIDTH = 520;

export const DEFAULT_PANE_LAYOUT: PaneLayoutPreferences = {
  sidebarWidth: 288,
  panelWidth: 352,
  sidebarCollapsed: false,
};

export function clampPaneWidth(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

export function maxSidebarWidth(
  viewportWidth: number,
  panelWidth: number,
  panelVisible: boolean,
): number {
  const available = viewportWidth - (panelVisible ? panelWidth : 0) - MAIN_MIN_WIDTH;
  return clampPaneWidth(available, SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH);
}

export function maxPanelWidth(
  viewportWidth: number,
  sidebarWidth: number,
  sidebarVisible: boolean,
): number {
  const available = viewportWidth - (sidebarVisible ? sidebarWidth : 0) - MAIN_MIN_WIDTH;
  return clampPaneWidth(available, PANEL_MIN_WIDTH, PANEL_MAX_WIDTH);
}

export function parsePaneLayout(raw: string | null): PaneLayoutPreferences {
  if (!raw) return { ...DEFAULT_PANE_LAYOUT };
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return { ...DEFAULT_PANE_LAYOUT };
    }
    const record = value as Record<string, unknown>;
    const sidebarWidth =
      typeof record.sidebarWidth === "number" && Number.isFinite(record.sidebarWidth)
        ? record.sidebarWidth
        : DEFAULT_PANE_LAYOUT.sidebarWidth;
    const panelWidth =
      typeof record.panelWidth === "number" && Number.isFinite(record.panelWidth)
        ? record.panelWidth
        : DEFAULT_PANE_LAYOUT.panelWidth;
    return {
      sidebarWidth: clampPaneWidth(
        sidebarWidth,
        SIDEBAR_MIN_WIDTH,
        SIDEBAR_MAX_WIDTH,
      ),
      panelWidth: clampPaneWidth(panelWidth, PANEL_MIN_WIDTH, PANEL_MAX_WIDTH),
      sidebarCollapsed:
        typeof record.sidebarCollapsed === "boolean"
          ? record.sidebarCollapsed
          : DEFAULT_PANE_LAYOUT.sidebarCollapsed,
    };
  } catch {
    return { ...DEFAULT_PANE_LAYOUT };
  }
}

export function serializePaneLayout(value: PaneLayoutPreferences): string {
  return JSON.stringify(value);
}
