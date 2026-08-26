export class ApiError extends Error {
  readonly status: number;
  readonly payload: any;

  constructor(status: number, payload: any) {
    const code = payload?.error || `http_${status}`;
    const detail = payload?.message ? `：${payload.message}` : "";
    super(`${code}${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export interface ProjectSummary {
  name: string;
  path: string;
  default_agent: string;
}

export type TreeEntryType = "file" | "directory";

export interface TreeEntry {
  name: string;
  path: string;
  type: TreeEntryType;
}

export type WorkspaceRevision = "work";

export interface FilePreview {
  path: string;
  rev: WorkspaceRevision;
  binary: boolean;
  content: string;
}

export interface DispatcherTaskSummary {
  task_id: string;
  kind: "dispatcher";
  description: string;
  status: string;
  active: boolean;
}

export interface AgentTaskSummary {
  task_id: string;
  project: string;
  agent: string;
  description: string;
  status: string;
  turns: number;
  issue_url: string | null;
  kind: "agent";
  active: boolean;
}

export type TaskSummary = DispatcherTaskSummary | AgentTaskSummary;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isProjectSummary(value: unknown): value is ProjectSummary {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    typeof value.path === "string" &&
    typeof value.default_agent === "string"
  );
}

function isTreeEntry(value: unknown): value is TreeEntry {
  return (
    isRecord(value) &&
    typeof value.name === "string" &&
    typeof value.path === "string" &&
    (value.type === "file" || value.type === "directory")
  );
}

function isFilePreview(value: unknown): value is FilePreview {
  return (
    isRecord(value) &&
    typeof value.path === "string" &&
    value.rev === "work" &&
    typeof value.binary === "boolean" &&
    typeof value.content === "string"
  );
}

function isDispatcherTaskSummary(value: unknown): value is DispatcherTaskSummary {
  return (
    isRecord(value) &&
    isNonEmptyString(value.task_id) &&
    value.kind === "dispatcher" &&
    typeof value.description === "string" &&
    typeof value.status === "string" &&
    typeof value.active === "boolean"
  );
}

function isAgentTaskSummary(value: unknown): value is AgentTaskSummary {
  return (
    isRecord(value) &&
    isNonEmptyString(value.task_id) &&
    typeof value.project === "string" &&
    typeof value.agent === "string" &&
    typeof value.description === "string" &&
    typeof value.status === "string" &&
    typeof value.turns === "number" &&
    Number.isInteger(value.turns) &&
    value.turns >= 0 &&
    (typeof value.issue_url === "string" || value.issue_url === null) &&
    value.kind === "agent" &&
    typeof value.active === "boolean"
  );
}

function isTaskSummary(value: unknown): value is TaskSummary {
  return isDispatcherTaskSummary(value) || isAgentTaskSummary(value);
}

function invalidResponse(name: string): Error {
  return new Error(`${name}响应格式无效`);
}

export function createApiRequest(getToken: () => string) {
  return async function apiRequest<T = any>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const token = getToken().trim();
    if (!token) {
      throw new Error("请输入 http-channel.token");
    }
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(path, { ...options, headers });
    let payload: any = {};
    try {
      payload = await response.json();
    } catch (_error) {
      payload = {};
    }
    if (!response.ok) {
      throw new ApiError(response.status, payload);
    }
    return payload as T;
  };
}

export function createApiClient(getToken: () => string) {
  const request = createApiRequest(getToken);

  return {
    request,

    async listProjects(): Promise<ProjectSummary[]> {
      const payload = await request<unknown>("/api/projects");
      return isRecord(payload) && Array.isArray(payload.items)
        ? payload.items.filter(isProjectSummary)
        : [];
    },

    async listTasks(): Promise<TaskSummary[]> {
      const payload = await request<unknown>("/api/tasks");
      return isRecord(payload) && Array.isArray(payload.tasks)
        ? payload.tasks.filter(isTaskSummary)
        : [];
    },

    async loadTreeChildren(project: string, path: string): Promise<TreeEntry[]> {
      const query = new URLSearchParams({ path });
      const payload = await request<unknown>(
        `/api/projects/${encodeURIComponent(project)}/tree/children?${query}`,
      );
      return isRecord(payload) && Array.isArray(payload.entries)
        ? payload.entries.filter(isTreeEntry)
        : [];
    },

    async readFile(
      project: string,
      path: string,
      rev: WorkspaceRevision = "work",
    ): Promise<FilePreview> {
      const query = new URLSearchParams({ path, rev });
      const payload = await request<unknown>(
        `/api/projects/${encodeURIComponent(project)}/file?${query}`,
      );
      if (!isFilePreview(payload)) {
        throw invalidResponse("文件");
      }
      return payload;
    },
  };
}
