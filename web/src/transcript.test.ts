import { describe, expect, it } from "vitest";
import { dropLocalMessage, mergeTranscript } from "./transcript";
import type { ChatMessage } from "./types";

const server: ChatMessage[] = [
  { id: "u1", role: "user", content: "hello" },
  { id: "a1", role: "assistant", content: "hi" },
];

describe("mergeTranscript", () => {
  it("keeps an unmatched optimistic user row while a turn is busy", () => {
    const existing = [
      ...server,
      { id: "local-run-1", role: "user", content: "continue from web" },
    ];
    expect(mergeTranscript(existing, server, true).map((row) => row.id)).toEqual([
      "u1",
      "a1",
      "local-run-1",
    ]);
  });

  it("drops the optimistic row once the server echoes the same user text", () => {
    const existing = [{ id: "local-run-1", role: "user", content: "continue from web" }];
    const incoming = [
      ...server,
      { id: "u2", role: "user", content: "continue from web" },
    ];
    expect(mergeTranscript(existing, incoming, true).map((row) => row.id)).toEqual([
      "u1",
      "a1",
      "u2",
    ]);
  });

  it("does not keep locals when the session is idle", () => {
    const existing = [...server, { id: "local-run-1", role: "user", content: "stale" }];
    expect(mergeTranscript(existing, server, false)).toEqual(server);
  });

  it("removes a failed optimistic send by id", () => {
    const existing = [...server, { id: "local-run-1", role: "user", content: "nope" }];
    expect(dropLocalMessage(existing, "local-run-1")).toEqual(server);
  });
});
