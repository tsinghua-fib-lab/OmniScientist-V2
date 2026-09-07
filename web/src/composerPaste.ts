/** Clipboard helpers for the composer: keep text/plain, attach images separately. */

export function insertTextAtSelection(
  value: string,
  start: number,
  end: number,
  insert: string,
): { value: string; caret: number } {
  const from = Math.max(0, Math.min(start, value.length));
  const to = Math.max(from, Math.min(end, value.length));
  return {
    value: `${value.slice(0, from)}${insert}${value.slice(to)}`,
    caret: from + insert.length,
  };
}

export function clipboardPlainText(data: DataTransfer | null | undefined): string {
  if (!data) return "";
  return data.getData("text/plain") || "";
}

export function clipboardHasImage(data: DataTransfer | null | undefined): boolean {
  if (!data) return false;
  const items = [...(data.items ?? [])];
  if (items.some((item) => item.kind === "file" && item.type.startsWith("image/"))) {
    return true;
  }
  return [...(data.files ?? [])].some((file) => file.type.startsWith("image/"));
}
