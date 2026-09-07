import { describe, expect, it } from "vitest";
import {
  clipboardImageFiles,
  failedPasteImageMessage,
  PASTE_IMAGE_FAILED_PREFIX,
  PASTE_IMAGE_NO_IMAGE,
} from "./clipboardImages";

describe("clipboardImageFiles", () => {
  it("prefers FileList images", () => {
    const png = new File([new Uint8Array([1])], "shot.png", { type: "image/png" });
    const data = {
      files: [png] as unknown as FileList,
      items: [] as unknown as DataTransferItemList,
    } as DataTransfer;
    Object.defineProperty(data, "files", { value: [png] });
    Object.defineProperty(data, "items", { value: [] });
    expect(clipboardImageFiles(data).map((file) => file.name)).toEqual(["shot.png"]);
  });

  it("returns empty when the clipboard has no image", () => {
    const data = {
      files: [] as unknown as FileList,
      items: [] as unknown as DataTransferItemList,
    } as DataTransfer;
    Object.defineProperty(data, "files", { value: [] });
    Object.defineProperty(data, "items", { value: [] });
    expect(clipboardImageFiles(data)).toEqual([]);
    expect(clipboardImageFiles(null)).toEqual([]);
  });
});

describe("failedPasteImageMessage", () => {
  it("keeps Codex prefixes intact", () => {
    expect(failedPasteImageMessage(PASTE_IMAGE_NO_IMAGE)).toBe(
      `${PASTE_IMAGE_FAILED_PREFIX}${PASTE_IMAGE_NO_IMAGE}`,
    );
    expect(failedPasteImageMessage("clipboard unavailable: denied")).toBe(
      `${PASTE_IMAGE_FAILED_PREFIX}clipboard unavailable: denied`,
    );
    expect(failedPasteImageMessage("upload timed out")).toBe(
      `${PASTE_IMAGE_FAILED_PREFIX}io error: upload timed out`,
    );
  });
});
