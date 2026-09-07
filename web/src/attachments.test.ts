import { describe, expect, it } from "vitest";
import {
  canSendComposer,
  formatMention,
  normalizeWebFileUri,
  readyFileUris,
  upsertAttachment,
} from "./attachments";
import type { ComposerAttachment } from "./types";

function chip(partial: Partial<ComposerAttachment>): ComposerAttachment {
  return {
    id: "att-1",
    name: "shot.png",
    uri: "/tmp/shot.png",
    status: "ready",
    ownerWorkspace: "/tmp/project",
    ownerSession: "sess-1",
    ...partial,
  };
}

describe("normalizeWebFileUri", () => {
  it("unquotes file URIs with spaces", () => {
    const uri = "file:///Users/me/OmniScientist%20Cli.pdf";
    expect(normalizeWebFileUri(uri)).toBe("/Users/me/OmniScientist Cli.pdf");
  });

  it("keeps an already-absolute path", () => {
    expect(normalizeWebFileUri("/tmp/paper.pdf")).toBe("/tmp/paper.pdf");
  });
});

describe("readyFileUris", () => {
  it("keeps ready paths and skips pending or failed chips", () => {
    expect(
      readyFileUris([
        chip({ id: "a", uri: "file:///tmp/a.png" }),
        chip({ id: "b", uri: "", status: "pending" }),
        chip({ id: "c", uri: "/tmp/c.png", status: "failed" }),
      ]),
    ).toEqual(["/tmp/a.png"]);
  });
});

describe("canSendComposer", () => {
  it("allows text or a pending/ready attachment, but not a failed chip alone", () => {
    expect(canSendComposer("hello", [])).toBe(true);
    expect(canSendComposer("", [chip({ status: "pending", uri: "" })])).toBe(true);
    expect(canSendComposer("", [chip({ status: "failed" })])).toBe(false);
  });

  it("blocks send when any owned attachment has already failed", () => {
    expect(canSendComposer("see this", [chip({ status: "failed" })])).toBe(false);
    expect(
      canSendComposer("see this", [chip({ id: "a", status: "ready" }), chip({ id: "b", status: "failed" })]),
    ).toBe(false);
  });
});

describe("upsertAttachment", () => {
  it("updates one chip by id without replacing the rest", () => {
    const first = chip({ id: "a", status: "pending", uri: "" });
    const second = chip({ id: "b", name: "other.png" });
    const next = upsertAttachment([first, second], { ...first, status: "ready", uri: "/tmp/a.png" });
    expect(next).toHaveLength(2);
    expect(next[0]).toMatchObject({ id: "a", status: "ready", uri: "/tmp/a.png" });
    expect(next[1].id).toBe("b");
  });
});

describe("formatMention", () => {
  it("quotes paths with spaces", () => {
    expect(formatMention("/Users/me/OmniScientist Cli.pdf")).toBe(
      '@"/Users/me/OmniScientist Cli.pdf"',
    );
  });
});
