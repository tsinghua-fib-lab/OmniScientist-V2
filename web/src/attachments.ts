/** Mirror the CLI ``@path`` submit so the optimistic bubble matches history. */

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

export function bindAttachments(
  text: string,
  fileUris: string[],
): { text: string; fileUris: string[] } {
  const extras: string[] = [];
  const seen = new Set<string>();
  for (const uri of fileUris) {
    const path = normalizeWebFileUri(uri);
    if (!path || seen.has(path)) continue;
    seen.add(path);
    extras.push(path);
  }
  const inject = extras.filter(
    (path) => !text.includes(`@${path}`) && !text.includes(`@"${path}"`),
  );
  if (!inject.length) return { text, fileUris: extras };
  const mentions = inject.map(formatMention).join("\n");
  const body = text.replace(/\s+$/, "");
  return {
    text: body ? `${body}\n\n${mentions}` : mentions,
    fileUris: extras,
  };
}
