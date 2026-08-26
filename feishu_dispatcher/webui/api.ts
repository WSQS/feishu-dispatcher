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

export interface SessionEventRecord {
  schema_version: 1;
  type: string;
  event_id: string;
  session_id: string;
  turn_id: string | null;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface TaskEventRecord {
  sequence: number;
  event: SessionEventRecord;
}

export interface TaskEventPage {
  task_id: string;
  events: TaskEventRecord[];
  oldest_sequence: number | null;
  latest_sequence: number | null;
}

export interface TaskEventQuery {
  before?: number;
  after?: number;
  limit?: number;
}

export interface ChannelHealth {
  ok: true;
  channel: "http";
  version: string;
  instance_id: string;
}

export interface ChannelEvent {
  cursor: number;
  type: string;
  [key: string]: unknown;
}

export interface ChannelEventPage {
  instance_id: string;
  conversation_id: string;
  events: ChannelEvent[];
  next_cursor: number;
  oldest_cursor: number;
}

export interface CreateTaskConversationRequest {
  conversation_id: string;
}

export interface TaskConversationCreated {
  task_id: string;
  conversation_id: string;
  thread_id: string;
}

export interface SendChannelMessageRequest {
  conversation_id: string;
  message_id: string;
  thread_id: string | null;
  sender_id: string;
  text: string;
}

export interface ChannelMessageAccepted {
  accepted: true;
}

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

function isSequence(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
}

function isSessionEventRecord(value: unknown): value is SessionEventRecord {
  return (
    isRecord(value) &&
    value.schema_version === 1 &&
    isNonEmptyString(value.type) &&
    isNonEmptyString(value.event_id) &&
    isNonEmptyString(value.session_id) &&
    (value.turn_id === null || isNonEmptyString(value.turn_id)) &&
    isNonEmptyString(value.occurred_at) &&
    isRecord(value.payload)
  );
}

function isTaskEventRecord(value: unknown): value is TaskEventRecord {
  return (
    isRecord(value) &&
    isSequence(value.sequence) &&
    isSessionEventRecord(value.event)
  );
}

function isSequenceBoundary(value: unknown): value is number | null {
  return value === null || isSequence(value);
}

function isTaskEventPage(value: unknown): value is TaskEventPage {
  return (
    isRecord(value) &&
    isNonEmptyString(value.task_id) &&
    Array.isArray(value.events) &&
    isSequenceBoundary(value.oldest_sequence) &&
    isSequenceBoundary(value.latest_sequence)
  );
}

function isChannelHealth(value: unknown): value is ChannelHealth {
  return (
    isRecord(value) &&
    value.ok === true &&
    value.channel === "http" &&
    isNonEmptyString(value.version) &&
    isNonEmptyString(value.instance_id)
  );
}

function isCursor(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isChannelEvent(value: unknown): value is ChannelEvent {
  return (
    isRecord(value) &&
    typeof value.type === "string" &&
    value.type.trim().length > 0 &&
    typeof value.cursor === "number" &&
    Number.isSafeInteger(value.cursor) &&
    value.cursor >= 1
  );
}

function isChannelEventPage(value: unknown): value is ChannelEventPage {
  return (
    isRecord(value) &&
    isNonEmptyString(value.instance_id) &&
    isNonEmptyString(value.conversation_id) &&
    Array.isArray(value.events) &&
    value.events.every(isChannelEvent) &&
    isCursor(value.next_cursor) &&
    isCursor(value.oldest_cursor)
  );
}

function isTaskConversationCreated(
  value: unknown,
): value is TaskConversationCreated {
  return (
    isRecord(value) &&
    isNonEmptyString(value.task_id) &&
    isNonEmptyString(value.conversation_id) &&
    isNonEmptyString(value.thread_id)
  );
}

function isChannelMessageAccepted(value: unknown): value is ChannelMessageAccepted {
  return isRecord(value) && value.accepted === true;
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

    async getChannelHealth(): Promise<ChannelHealth> {
      const payload = await request<unknown>("/api/channel/health");
      if (!isChannelHealth(payload)) {
        throw invalidResponse("HTTP Channel 健康");
      }
      return payload;
    },

    async loadChannelEvents(
      conversationId: string,
      after: number,
    ): Promise<ChannelEventPage> {
      const query = new URLSearchParams({
        conversation_id: conversationId,
        after: String(after),
      });
      const payload = await request<unknown>(`/api/channel/events?${query}`);
      if (
        !isChannelEventPage(payload) ||
        payload.conversation_id !== conversationId
      ) {
        throw invalidResponse("Channel 事件");
      }
      return payload;
    },

    async createTaskConversation(
      taskId: string,
      conversationId: string,
    ): Promise<TaskConversationCreated> {
      const body: CreateTaskConversationRequest = {
        conversation_id: conversationId,
      };
      const payload = await request<unknown>(
        `/api/tasks/${encodeURIComponent(taskId)}/conversations`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (
        !isTaskConversationCreated(payload) ||
        payload.task_id !== taskId ||
        payload.conversation_id !== conversationId
      ) {
        throw invalidResponse("Task Conversation");
      }
      return payload;
    },

    async sendChannelMessage(
      message: SendChannelMessageRequest,
    ): Promise<ChannelMessageAccepted> {
      const payload = await request<unknown>("/api/channel/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(message),
      });
      if (!isChannelMessageAccepted(payload)) {
        throw invalidResponse("Channel 消息发送");
      }
      return payload;
    },

    async loadTaskEvents(
      taskId: string,
      {
        before,
        after,
        limit = 100,
      }: TaskEventQuery = {},
    ): Promise<TaskEventPage> {
      const query = new URLSearchParams({ limit: String(limit) });
      if (before !== undefined) {
        query.set("before", String(before));
      }
      if (after !== undefined) {
        query.set("after", String(after));
      }
      const payload = await request<unknown>(
        `/api/tasks/${encodeURIComponent(taskId)}/events?${query}`,
      );
      if (!isTaskEventPage(payload) || payload.task_id !== taskId) {
        throw invalidResponse("Task 历史");
      }
      return {
        task_id: payload.task_id,
        events: payload.events.filter(
          (record) =>
            isTaskEventRecord(record) && record.event.session_id === taskId,
        ),
        oldest_sequence: payload.oldest_sequence,
        latest_sequence: payload.latest_sequence,
      };
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
