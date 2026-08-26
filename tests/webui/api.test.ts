import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";

import {
  ApiError,
  createApiClient,
  createApiRequest,
} from "../../feishu_dispatcher/webui/api.ts";
import type {
  AgentTaskSummary,
  DispatcherTaskSummary,
  FilePreview,
  ProjectSummary,
  TaskSummary,
  TreeEntry,
} from "../../feishu_dispatcher/webui/api.ts";

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

describe("createApiClient 类型化方法", () => {
  it("暴露精确的 Workspace 返回类型", () => {
    const api = createApiClient(() => "token-a");

    expectTypeOf(api.listProjects).returns.resolves.toEqualTypeOf<
      ProjectSummary[]
    >();
    expectTypeOf(api.loadTreeChildren).returns.resolves.toEqualTypeOf<
      TreeEntry[]
    >();
    expectTypeOf(api.readFile).returns.resolves.toEqualTypeOf<FilePreview>();
    expectTypeOf(api.listTasks).returns.resolves.toEqualTypeOf<TaskSummary[]>();
  });

  it("列出项目并归一化非法 items", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            items: [
              { name: "项目 A", path: "C:/project-a", default_agent: "codex" },
              { name: "invalid" },
            ],
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ items: null }), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");

    await expect(api.listProjects()).resolves.toEqual([
      { name: "项目 A", path: "C:/project-a", default_agent: "codex" },
    ]);
    await expect(api.listProjects()).resolves.toEqual([]);
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/projects",
      "/api/projects",
    ]);
  });

  it("编码项目名和目录路径并归一化非法 entries", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            entries: [
              { name: "文件.ts", path: "src/文件.ts", type: "file" },
              { name: "invalid", path: "src/invalid", type: "other" },
            ],
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ entries: "invalid" }), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");

    await expect(
      api.loadTreeChildren("项目 A", "src/中文"),
    ).resolves.toEqual([
      { name: "文件.ts", path: "src/文件.ts", type: "file" },
    ]);
    await expect(api.loadTreeChildren("项目 A", "")).resolves.toEqual([]);
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/projects/%E9%A1%B9%E7%9B%AE%20A/tree/children?path=src%2F%E4%B8%AD%E6%96%87",
      "/api/projects/%E9%A1%B9%E7%9B%AE%20A/tree/children?path=",
    ]);
  });

  it("读取文件时默认使用 work revision", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          path: "src/中文.ts",
          rev: "work",
          binary: false,
          content: "ok",
        }),
        { status: 200 },
      ),
    );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");

    await expect(api.readFile("项目 A", "src/中文.ts")).resolves.toEqual({
      path: "src/中文.ts",
      rev: "work",
      binary: false,
      content: "ok",
    });
    expect(fetch.mock.calls[0][0]).toBe(
      "/api/projects/%E9%A1%B9%E7%9B%AE%20A/file?path=src%2F%E4%B8%AD%E6%96%87.ts&rev=work",
    );
  });

  it("列出并过滤 Dispatcher 与 Agent Task", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            tasks: [
              {
                task_id: "dispatcher",
                kind: "dispatcher",
                description: "Dispatcher",
                status: "active",
                active: true,
              },
              {
                task_id: "task-a",
                project: "demo",
                agent: "codex",
                description: "task",
                status: "running",
                turns: 2,
                issue_url: null,
                kind: "agent",
                active: true,
              },
              {
                task_id: "invalid",
                kind: "agent",
                turns: 2,
              },
              {
                task_id: "   ",
                kind: "dispatcher",
                description: "invalid",
                status: "active",
                active: true,
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );
    const api = createApiClient(() => "token-a");

    await expect(api.listTasks()).resolves.toEqual([
      {
        task_id: "dispatcher",
        kind: "dispatcher",
        description: "Dispatcher",
        status: "active",
        active: true,
      } satisfies DispatcherTaskSummary,
      {
        task_id: "task-a",
        project: "demo",
        agent: "codex",
        description: "task",
        status: "running",
        turns: 2,
        issue_url: null,
        kind: "agent",
        active: true,
      } satisfies AgentTaskSummary,
    ]);
  });

  it("Task 顶层响应不是数组时返回空列表", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ tasks: "invalid" }), { status: 200 }),
      ),
    );
    const api = createApiClient(() => "token-a");

    await expect(api.listTasks()).resolves.toEqual([]);
  });

  it("文件响应字段不完整时抛出格式错误", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ path: "src/中文.ts", content: "ok" }), {
          status: 200,
        }),
      ),
    );
    const api = createApiClient(() => "token-a");

    await expect(api.readFile("项目 A", "src/中文.ts")).rejects.toThrow(
      "文件响应格式无效",
    );
  });
});
