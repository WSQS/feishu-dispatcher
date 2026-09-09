export const storageKeys = Object.freeze({
  channelInstance: "feishu-dispatcher.http-channel.instance",
  columnWidths: "feishu-dispatcher.webui.column-widths",
  conversation: "feishu-dispatcher.http-channel.conversation",
  cursor: (conversationId: string) =>
    `feishu-dispatcher.http-channel.cursor.${conversationId}`,
  started: (conversationId: string) =>
    `feishu-dispatcher.http-channel.started.${conversationId}`,
});

export function storageGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch (_error) {
    return null;
  }
}

export function storageSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch (_error) {
    return;
  }
}

export function storageRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch (_error) {
    return;
  }
}

export function storedConversationId(
  createConversationId: () => string,
): string {
  const existing = storageGet(storageKeys.conversation)?.trim();
  if (existing) {
    return existing;
  }
  const created = createConversationId();
  storageSet(storageKeys.conversation, created);
  return created;
}

export function storedCursor(conversationId: string): number {
  const value = Number.parseInt(
    storageGet(storageKeys.cursor(conversationId)) || "0",
    10,
  );
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

export function storedChannelInstanceId(): string | null {
  const value = storageGet(storageKeys.channelInstance)?.trim();
  return value || null;
}
