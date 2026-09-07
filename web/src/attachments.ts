import type { ComposerAttachment } from "./types";

/** Mirror the CLI path normalisation so chips can carry file:// or bare paths. */

export function normalizeWebFileUri(uri: string): string {
  const raw = uri.trim();
  if (!raw || raw.startsWith("artifact://")) return "";
  if (raw.toLowerCase().startsWith("file:")) {
    try {
      const parsed = new URL(raw);
      if (parsed.protocol !== "file:") return "";
      if (parsed.hostname && parsed.hostname !== "localhost") return "";
      return decodeURIComponent(parsed.pathname);
    } catch {
      return "";
    }
  }
  return raw;
}

export function formatMention(path: string): string {
  return /\s/.test(path) ? `@"${path}"` : `@${path}`;
}

export function readyFileUris(attachments: ComposerAttachment[]): string[] {
  const extras: string[] = [];
  const seen = new Set<string>();
  for (const item of attachments) {
    if (item.status !== "ready") continue;
    const path = normalizeWebFileUri(item.uri);
    if (!path || seen.has(path)) continue;
    seen.add(path);
    extras.push(path);
  }
  return extras;
}

export function canSendComposer(text: string, attachments: ComposerAttachment[]): boolean {
  if (attachments.some((item) => item.status === "failed")) return false;
  if (text.trim()) return true;
  return attachments.some((item) => item.status === "pending" || item.status === "ready");
}

export function upsertAttachment(
  list: ComposerAttachment[],
  next: ComposerAttachment,
): ComposerAttachment[] {
  const index = list.findIndex((item) => item.id === next.id);
  if (index < 0) return [...list, next];
  const copy = list.slice();
  copy[index] = { ...copy[index], ...next };
  return copy;
}
