import { describe, expect, it } from "vitest";
import { parseUiPrefs, resolvedDark } from "./uiPrefs";

describe("parseUiPrefs", () => {
  it("falls back to defaults", () => {
    expect(parseUiPrefs(null).theme).toBe("system");
    expect(parseUiPrefs("{").locale).toBe("zh");
  });

  it("keeps known values", () => {
    const prefs = parseUiPrefs(
      JSON.stringify({ theme: "dark", locale: "en", lastSection: "advanced" }),
    );
    expect(prefs).toEqual({ theme: "dark", locale: "en", lastSection: "advanced" });
  });

  it("keeps the channels settings destination", () => {
    const prefs = parseUiPrefs(
      JSON.stringify({ theme: "system", locale: "zh", lastSection: "channels" }),
    );

    expect(prefs.lastSection).toBe("channels");
  });

  it("keeps the scientist personas settings destination", () => {
    const prefs = parseUiPrefs(
      JSON.stringify({ theme: "system", locale: "zh", lastSection: "personas" }),
    );

    expect(prefs.lastSection).toBe("personas");
  });
});

describe("resolvedDark", () => {
  it("follows system only when asked", () => {
    expect(resolvedDark("light", true)).toBe(false);
    expect(resolvedDark("dark", false)).toBe(true);
    expect(resolvedDark("system", true)).toBe(true);
  });
});
