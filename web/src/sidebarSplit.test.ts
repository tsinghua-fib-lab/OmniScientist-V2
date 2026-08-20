import { describe, expect, it } from "vitest";
import {
  DEFAULT_WORKSPACE_SECTION_HEIGHT,
  SIDEBAR_SPLIT_STORAGE_KEY,
  WORKSPACE_SECTION_MIN_HEIGHT,
  clampWorkspaceSectionHeight,
  maxWorkspaceSectionHeight,
  minWorkspaceSectionHeight,
  parseWorkspaceSectionHeight,
  serializeWorkspaceSectionHeight,
} from "./sidebarSplit";

describe("sidebar workspace/session split", () => {
  it("falls back safely when the persisted height is missing or malformed", () => {
    expect(SIDEBAR_SPLIT_STORAGE_KEY).toBe("omni.web.sidebar-split.v1");
    expect(parseWorkspaceSectionHeight(null)).toBe(DEFAULT_WORKSPACE_SECTION_HEIGHT);
    expect(parseWorkspaceSectionHeight("not-json")).toBe(DEFAULT_WORKSPACE_SECTION_HEIGHT);
    expect(parseWorkspaceSectionHeight(JSON.stringify({ height: "large" }))).toBe(
      DEFAULT_WORKSPACE_SECTION_HEIGHT,
    );
  });

  it("round-trips a user-selected workspace height", () => {
    const raw = serializeWorkspaceSectionHeight(312);
    expect(parseWorkspaceSectionHeight(raw)).toBe(312);
  });

  it("preserves usable workspace and session regions", () => {
    expect(minWorkspaceSectionHeight(640)).toBe(WORKSPACE_SECTION_MIN_HEIGHT);
    expect(maxWorkspaceSectionHeight(640)).toBe(470);
    expect(clampWorkspaceSectionHeight(40, 640)).toBe(WORKSPACE_SECTION_MIN_HEIGHT);
    expect(clampWorkspaceSectionHeight(900, 640)).toBe(470);
    expect(clampWorkspaceSectionHeight(300, 640)).toBe(300);
  });

  it("shares very short sidebars without overflowing either region", () => {
    expect(minWorkspaceSectionHeight(180)).toBe(85);
    expect(maxWorkspaceSectionHeight(180)).toBe(85);
    expect(clampWorkspaceSectionHeight(238, 180)).toBe(85);
  });
});
