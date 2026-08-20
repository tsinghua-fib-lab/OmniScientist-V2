import { afterEach, describe, expect, it, vi } from "vitest";
import { api, watchTask } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("web request identity", () => {
  it("marks JSON RPC requests for the loopback CSRF boundary", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ ok: true, channels: [], service: { phase: "ready" } }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.describeChannels();

    expect(fetchMock).toHaveBeenCalledWith(
      "/api",
      expect.objectContaining({
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Omni-Web": "1" },
      }),
    );
  });

  it("marks streaming RPC requests", async () => {
    const fetchMock = vi.fn(async () =>
      new Response("", { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await watchTask("/tmp/project", "task-a", 0, {});

    expect(fetchMock).toHaveBeenCalledWith(
      "/api",
      expect.objectContaining({
        headers: { "Content-Type": "application/json", "X-Omni-Web": "1" },
      }),
    );
  });

  it("rejects a JSON RPC error instead of silently treating it as an event stream", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({
          ok: false,
          error: { code: "not_found", message: "task missing" },
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(watchTask("/tmp/project", "missing", 0, {})).rejects.toMatchObject({
      code: "not_found",
      message: "task missing",
    });
  });

  it("marks multipart uploads without overriding the browser boundary", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ ok: true, uri: "file:///tmp/paper.pdf" }), {
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const file = new File(["paper"], "paper.pdf", { type: "application/pdf" });

    await api.upload("/tmp/project", file);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/attachment.upload",
      expect.objectContaining({ method: "POST", headers: { "X-Omni-Web": "1" } }),
    );
  });
});
