import { describe, expect, it } from "vitest";
import { bindAttachments, formatMention, normalizeWebFileUri } from "./attachments";

describe("normalizeWebFileUri", () => {
  it("unquotes file URIs with spaces", () => {
    const uri = "file:///Users/me/OmniScientist%20Cli.pdf";
    expect(normalizeWebFileUri(uri)).toBe("/Users/me/OmniScientist Cli.pdf");
  });

  it("keeps an already-absolute path", () => {
    expect(normalizeWebFileUri("/tmp/paper.pdf")).toBe("/tmp/paper.pdf");
  });
});

describe("bindAttachments", () => {
  it("appends quoted @ mentions and does not duplicate", () => {
    const path = "/Users/me/OmniScientist Cli.pdf";
    const first = bindAttachments("完整分析总结这篇论文，总结生成 PPT", [
      `file://${path.replaceAll(" ", "%20")}`,
    ]);
    expect(first.fileUris).toEqual([path]);
    expect(first.text).toContain(formatMention(path));
    expect(first.text.startsWith("完整分析总结这篇论文")).toBe(true);

    const again = bindAttachments(first.text, [path]);
    expect(again.text).toBe(first.text);
  });
});
