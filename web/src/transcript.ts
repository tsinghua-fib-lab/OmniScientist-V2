import type { ChatMessage } from "./types";

export function isLocalMessageId(id: string): boolean {
  return id.startsWith("local-");
}

/** Keep optimistic local user rows that the server has not echoed yet. */
export function mergeTranscript(
  existing: ChatMessage[],
  incoming: ChatMessage[],
  keepLocal: boolean,
): ChatMessage[] {
  if (!keepLocal) return incoming;
  const serverIds = new Set(incoming.map((message) => message.id));
  const serverUserText = new Set(
    incoming.filter((message) => message.role === "user").map((message) => message.content),
  );
  const locals = existing.filter(
    (message) =>
      isLocalMessageId(message.id) &&
      !serverIds.has(message.id) &&
      !serverUserText.has(message.content),
  );
  return locals.length ? [...incoming, ...locals] : incoming;
}

export function dropLocalMessage(messages: ChatMessage[], localId: string): ChatMessage[] {
  return messages.filter((message) => message.id !== localId);
}
