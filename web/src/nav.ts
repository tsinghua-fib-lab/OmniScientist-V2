import type { Workspace } from "./types";

const NAV_KEY = "omni.web.nav.v1";

export type NavWorkspace =
  | { kind: "path"; path: string }
  | { kind: "named"; projectDir: string; name: string };

export type NavRestore = {
  workspace: NavWorkspace;
  sessionId: string | null;
};

function parseWorkspace(value: unknown): NavWorkspace | null {
  if (typeof value === "string" && value) {
    return { kind: "path", path: value };
  }
  if (!value || typeof value !== "object") return null;
  const row = value as Record<string, unknown>;
  if (row.kind === "path" && typeof row.path === "string" && row.path) {
    return { kind: "path", path: row.path };
  }
  if (
    row.kind === "named" &&
    typeof row.projectDir === "string" &&
    row.projectDir &&
    typeof row.name === "string" &&
    row.name
  ) {
    return { kind: "named", projectDir: row.projectDir, name: row.name };
  }
  return null;
}

function parseRecord(raw: string | null): NavRestore | null {
  if (!raw) return null;
  try {
    const data = JSON.parse(raw) as Partial<NavRestore>;
    const workspace = parseWorkspace(data.workspace);
    if (!workspace) return null;
    return {
      workspace,
      sessionId: typeof data.sessionId === "string" ? data.sessionId : null,
    };
  } catch {
    return null;
  }
}

export function formatHash(workspace: NavWorkspace, sessionId: string | null): string {
  if (workspace.kind === "named") {
    const name = encodeURIComponent(workspace.name);
    return sessionId
      ? `#/w/named/${name}/s/${encodeURIComponent(sessionId)}`
      : `#/w/named/${name}`;
  }
  const path = encodeURIComponent(workspace.path);
  return sessionId ? `#/w/path/${path}/s/${encodeURIComponent(sessionId)}` : `#/w/path/${path}`;
}

export function parseHash(hash: string): NavRestore | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  const match = raw.match(
    /^\/w\/(named|path)\/([^/]+)(?:\/s\/([^/]+))?$/,
  );
  if (!match) return null;
  const [, kind, encoded, sessionEnc] = match;
  const value = decodeURIComponent(encoded || "");
  const sessionId = sessionEnc ? decodeURIComponent(sessionEnc) : null;
  if (!value) return null;
  if (kind === "named") {
    return { workspace: { kind: "named", projectDir: "", name: value }, sessionId };
  }
  return { workspace: { kind: "path", path: value }, sessionId };
}

export function readHash(): NavRestore | null {
  try {
    return parseHash(window.location.hash);
  } catch {
    return null;
  }
}

function storageGet(storage: Storage): NavRestore | null {
  try {
    return parseRecord(storage.getItem(NAV_KEY));
  } catch {
    return null;
  }
}

function storageSet(storage: Storage, value: string): void {
  try {
    storage.setItem(NAV_KEY, value);
  } catch {
    // Private mode / quota — navigation restore is optional.
  }
}

export function locatorFor(workspace: Workspace): NavWorkspace {
  return workspace.kind === "named"
    ? {
        kind: "named",
        projectDir: workspace.project_dir,
        name: workspace.project_name,
      }
    : {
        kind: "path",
        path: workspace.open_path || workspace.root || workspace.project_dir,
      };
}

export function sameNav(left: NavRestore | null, right: NavRestore | null): boolean {
  if (!left || !right) return left === right;
  if (left.sessionId !== right.sessionId) return false;
  const a = left.workspace;
  const b = right.workspace;
  if (a.kind !== b.kind) return false;
  if (a.kind === "named" && b.kind === "named") return a.name === b.name;
  if (a.kind === "path" && b.kind === "path") return a.path === b.path;
  return false;
}

export function readNav(): NavRestore | null {
  const fromHash = readHash();
  if (fromHash) {
    const stored = storageGet(sessionStorage) || storageGet(localStorage);
    if (
      fromHash.workspace.kind === "named" &&
      !fromHash.workspace.projectDir &&
      stored?.workspace.kind === "named" &&
      stored.workspace.name === fromHash.workspace.name
    ) {
      return { workspace: stored.workspace, sessionId: fromHash.sessionId };
    }
    return fromHash;
  }
  return storageGet(sessionStorage) || storageGet(localStorage);
}

export function writeNav(
  workspace: Workspace,
  sessionId: string | null,
  options: { push?: boolean } = {},
): void {
  const locator = locatorFor(workspace);
  const payload = JSON.stringify({ workspace: locator, sessionId });
  storageSet(sessionStorage, payload);
  storageSet(localStorage, payload);
  writeLocation(locator, sessionId, options.push === true);
}

export function writeLocation(
  workspace: NavWorkspace,
  sessionId: string | null,
  push = false,
): void {
  try {
    const next = formatHash(workspace, sessionId);
    if (window.location.hash === next) return;
    if (push) window.history.pushState(null, "", next);
    else window.history.replaceState(null, "", next);
  } catch {
    // jsdom / file URLs may refuse history writes.
  }
}

export function clearNav(): void {
  try {
    sessionStorage.removeItem(NAV_KEY);
    localStorage.removeItem(NAV_KEY);
  } catch {
    // ignore
  }
}
