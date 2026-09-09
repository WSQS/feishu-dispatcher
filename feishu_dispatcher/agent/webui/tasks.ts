import type { TaskSummary } from "./api.js";

export const DISPATCHER_TASK_ID = "dispatcher";

const TERMINAL_TASK_STATUSES = new Set(["done", "stopped"]);

export function taskName(task: TaskSummary | null | undefined): string {
  if (!task || task.task_id === DISPATCHER_TASK_ID) {
    return "Dispatcher";
  }
  return `${task.task_id} · ${
    task.kind === "agent" && task.project ? task.project : "未命名项目"
  }`;
}

export function taskIsTerminal(
  task: TaskSummary | null | undefined,
): boolean {
  return TERMINAL_TASK_STATUSES.has(task?.status ?? "");
}

export function indexTasks(
  items: readonly TaskSummary[],
): Map<string, TaskSummary> {
  return new Map(items.map((task) => [task.task_id, task]));
}
