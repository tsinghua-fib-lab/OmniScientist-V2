export type ThemePreference = "system" | "light" | "dark";
export type LocalePreference = "zh" | "en";
export type SettingsSection =
  | "general"
  | "models"
  | "capability"
  | "personas"
  | "channels"
  | "runtime"
  | "skills"
  | "advanced"
  | "interface";

export type UiPrefs = {
  theme: ThemePreference;
  locale: LocalePreference;
  lastSection: SettingsSection;
};

export const UI_PREFS_STORAGE_KEY = "omni.web.ui";

export const DEFAULT_UI_PREFS: UiPrefs = {
  theme: "system",
  locale: "zh",
  lastSection: "models",
};

const SECTIONS = new Set<SettingsSection>([
  "general",
  "models",
  "capability",
  "personas",
  "channels",
  "runtime",
  "skills",
  "advanced",
  "interface",
]);

export function parseUiPrefs(raw: string | null): UiPrefs {
  if (!raw) return { ...DEFAULT_UI_PREFS };
  try {
    const data = JSON.parse(raw) as Partial<UiPrefs>;
    const theme = data.theme;
    const locale = data.locale;
    const lastSection = data.lastSection;
    return {
      theme: theme === "light" || theme === "dark" || theme === "system" ? theme : "system",
      locale: locale === "en" || locale === "zh" ? locale : "zh",
      lastSection: lastSection && SECTIONS.has(lastSection) ? lastSection : "models",
    };
  } catch {
    return { ...DEFAULT_UI_PREFS };
  }
}

export function readUiPrefs(): UiPrefs {
  try {
    return parseUiPrefs(window.localStorage.getItem(UI_PREFS_STORAGE_KEY));
  } catch {
    return { ...DEFAULT_UI_PREFS };
  }
}

export function writeUiPrefs(prefs: UiPrefs): void {
  try {
    window.localStorage.setItem(UI_PREFS_STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    // Chrome can be usable with storage disabled.
  }
}

export function resolvedDark(theme: ThemePreference, systemDark = false): boolean {
  return theme === "dark" || (theme === "system" && systemDark);
}

export function applyTheme(theme: ThemePreference, root: HTMLElement = document.documentElement): void {
  const systemDark =
    typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = resolvedDark(theme, systemDark) ? "dark" : "light";
}

export function applyLocale(locale: LocalePreference, root: HTMLElement = document.documentElement): void {
  root.lang = locale === "en" ? "en" : "zh-CN";
}
