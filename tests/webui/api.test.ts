import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";

import {
  ApiError,
  createApiClient,
  createApiRequest,
} from "../../feishu_dispatcher/webui/api.ts";
import type {
  AgentTaskSummary,
  ChannelEventPage,
  ChannelHealth,
  ChannelMessageAccepted,
  DispatcherTaskSummary,
  FilePreview,
  ProjectSummary,
  SendChannelMessageRequest,
  TaskEventPage,
  SessionConversationCreated,
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

    expect(api).not.toHaveProperty("request");
    expectTypeOf(api.listProjects).returns.resolves.toEqualTypeOf<
      ProjectSummary[]
    >();
    expectTypeOf(api.loadTreeChildren).returns.resolves.toEqualTypeOf<
      TreeEntry[]
    >();
    expectTypeOf(api.readFile).returns.resolves.toEqualTypeOf<FilePreview>();
    expectTypeOf(api.listTasks).returns.resolves.toEqualTypeOf<TaskSummary[]>();
    expectTypeOf(api.getChannelHealth).returns.resolves.toEqualTypeOf<
      ChannelHealth
    >();
    expectTypeOf(api.loadChannelEvents).returns.resolves.toEqualTypeOf<
      ChannelEventPage
    >();
    expectTypeOf(api.createSessionConversation).parameters.toEqualTypeOf<
      [string]
    >();
    expectTypeOf(api.createSessionConversation).returns.resolves.toEqualTypeOf<
      SessionConversationCreated
    >();
    expectTypeOf(api.sendChannelMessage).parameters.toEqualTypeOf<
      [SendChannelMessageRequest]
    >();
    expectTypeOf(api.sendChannelMessage).returns.resolves.toEqualTypeOf<
      ChannelMessageAccepted
    >();
    expectTypeOf(api.loadTaskEvents).returns.resolves.toEqualTypeOf<
      TaskEventPage
    >();
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

  it("读取并校验 HTTP Channel 健康响应", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: true,
            channel: "http",
            version: "0.0.1",
            instance_id: "instance-a",
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ok: false,
            channel: "http",
            version: "0.0.1",
            instance_id: "instance-a",
          }),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");

    await expect(api.getChannelHealth()).resolves.toEqual({
      ok: true,
      channel: "http",
      version: "0.0.1",
      instance_id: "instance-a",
    } satisfies ChannelHealth);
    await expect(api.getChannelHealth()).rejects.toThrow(
      "HTTP Channel 健康响应格式无效",
    );
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/channel/health",
      "/api/channel/health",
    ]);
  });

  it("读取并校验 Channel 事件页并编码 cursor", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            instance_id: "instance-a",
            conversation_id: "conversation A",
            events: [
              {
                cursor: 2,
                type: "message.created",
                message_id: "message-a",
                text: "hello",
              },
            ],
            next_cursor: 2,
            oldest_cursor: 1,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            instance_id: "instance-a",
            conversation_id: "other-conversation",
            events: [],
            next_cursor: 2,
            oldest_cursor: 1,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            instance_id: "instance-a",
            conversation_id: "conversation A",
            events: [{ cursor: 0, type: "message.created" }],
            next_cursor: 2,
            oldest_cursor: 1,
          }),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");

    await expect(api.loadChannelEvents("conversation A", 1)).resolves.toEqual({
      instance_id: "instance-a",
      conversation_id: "conversation A",
      events: [
        {
          cursor: 2,
          type: "message.created",
          message_id: "message-a",
          text: "hello",
        },
      ],
      next_cursor: 2,
      oldest_cursor: 1,
    } satisfies ChannelEventPage);
    await expect(api.loadChannelEvents("conversation A", 2)).rejects.toThrow(
      "Channel 事件响应格式无效",
    );
    await expect(api.loadChannelEvents("conversation A", 2)).rejects.toThrow(
      "Channel 事件响应格式无效",
    );
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/channel/events?conversation_id=conversation+A&after=1",
      "/api/channel/events?conversation_id=conversation+A&after=2",
      "/api/channel/events?conversation_id=conversation+A&after=2",
    ]);
  });

  it("创建 Session Conversation 并校验响应身份", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            task_id: "task A",
            conversation_id: "conversation-a",
          }),
          { status: 201 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            task_id: "other-task",
            conversation_id: "conversation-b",
          }),
          { status: 201 },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");

    await expect(
      api.createSessionConversation("task A"),
    ).resolves.toEqual({
      session_id: "task A",
      conversation_id: "conversation-a",
    } satisfies SessionConversationCreated);
    await expect(
      api.createSessionConversation("task A"),
    ).rejects.toThrow("Session Conversation响应格式无效");

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/tasks/task%20A/conversations",
      "/api/tasks/task%20A/conversations",
    ]);
    for (const [, options] of fetch.mock.calls) {
      expect(options).toMatchObject({
        method: "POST",
      });
      expect(options.body).toBeUndefined();
    }
  });

  it("发送 Channel 消息并校验 accepted 响应", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ accepted: true }), { status: 202 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ accepted: false }), { status: 202 }),
      );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");
    const message: SendChannelMessageRequest = {
      conversation_id: "conversation-a",
      message_id: "message-a",
      sender_id: "webui:conversation-a",
      text: "hello",
    };

    await expect(api.sendChannelMessage(message)).resolves.toEqual({
      accepted: true,
    } satisfies ChannelMessageAccepted);
    await expect(api.sendChannelMessage(message)).rejects.toThrow(
      "Channel 消息发送响应格式无效",
    );

    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/channel/messages",
      "/api/channel/messages",
    ]);
    for (const [, options] of fetch.mock.calls) {
      expect(options).toMatchObject({
        method: "POST",
        body: JSON.stringify(message),
      });
      expect(options.headers).toBeInstanceOf(Headers);
      expect(options.headers.get("Content-Type")).toBe("application/json");
    }
  });

  it("读取 Task 历史并编码分页参数", async () => {
    const response = {
      task_id: "项目 A/task",
      events: [
        {
          sequence: 2,
          event: {
            schema_version: 1,
            type: "session.input.accepted",
            event_id: "event-2",
            session_id: "项目 A/task",
            turn_id: "turn-2",
            occurred_at: "2026-08-25T00:00:00Z",
            payload: { text: "hello" },
          },
        },
        { sequence: 0, event: {} },
        {
          sequence: 3,
          event: {
            schema_version: 1,
            type: "session.input.accepted",
            event_id: "event-other",
            session_id: "other-task",
            turn_id: "turn-3",
            occurred_at: "2026-08-25T00:00:00Z",
            payload: { text: "other" },
          },
        },
      ],
      oldest_sequence: 1,
      latest_sequence: 2,
    };
    const fetch = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify(response), {
          status: 200,
        }),
      ),
    );
    vi.stubGlobal("fetch", fetch);
    const api = createApiClient(() => "token-a");

    await expect(
      api.loadTaskEvents("项目 A/task", { before: 6, limit: 2 }),
    ).resolves.toEqual({
      task_id: "项目 A/task",
      events: [
        {
          sequence: 2,
          event: {
            schema_version: 1,
            type: "session.input.accepted",
            event_id: "event-2",
            session_id: "项目 A/task",
            turn_id: "turn-2",
            occurred_at: "2026-08-25T00:00:00Z",
            payload: { text: "hello" },
          },
        },
      ],
      oldest_sequence: 1,
      latest_sequence: 2,
    } satisfies TaskEventPage);
    await expect(
      api.loadTaskEvents("项目 A/task", { after: 2, limit: 3 }),
    ).resolves.toMatchObject({ task_id: "项目 A/task" });
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([
      "/api/tasks/%E9%A1%B9%E7%9B%AE%20A%2Ftask/events?limit=2&before=6",
      "/api/tasks/%E9%A1%B9%E7%9B%AE%20A%2Ftask/events?limit=3&after=2",
    ]);
  });

  it("Task 历史分页响应结构无效时抛出格式错误", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            task_id: "task-a",
            events: [],
            oldest_sequence: 0,
            latest_sequence: null,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            task_id: "other-task",
            events: [],
            oldest_sequence: null,
            latest_sequence: null,
          }),
          { status: 200 },
        ),
      );
    vi.stubGlobal(
      "fetch",
      fetch,
    );
    const api = createApiClient(() => "token-a");

    await expect(api.loadTaskEvents("task-a")).rejects.toThrow(
      "Task 历史响应格式无效",
    );
    await expect(api.loadTaskEvents("task-a")).rejects.toThrow(
      "Task 历史响应格式无效",
    );
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
