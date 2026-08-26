import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  createApiRequest,
} from "../../feishu_dispatcher/webui/api.js";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("createApiRequest", () => {
  it("缺少 token 时不发送请求", async () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const apiRequest = createApiRequest(() => "   ");

    await expect(apiRequest("/api/tasks")).rejects.toThrow(
      "请输入 http-channel.token",
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("注入 Bearer token 并返回 JSON", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ tasks: ["task-a"] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetch);
    const apiRequest = createApiRequest(() => " token-a ");

    await expect(
      apiRequest<{ tasks: string[] }>("/api/tasks", {
        headers: { "X-Test": "value-a" },
      }),
    ).resolves.toEqual({ tasks: ["task-a"] });

    expect(fetch).toHaveBeenCalledOnce();
    const [path, options] = fetch.mock.calls[0];
    expect(path).toBe("/api/tasks");
    expect(options.headers).toBeInstanceOf(Headers);
    expect(options.headers.get("Authorization")).toBe("Bearer token-a");
    expect(options.headers.get("X-Test")).toBe("value-a");
  });

  it("成功响应不是 JSON 时返回空对象", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("not-json", { status: 200 })),
    );
    const apiRequest = createApiRequest(() => "token-a");

    await expect(apiRequest("/api/tasks")).resolves.toEqual({});
  });

  it("非成功响应转换为 ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: "unknown_conversation",
            message: "conversation missing",
          }),
          {
            status: 404,
            headers: { "Content-Type": "application/json" },
          },
        ),
      ),
    );
    const apiRequest = createApiRequest(() => "token-a");

    const request = apiRequest("/api/channel/events");
    await expect(request).rejects.toThrow(
      "unknown_conversation：conversation missing",
    );
    await expect(request).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      payload: {
        error: "unknown_conversation",
        message: "conversation missing",
      },
    } satisfies Partial<ApiError>);
  });
});
