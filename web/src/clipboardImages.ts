/** Clipboard image paste, matching Codex CLI error strings. */

export const PASTE_IMAGE_FAILED_PREFIX = "Failed to paste image: ";
export const PASTE_IMAGE_NO_IMAGE = "no image on clipboard: no image data or image file";
export const PASTE_IMAGE_UNAVAILABLE = "clipboard unavailable: ";

export function clipboardImageFiles(data: DataTransfer | null | undefined): File[] {
  if (!data) return [];
  const fromFiles = [...data.files].filter((file) => file.type.startsWith("image/"));
  if (fromFiles.length) return fromFiles;
  const fromItems: File[] = [];
  for (const item of data.items) {
    if (item.kind !== "file" || !item.type.startsWith("image/")) continue;
    const file = item.getAsFile();
    if (file) fromItems.push(file);
  }
  return fromItems;
}

export function failedPasteImageMessage(error: unknown): string {
  const detail = error instanceof Error ? error.message : String(error || PASTE_IMAGE_NO_IMAGE);
  if (detail.startsWith(PASTE_IMAGE_FAILED_PREFIX)) return detail;
  if (
    detail.startsWith(PASTE_IMAGE_UNAVAILABLE) ||
    detail.startsWith("no image on clipboard:") ||
    detail.startsWith("could not encode image:") ||
    detail.startsWith("io error:")
  ) {
    return `${PASTE_IMAGE_FAILED_PREFIX}${detail}`;
  }
  return `${PASTE_IMAGE_FAILED_PREFIX}io error: ${detail}`;
}
