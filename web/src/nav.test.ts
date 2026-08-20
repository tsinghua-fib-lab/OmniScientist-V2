import { beforeEach, describe, expect, it, vi } from "vitest";
import { clearNav, formatHash, parseHash, readNav, writeNav } from "./nav";
import type { Workspace } from "./types";

const sessionValues = new Map<string, string>();
const localValues = new Map<string, string>();

function applyHistoryUrl(url: string) {
  const hash = url.startsWith("#") ? url : url.includes("#") ? `#${url.split("#")[1]}` : "";
  (window.location as { hash: string }).hash = hash;
}

beforeEach(() => {
  sessionValues.clear();
  localValues.clear();
  vi.stubGlobal("sessionStorage", {
    getItem: (key: string) => sessionValues.get(key) ?? null,
    setItem: (key: string, value: string) => sessionValues.set(key, value),
    removeItem: (key: string) => sessionValues.delete(key),
  });
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => localValues.get(key) ?? null,
    setItem: (key: string, value: string) => localValues.set(key, value),
    removeItem: (key: string) => localValues.delete(key),
  });
  const location = { hash: "" };
  vi.stubGlobal("window", {
    location,
    history: {
      replaceState(_state: unknown, _title: string, url: string) {
        applyHistoryUrl(url);
      },
      pushState(_state: unknown, _title: string, url: string) {
        applyHistoryUrl(url);
      },
    },
  });
  window.history.replaceState(null, "", "/");
});

describe("navigation restore", () => {
  it("parses named and path hashes", () => {
    expect(parseHash("#/w/named/default/s/session-1")).toEqual({
      workspace: { kind: "named", projectDir: "", name: "default" },
      sessionId: "session-1",
    });
    expect(parseHash(formatHash({ kind: "path", path: "/tmp/project" }, "sess"))).toEqual({
      workspace: { kind: "path", path: "/tmp/project" },
      sessionId: "sess",
    });
  });

  it("prefers the URL hash over stored records", () => {
    sessionValues.set(
      "omni.web.nav.v1",
      JSON.stringify({
        workspace: { kind: "named", projectDir: "/tmp/.omni/projects/default", name: "default" },
        sessionId: "session-old",
      }),
    );
    window.history.replaceState(null, "", "#/w/named/default/s/session-new");

    expect(readNav()).toEqual({
      workspace: { kind: "named", projectDir: "/tmp/.omni/projects/default", name: "default" },
      sessionId: "session-new",
    });
  });

  it("keeps reading the legacy path-only record", () => {
    sessionValues.set(
      "omni.web.nav.v1",
      JSON.stringify({ workspace: "/tmp/project", sessionId: "session-old" }),
    );

    expect(readNav()).toEqual({
      workspace: { kind: "path", path: "/tmp/project" },
      sessionId: "session-old",
    });
  });

  it("persists a named project as a project selector instead of reopening its data directory", () => {
    const workspace: Workspace = {
      root: null,
      project_dir: "/Users/me/.omni/projects/default",
      project_name: "default",
      invocation_cwd: "/Users/me/.omni/projects/default",
      kind: "named",
      label: "default",
      trusted: true,
      writable: true,
      open_path: "/Users/me/.omni/projects/default",
      artifacts_dir: "/Users/me/.omni/projects/default/artifacts",
      db: "/Users/me/.omni/projects/default/sessions.sqlite3",
    };

    writeNav(workspace, "session-named");

    expect(readNav()).toEqual({
      workspace: {
        kind: "named",
        projectDir: "/Users/me/.omni/projects/default",
        name: "default",
      },
      sessionId: "session-named",
    });
    expect(window.location.hash).toBe("#/w/named/default/s/session-named");
    clearNav();
    window.history.replaceState(null, "", "/");
    expect(readNav()).toBeNull();
  });
});
