import { afterEach, describe, expect, it, vi } from "vitest";

import {
  storageGet,
  storageKeys,
  storageRemove,
  storageSet,
  storedChannelInstanceId,
  storedConversationId,
  storedCursor,
} from "../../feishu_dispatcher/webui/storage.ts";

function stubStorage(overrides: Partial<Storage> = {}): Storage {
  const storage = {
    clear: vi.fn(),
    getItem: vi.fn(() => null),
    key: vi.fn(() => null),
    length: 0,
    removeItem: vi.fn(),
    setItem: vi.fn(),
    ...overrides,
  } satisfies Storage;
  vi.stubGlobal("localStorage", storage);
  return storage;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("storageKeys", () => {
  it("保留现有持久化 key", () => {
    expect(storageKeys.channelInstance).toBe(
      "feishu-dispatcher.http-channel.instance",
    );
    expect(storageKeys.columnWidths).toBe(
      "feishu-dispatcher.webui.column-widths",
    );
    expect(storageKeys.conversation).toBe(
      "feishu-dispatcher.http-channel.conversation",
    );
    expect(storageKeys.cursor("conversation-a")).toBe(
      "feishu-dispatcher.http-channel.cursor.conversation-a",
    );
    expect(storageKeys.started("conversation-a")).toBe(
      "feishu-dispatcher.http-channel.started.conversation-a",
    );
  });
});

describe("localStorage 容错", () => {
  it("读写和删除使用相同 key", () => {
    const storage = stubStorage({
      getItem: vi.fn(() => "stored"),
    });

    expect(storageGet("key-a")).toBe("stored");
    storageSet("key-a", "value-a");
    storageRemove("key-a");

    expect(storage.getItem).toHaveBeenCalledWith("key-a");
    expect(storage.setItem).toHaveBeenCalledWith("key-a", "value-a");
    expect(storage.removeItem).toHaveBeenCalledWith("key-a");
  });

  it("浏览器拒绝访问存储时保持无异常", () => {
    stubStorage({
      getItem: vi.fn(() => {
        throw new Error("blocked");
      }),
      removeItem: vi.fn(() => {
        throw new Error("blocked");
      }),
      setItem: vi.fn(() => {
        throw new Error("blocked");
      }),
    });

    expect(storageGet("key-a")).toBeNull();
    expect(() => storageSet("key-a", "value-a")).not.toThrow();
    expect(() => storageRemove("key-a")).not.toThrow();
  });
});

describe("已存 WebUI 状态", () => {
  it("复用已有 Conversation ID", () => {
    const createConversationId = vi.fn(() => "conversation-new");
    const storage = stubStorage({
      getItem: vi.fn(() => "  conversation-existing  "),
    });

    expect(storedConversationId(createConversationId)).toBe(
      "conversation-existing",
    );
    expect(createConversationId).not.toHaveBeenCalled();
    expect(storage.setItem).not.toHaveBeenCalled();
  });

  it("缺少 Conversation ID 时生成并保存", () => {
    const storage = stubStorage();

    expect(storedConversationId(() => "conversation-new")).toBe(
      "conversation-new",
    );
    expect(storage.setItem).toHaveBeenCalledWith(
      storageKeys.conversation,
      "conversation-new",
    );
  });

  it.each([
    [null, 0],
    ["", 0],
    ["invalid", 0],
    ["-1", 0],
    ["42", 42],
  ])("将 cursor %j 解析为 %i", (raw, expected) => {
    stubStorage({ getItem: vi.fn(() => raw) });

    expect(storedCursor("conversation-a")).toBe(expected);
  });

  it("读取并清理 Channel instance ID", () => {
    stubStorage({ getItem: vi.fn(() => "  instance-a  ") });

    expect(storedChannelInstanceId()).toBe("instance-a");
  });

  it("空 Channel instance ID 返回 null", () => {
    stubStorage({ getItem: vi.fn(() => "   ") });

    expect(storedChannelInstanceId()).toBeNull();
  });
});
