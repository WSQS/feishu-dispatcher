import { describe, expect, expectTypeOf, it } from "vitest";

import {
  DISPATCHER_TASK_ID,
  indexTasks,
  taskIsTerminal,
  taskName,
} from "../../feishu_dispatcher/webui/tasks.ts";
import type {
  AgentTaskSummary,
  DispatcherTaskSummary,
  TaskSummary,
} from "../../feishu_dispatcher/webui/api.ts";

const dispatcher: DispatcherTaskSummary = {
  task_id: DISPATCHER_TASK_ID,
  kind: "dispatcher",
  description: "Dispatcher",
  status: "active",
  active: true,
};

const agent: AgentTaskSummary = {
  task_id: "task-a",
  project: "demo",
  agent: "codex",
  description: "Review changes",
  status: "running",
  turns: 2,
  issue_url: null,
  kind: "agent",
  active: true,
};

describe("Task 纯逻辑", () => {
  it("使用稳定的 Dispatcher 标识和名称", () => {
    expect(DISPATCHER_TASK_ID).toBe("dispatcher");
    expect(taskName(dispatcher)).toBe("Dispatcher");
    expect(taskName(null)).toBe("Dispatcher");
  });

  it("格式化 Agent Task 名称并处理缺失项目", () => {
    expect(taskName(agent)).toBe("task-a · demo");
    expect(
      taskName({
        ...agent,
        project: "",
      }),
    ).toBe("task-a · 未命名项目");
  });

  it("只将终止状态视为不可继续发送", () => {
    expect(taskIsTerminal(agent)).toBe(false);
    expect(taskIsTerminal({ ...agent, status: "done" })).toBe(true);
    expect(taskIsTerminal({ ...agent, status: "stopped" })).toBe(true);
    expect(taskIsTerminal(undefined)).toBe(false);
  });

  it("按 task_id 建立索引并保留原始对象", () => {
    const items: TaskSummary[] = [dispatcher, agent];
    const indexed = indexTasks(items);

    expectTypeOf(indexed).toEqualTypeOf<Map<string, TaskSummary>>();
    expect(indexed).toEqual(
      new Map<string, TaskSummary>([
        [dispatcher.task_id, dispatcher],
        [agent.task_id, agent],
      ]),
    );
    expect(indexed.get(agent.task_id)).toBe(agent);
  });
});
