export const SIDEBAR_SPLIT_STORAGE_KEY = "omni.web.sidebar-split.v1";
export const DEFAULT_WORKSPACE_SECTION_HEIGHT = 238;
export const WORKSPACE_SECTION_MIN_HEIGHT = 96;
export const SESSION_SECTION_MIN_HEIGHT = 160;
export const SIDEBAR_SPLIT_HANDLE_HEIGHT = 10;

const MAX_PERSISTED_HEIGHT = 2_000;

export function minWorkspaceSectionHeight(containerHeight: number): number {
  const usableHeight = Math.max(0, Math.floor(containerHeight) - SIDEBAR_SPLIT_HANDLE_HEIGHT);
  return Math.min(WORKSPACE_SECTION_MIN_HEIGHT, Math.floor(usableHeight / 2));
}

export function maxWorkspaceSectionHeight(containerHeight: number): number {
  const usableHeight = Math.max(0, Math.floor(containerHeight) - SIDEBAR_SPLIT_HANDLE_HEIGHT);
  const workspaceMin = minWorkspaceSectionHeight(containerHeight);
  const sessionMin = Math.min(SESSION_SECTION_MIN_HEIGHT, usableHeight - workspaceMin);
  return Math.max(workspaceMin, usableHeight - sessionMin);
}

export function clampWorkspaceSectionHeight(value: number, containerHeight: number): number {
  const finiteValue = Number.isFinite(value) ? value : DEFAULT_WORKSPACE_SECTION_HEIGHT;
  const min = minWorkspaceSectionHeight(containerHeight);
  return Math.min(
    Math.max(Math.round(finiteValue), min),
    maxWorkspaceSectionHeight(containerHeight),
  );
}

export function parseWorkspaceSectionHeight(raw: string | null): number {
  if (!raw) return DEFAULT_WORKSPACE_SECTION_HEIGHT;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      return DEFAULT_WORKSPACE_SECTION_HEIGHT;
    }
    const height = (value as Record<string, unknown>).workspaceHeight;
    if (typeof height !== "number" || !Number.isFinite(height)) {
      return DEFAULT_WORKSPACE_SECTION_HEIGHT;
    }
    return Math.min(
      Math.max(Math.round(height), WORKSPACE_SECTION_MIN_HEIGHT),
      MAX_PERSISTED_HEIGHT,
    );
  } catch {
    return DEFAULT_WORKSPACE_SECTION_HEIGHT;
  }
}

export function serializeWorkspaceSectionHeight(workspaceHeight: number): string {
  return JSON.stringify({ workspaceHeight: Math.round(workspaceHeight) });
}
