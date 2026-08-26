// @ts-nocheck
// Compile with `npm run build:webui`; app.js is the deployed artifact.

import { ApiError, createApiClient } from "./api.js";
import {
  storageGet,
  storageKeys,
  storageRemove,
  storageSet,
  storedChannelInstanceId,
  storedConversationId,
  storedCursor,
} from "./storage.js";

const DISPATCHER_TASK_ID = "dispatcher";
const MAX_POLL_RECOVERY_ATTEMPTS = 2;
const TASK_HISTORY_LIMIT = 100;
const TASK_POLL_INTERVAL_MS = 2000;
const TERMINAL_TASK_STATUSES = new Set(["done", "stopped"]);

const elements = Object.freeze({
  composer: document.querySelector("#composer"),
  composerTarget: document.querySelector("#composer-target"),
  connect: document.querySelector("#connect"),
  connectionSettings: document.querySelector("#connection-settings"),
  conversationId: document.querySelector("#conversation-id"),
  currentTask: document.querySelector("#current-task"),
  currentThread: document.querySelector("#current-thread"),
  cursor: document.querySelector("#cursor"),
  filePreview: document.querySelector("#file-preview"),
  fileTree: document.querySelector("#file-tree"),
  message: document.querySelector("#message"),
  projectSelect: document.querySelector("#project-select"),
  resetConversation: document.querySelector("#reset-conversation"),
  send: document.querySelector("#send"),
  status: document.querySelector("#status"),
  taskList: document.querySelector("#task-list"),
  timelines: document.querySelector("#timelines"),
  token: document.querySelector("#token"),
});

const api = createApiClient(() => elements.token.value);
const apiRequest = api.request;

const outputs = new Map();
const tasks = new Map();
const taskThreads = new Map();
const targetTasks = new Map();
const taskTraceStates = new Map();
const taskTimelines = new Map();
let conversationId = storedConversationId(() => newId("webui-conversation"));
let cursor = storedCursor(conversationId);
let renderedCursor = 0;
let conversationStarted = storageGet(storageKeys.started(conversationId)) === "1";
let channelInstanceId = storedChannelInstanceId();
let connected = false;
let pollGeneration = 0;
let selectedTaskId = DISPATCHER_TASK_ID;
let statusRevision = 0;
let statusSource = null;
let taskHistoryGeneration = 0;
let taskPollGeneration = 0;
let taskRequestTail = Promise.resolve();
let taskSelectionBusy = false;
let taskSnapshot = null;

let selectedProject = null;
const treeChildrenCache = new Map();
const expandedDirs = new Set();
let selectedFilePath = null;

function newId(prefix) {
  if (globalThis.crypto?.randomUUID) {
    return `${prefix}-${globalThis.crypto.randomUUID()}`;
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  const suffix = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join(
    "",
  );
  return `${prefix}-${suffix}`;
}

function setStatus(text, tone = "idle", source = null) {
  statusRevision += 1;
  statusSource = source;
  elements.status.textContent = text;
  elements.status.title = text;
  elements.status.dataset.tone = tone;
}

function taskName(task) {
  if (!task || task.task_id === DISPATCHER_TASK_ID) {
    return "Dispatcher";
  }
  return `${task.task_id} · ${task.project || "未命名项目"}`;
}

function taskIsTerminal(task) {
  return TERMINAL_TASK_STATUSES.has(task?.status);
}

function renderMetadata() {
  elements.conversationId.textContent = conversationId;
  elements.cursor.textContent = String(cursor);
}

function createEmptyState(taskId) {
  const empty = document.createElement("div");
  empty.className = "empty-state";
  const title = document.createElement("p");
  const detail = document.createElement("span");
  if (taskId === DISPATCHER_TASK_ID) {
    title.textContent = "还没有 Dispatcher 事件。";
    detail.textContent = "发送 /help 开始。";
  } else {
    title.textContent = "这个 Task 还没有页面事件。";
    detail.textContent = "发送消息后，输入与流式输出会显示在这里。";
  }
  empty.append(title, detail);
  return empty;
}

function ensureTimeline(taskId) {
  const existing = taskTimelines.get(taskId);
  if (existing) {
    return existing;
  }
  const timeline = document.createElement("div");
  timeline.className = "task-timeline";
  timeline.dataset.taskId = taskId;
  timeline.hidden = taskId !== selectedTaskId;
  const historyLoad = document.createElement("button");
  historyLoad.type = "button";
  historyLoad.className = "history-load";
  historyLoad.hidden = true;
  historyLoad.textContent = "加载更早历史";
  historyLoad.addEventListener("click", () => {
    const generation = taskHistoryGeneration;
    void loadEarlierTaskHistory(taskId, generation).catch((error) => {
      if (generation === taskHistoryGeneration && selectedTaskId === taskId) {
        showError(error);
      }
    });
  });
  timeline.append(historyLoad, createEmptyState(taskId));
  timeline.addEventListener("scroll", () => {
    if (
      timeline.hidden ||
      timeline.scrollTop > 24 ||
      taskId === DISPATCHER_TASK_ID
    ) {
      return;
    }
    const generation = taskHistoryGeneration;
    void loadEarlierTaskHistory(taskId, generation).catch((error) => {
      if (generation === taskHistoryGeneration && selectedTaskId === taskId) {
        showError(error);
      }
    });
  });
  elements.timelines.append(timeline);
  taskTimelines.set(taskId, timeline);
  return timeline;
}

function renderSelectedTask() {
  const task = tasks.get(selectedTaskId);
  const readOnly = taskIsTerminal(task);
  const threadId =
    selectedTaskId === DISPATCHER_TASK_ID
      ? null
      : taskThreads.get(selectedTaskId) || null;
  elements.currentTask.textContent = taskName(task);
  elements.currentThread.textContent = threadId || "root";
  elements.composerTarget.textContent = readOnly
    ? `${taskName(task)} · 历史只读`
    : `发送给 ${taskName(task)}`;
  elements.message.disabled = readOnly;
  elements.send.disabled = readOnly;
  for (const [taskId, timeline] of taskTimelines) {
    timeline.hidden = taskId !== selectedTaskId;
  }
  scrollTimeline(selectedTaskId);
  syncProjectFromTask();
}

function revealTimeline(taskId) {
  ensureTimeline(taskId).querySelector(".empty-state")?.remove();
}

function updateHistoryLoad(taskId) {
  const timeline = ensureTimeline(taskId);
  const button = timeline.querySelector(".history-load");
  const state = traceState(taskId);
  if (!button) {
    return;
  }
  button.hidden = state.exhausted || state.oldestLoaded === null;
  button.disabled = state.loadingCount > 0;
  button.textContent = state.loadingCount > 0 ? "加载中…" : "加载更早历史";
}

function scrollTimeline(taskId) {
  const timeline = taskTimelines.get(taskId);
  if (timeline && !timeline.hidden) {
    timeline.scrollTop = timeline.scrollHeight;
  }
}

function createEventArticle({
  role = "assistant",
  label,
  text,
  detail = "",
}) {
  const article = document.createElement("article");
  article.className = "event";
  article.dataset.role = role;

  const meta = document.createElement("div");
  meta.className = "event-meta";
  const name = document.createElement("span");
  name.textContent = label;
  const extra = document.createElement("span");
  extra.textContent = detail;
  meta.append(name, extra);

  const content = document.createElement("p");
  content.className = "event-text";
  content.textContent = text;
  article.append(meta, content);
  return article;
}

function appendEvent({
  taskId = selectedTaskId,
  role = "assistant",
  label,
  text,
  detail = "",
}) {
  revealTimeline(taskId);
  const article = createEventArticle({ role, label, text, detail });
  ensureTimeline(taskId).append(article);
  scrollTimeline(taskId);
}

function ensureOutput(event, taskId) {
  const existing = outputs.get(event.output_id);
  if (existing) {
    return existing;
  }

  revealTimeline(taskId);
  const article = document.createElement("article");
  article.className = "event output";
  article.dataset.role = "assistant";

  const header = document.createElement("div");
  header.className = "output-header";
  const title = document.createElement("span");
  title.className = "output-title";
  title.textContent = event.title || "Agent output";
  const status = document.createElement("span");
  status.textContent = event.status || "running";
  header.append(title, status);

  const body = document.createElement("pre");
  body.className = "output-body";
  const footer = document.createElement("p");
  footer.className = "output-footer";
  footer.textContent = event.footer || "";
  article.append(header, body, footer);
  ensureTimeline(taskId).append(article);

  const output = { article, body, footer, status, taskId };
  outputs.set(event.output_id, output);
  scrollTimeline(taskId);
  return output;
}

function rememberTarget(targetId, taskId) {
  if (typeof targetId === "string" && targetId) {
    targetTasks.set(targetId, taskId);
  }
}

function taskForEvent(event) {
  if (event.type === "output.delta" || event.type === "output.updated") {
    return outputs.get(event.output_id)?.taskId || DISPATCHER_TASK_ID;
  }
  if (event.type === "session.event") {
    const sessionId = event.event?.session_id;
    if (typeof sessionId === "string" && sessionId) {
      return sessionId;
    }
  }
  const targetIds = [event.target_id, event.thread_id, event.output_id, event.message_id];
  for (const targetId of targetIds) {
    const taskId = targetTasks.get(targetId);
    if (taskId) {
      return taskId;
    }
  }
  return DISPATCHER_TASK_ID;
}

function renderEvent(event) {
  const taskId = taskForEvent(event);
  if (
    event.type === "session.event" &&
    Number.isSafeInteger(event.trace_sequence) &&
    !claimTraceSequence(taskId, event.trace_sequence)
  ) {
    return;
  }
  switch (event.type) {
    case "message.created":
      rememberTarget(event.message_id, taskId);
      appendEvent({
        taskId,
        label: event.threaded ? "Thread reply" : "Reply",
        text: event.text || "",
        detail: `cursor ${event.cursor}`,
      });
      break;
    case "thread.created":
      rememberTarget(event.thread_id, taskId);
      appendEvent({
        taskId,
        role: "system",
        label: "Thread created",
        text: event.text || "",
        detail: event.thread_id || "",
      });
      break;
    case "output.started":
      rememberTarget(event.output_id, taskId);
      ensureOutput(event, taskId);
      break;
    case "output.delta": {
      const output = ensureOutput(event, taskId);
      output.body.textContent += event.text || "";
      scrollTimeline(output.taskId);
      break;
    }
    case "output.updated": {
      const output = ensureOutput(event, taskId);
      output.footer.textContent = event.footer || "";
      output.status.textContent = event.status || "running";
      output.article.dataset.status = event.status || "running";
      break;
    }
    case "session.event": {
      const sessionEvent = event.event;
      if (!sessionEvent || typeof sessionEvent.type !== "string") {
        appendEvent({
          taskId,
          role: "system",
          label: "Session event",
          text: "收到无法识别的 SessionEvent。",
          detail: `cursor ${event.cursor ?? "?"}`,
        });
        break;
      }
      const presentation = event.presentation;
      if (sessionEvent.type === "session.input.accepted") {
        break;
      }
      if (sessionEvent.type === "agent.output.started") {
        if (presentation?.output_id) {
          rememberTarget(presentation.output_id, taskId);
          ensureOutput(presentation, taskId);
        }
        break;
      }
      if (
        sessionEvent.type === "agent.output.delta" ||
        sessionEvent.type === "agent.plan.updated" ||
        sessionEvent.type === "tool.call.observed"
      ) {
        if (presentation?.output_id) {
          const output = ensureOutput(presentation, taskId);
          output.body.textContent += presentation.text || "";
          scrollTimeline(output.taskId);
        }
        break;
      }
      if (sessionEvent.type === "agent.output.finished") {
        if (presentation?.output_id) {
          const output = ensureOutput(presentation, taskId);
          output.body.textContent += presentation.text || "";
          output.footer.textContent = presentation.footer || "";
          output.status.textContent = presentation.status || "running";
          output.article.dataset.status = presentation.status || "running";
          scrollTimeline(output.taskId);
        }
        break;
      }
      appendEvent({
        taskId,
        role: "system",
        label: "Session event",
        text: sessionEvent.type,
        detail: `cursor ${event.cursor ?? "?"}`,
      });
      break;
    }
    default:
      appendEvent({
        taskId,
        role: "system",
        label: event.type || "Unknown event",
        text: JSON.stringify(event),
        detail: `cursor ${event.cursor ?? "?"}`,
      });
  }
}

function traceState(taskId) {
  let state = taskTraceStates.get(taskId);
  if (!state) {
    state = {
      exhausted: false,
      loadingCount: 0,
      oldestLoaded: null,
      finishedTurns: new Set(),
      seenSequences: new Set(),
    };
    taskTraceStates.set(taskId, state);
  }
  return state;
}

function claimTraceSequence(taskId, sequence) {
  const state = traceState(taskId);
  if (state.seenSequences.has(sequence)) {
    return false;
  }
  state.seenSequences.add(sequence);
  state.oldestLoaded =
    state.oldestLoaded === null ? sequence : Math.min(state.oldestLoaded, sequence);
  return true;
}

function traceRecordArticle(record, state) {
  const event = record?.event;
  const payload = event?.payload;
  if (
    !Number.isSafeInteger(record?.sequence) ||
    !event ||
    typeof event.type !== "string" ||
    !payload ||
    typeof payload !== "object"
  ) {
    return null;
  }
  const detail = `seq ${record.sequence}`;
  switch (event.type) {
    case "session.input.accepted": {
      const source = payload.source?.channel_key;
      return createEventArticle({
        role: "user",
        label: typeof source === "string" && source ? source : "User",
        text: typeof payload.text === "string" ? payload.text : "",
        detail,
      });
    }
    case "agent.output.finished":
      if (typeof event.turn_id === "string" && event.turn_id) {
        state.finishedTurns.add(event.turn_id);
      }
      return createEventArticle({
        label: "Agent",
        text: typeof payload.message === "string" ? payload.message : "",
        detail: `${payload.outcome || "completed"} · ${detail}`,
      });
    case "agent.output.delta":
      if (
        payload.stream !== "message" ||
        (typeof event.turn_id === "string" && state.finishedTurns.has(event.turn_id))
      ) {
        return null;
      }
      return createEventArticle({
        label: "Agent",
        text: typeof payload.text === "string" ? payload.text : "",
        detail: detail,
      });
    case "agent.plan.updated": {
      const marks = {
        completed: "☑️",
        in_progress: "🔄",
        pending: "⬜",
      };
      const entries = Array.isArray(payload.entries) ? payload.entries : [];
      return createEventArticle({
        role: "system",
        label: "Plan",
        text: entries
          .map((entry) => `${marks[entry?.status] || "•"} ${entry?.content || ""}`)
          .join("\n"),
        detail,
      });
    }
    case "tool.call.observed": {
      const icons = { completed: "✅", failed: "❌", started: "🔧" };
      const title = typeof payload.title === "string" ? payload.title : "Tool call";
      const toolDetail =
        typeof payload.detail === "string" && payload.detail !== title
          ? `：${payload.detail}`
          : "";
      return createEventArticle({
        role: "system",
        label: "Tool",
        text: `${icons[payload.status] || "🔧"} ${title}${toolDetail}`,
        detail: `${payload.status || "unknown"} · ${detail}`,
      });
    }
    case "session.state.changed":
      return createEventArticle({
        role: "system",
        label: "Session",
        text: `${payload.previous_state || "unknown"} → ${
          payload.current_state || "unknown"
        }`,
        detail,
      });
    case "session.error.occurred":
      return createEventArticle({
        role: "system",
        label: "Error",
        text: typeof payload.message === "string" ? payload.message : "",
        detail: `${payload.phase || "unknown"} · ${detail}`,
      });
    default:
      return null;
  }
}

function renderTraceRecords(taskId, records, { preserveScroll = false } = {}) {
  const timeline = ensureTimeline(taskId);
  const state = traceState(taskId);
  for (const record of records) {
    if (record?.event?.type === "agent.output.finished") {
      const turnId = record.event.turn_id;
      if (typeof turnId === "string" && turnId) {
        state.finishedTurns.add(turnId);
      }
    }
  }
  const fragment = document.createDocumentFragment();
  for (const record of records) {
    if (
      !Number.isSafeInteger(record?.sequence) ||
      !claimTraceSequence(taskId, record.sequence)
    ) {
      continue;
    }
    const article = traceRecordArticle(record, state);
    if (article) {
      fragment.append(article);
    }
  }
  if (!fragment.childNodes.length) {
    return;
  }
  revealTimeline(taskId);
  const previousHeight = timeline.scrollHeight;
  timeline.querySelector(".history-load")?.after(fragment);
  if (preserveScroll) {
    timeline.scrollTop += timeline.scrollHeight - previousHeight;
  } else {
    scrollTimeline(taskId);
  }
  updateHistoryLoad(taskId);
}

async function loadTaskHistory(
  taskId,
  { before = null, generation = taskHistoryGeneration } = {},
) {
  if (taskId === DISPATCHER_TASK_ID) {
    return;
  }
  const state = traceState(taskId);
  if (
    before !== null &&
    (state.loadingCount > 0 || state.exhausted)
  ) {
    return;
  }
  state.loadingCount += 1;
  updateHistoryLoad(taskId);
  try {
    const payload = await api.loadTaskEvents(taskId, {
      before: before ?? undefined,
      limit: TASK_HISTORY_LIMIT,
    });
    if (generation !== taskHistoryGeneration || selectedTaskId !== taskId) {
      return;
    }
    const records = payload.events;
    renderTraceRecords(taskId, records, { preserveScroll: before !== null });
    const pageSequences = records
      .map((record) => record?.sequence)
      .filter((sequence) => Number.isSafeInteger(sequence));
    const firstSequence = pageSequences.length ? Math.min(...pageSequences) : null;
    state.exhausted =
      firstSequence === null ||
      payload.oldest_sequence === null ||
      firstSequence <= payload.oldest_sequence;
  } finally {
    state.loadingCount -= 1;
    updateHistoryLoad(taskId);
  }
}

async function loadEarlierTaskHistory(taskId, generation) {
  const state = traceState(taskId);
  if (state.exhausted || state.oldestLoaded === null) {
    return;
  }
  await loadTaskHistory(taskId, {
    before: state.oldestLoaded,
    generation,
  });
}

function renderTaskList() {
  elements.taskList.replaceChildren();
  elements.resetConversation.disabled = taskSelectionBusy;
  if (tasks.size === 0) {
    const empty = document.createElement("p");
    empty.className = "task-list-empty";
    empty.textContent = "连接后加载 Task。";
    elements.taskList.append(empty);
    return;
  }

  for (const task of tasks.values()) {
    if (task.task_id !== DISPATCHER_TASK_ID && task.status === "stopped") {
      continue;
    }
    const terminal = taskIsTerminal(task);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "task-item";
    button.dataset.selected = String(task.task_id === selectedTaskId);
    button.dataset.terminal = String(terminal);
    button.disabled = taskSelectionBusy;
    button.setAttribute("aria-pressed", String(task.task_id === selectedTaskId));

    const header = document.createElement("span");
    header.className = "task-item-header";
    const name = document.createElement("span");
    name.className = "task-name";
    name.textContent = taskName(task);
    const status = document.createElement("span");
    status.className = "task-status";
    status.textContent = task.status || "unknown";
    header.append(name, status);

    const description = document.createElement("span");
    description.className = "task-description";
    description.textContent = task.description || "无描述";

    const detail = document.createElement("span");
    detail.className = "task-detail";
    const identity = document.createElement("span");
    identity.textContent =
      task.task_id === DISPATCHER_TASK_ID
        ? "root conversation"
        : `${task.agent || "agent"} · ${task.turns ?? 0} turns`;
    const binding = document.createElement("span");
    binding.className = "task-binding";
    if (task.task_id === DISPATCHER_TASK_ID) {
      binding.textContent = "根会话";
    } else if (terminal) {
      binding.textContent = "查看历史";
    } else if (taskThreads.has(task.task_id)) {
      binding.textContent = "已打开";
    } else {
      binding.textContent = "点击打开";
    }
    detail.append(identity, binding);

    button.append(header, description, detail);
    button.addEventListener("click", () => {
      void selectTask(task.task_id).catch(showError);
    });
    elements.taskList.append(button);
  }
}

function normalizeTasks(items) {
  const normalized = new Map();
  for (const task of items) {
    if (task && typeof task.task_id === "string" && task.task_id) {
      normalized.set(task.task_id, {
        task_id: task.task_id,
        project: task.project ?? null,
        agent: task.agent ?? null,
        description: task.description ?? null,
        status: task.status ?? null,
        turns: task.turns ?? null,
        issue_url: task.issue_url ?? null,
        kind: task.kind ?? null,
        active: task.active ?? null,
      });
    }
  }
  return normalized;
}

function applyTasks(nextTasks) {
  if (!nextTasks.has(DISPATCHER_TASK_ID)) {
    throw new Error("Task 列表缺少 Dispatcher");
  }
  const nextSnapshot = JSON.stringify([...nextTasks.values()]);
  if (nextSnapshot === taskSnapshot) {
    return false;
  }

  tasks.clear();
  for (const [taskId, task] of nextTasks) {
    tasks.set(taskId, task);
  }
  taskSnapshot = nextSnapshot;
  const selected = tasks.get(selectedTaskId);
  if (!selected) {
    selectedTaskId = DISPATCHER_TASK_ID;
  }
  renderTaskList();
  ensureTimeline(selectedTaskId);
  renderSelectedTask();
  return true;
}

async function fetchTasks() {
  const request = taskRequestTail.then(() => api.listTasks());
  taskRequestTail = request.catch(() => {});
  return normalizeTasks(await request);
}

async function loadTasks() {
  applyTasks(await fetchTasks());
}

function clearChannelRuntimeState() {
  pollGeneration += 1;
  taskHistoryGeneration += 1;
  storageRemove(storageKeys.cursor(conversationId));
  storageRemove(storageKeys.started(conversationId));
  cursor = 0;
  renderedCursor = 0;
  conversationStarted = false;
  selectedTaskId = DISPATCHER_TASK_ID;
  outputs.clear();
  taskThreads.clear();
  targetTasks.clear();
  ensureTimeline(selectedTaskId);
  renderTaskList();
  renderSelectedTask();
  renderMetadata();
}

function acceptChannelInstance(payload, required = true) {
  const raw = payload?.instance_id;
  if (typeof raw !== "string" || !raw.trim()) {
    if (required) {
      throw new Error("HTTP Channel 未返回 instance_id");
    }
    return "missing";
  }
  const nextInstanceId = raw.trim();
  if (nextInstanceId === channelInstanceId) {
    return "same";
  }
  const result = channelInstanceId === null ? "initial" : "changed";
  channelInstanceId = nextInstanceId;
  storageSet(storageKeys.channelInstance, channelInstanceId);
  clearChannelRuntimeState();
  return result;
}

async function refreshChannelInstance() {
  return acceptChannelInstance(await api.getChannelHealth());
}

async function loadProjects() {
  const items = await api.listProjects();
  const previous = elements.projectSelect.value;
  elements.projectSelect.replaceChildren();
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "未选择项目";
  elements.projectSelect.append(none);
  for (const item of items) {
    if (!item || typeof item.name !== "string" || !item.name) {
      continue;
    }
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = item.name;
    elements.projectSelect.append(option);
  }
  if (selectedProject) {
    elements.projectSelect.value = selectedProject;
  } else if (previous && previous !== "") {
    elements.projectSelect.value = previous;
  }
}

function syncProjectFromTask() {
  const task = tasks.get(selectedTaskId);
  const project = task?.project || null;
  if (project && project !== selectedProject) {
    void setProject(project);
  }
}

function treeChildrenKey(dirPath) {
  return `${selectedProject ?? ""}\u0000${dirPath}`;
}

async function loadChildren(dirPath) {
  if (!selectedProject) {
    return;
  }
  treeChildrenCache.set(
    treeChildrenKey(dirPath),
    await api.loadTreeChildren(selectedProject, dirPath),
  );
}

function renderEntry(entry) {
  const path = entry.path;
  const node = document.createElement("div");
  node.className = "tree-node";

  const row = document.createElement("button");
  row.type = "button";
  row.className = "tree-row";
  row.dataset.type = entry.type;
  row.dataset.path = path;

  const name = document.createElement("span");
  name.className = "tree-name";
  name.textContent = entry.name;

  if (entry.type === "directory") {
    const toggle = document.createElement("span");
    toggle.className = "tree-toggle";
    toggle.setAttribute("aria-hidden", "true");
    if (expandedDirs.has(path)) {
      toggle.dataset.expanded = "true";
    }
    row.append(toggle, name);
    row.addEventListener("click", () => {
      void toggleDir(path);
    });
  } else {
    row.dataset.selected = String(path === selectedFilePath);
    row.append(name);
    row.addEventListener("click", () => {
      void openFile(path);
    });
  }

  node.append(row);

  if (entry.type === "directory" && expandedDirs.has(path)) {
    const childBox = document.createElement("div");
    childBox.className = "tree-children";
    const children = treeChildrenCache.get(treeChildrenKey(path));
    if (children) {
      for (const child of children) {
        childBox.append(renderEntry(child));
      }
    } else {
      const loading = document.createElement("p");
      loading.className = "file-tree-empty";
      loading.textContent = "加载中…";
      childBox.append(loading);
    }
    node.append(childBox);
  }
  return node;
}

function renderTree() {
  elements.fileTree.replaceChildren();
  if (!selectedProject) {
    const empty = document.createElement("p");
    empty.className = "file-tree-empty";
    empty.textContent = "连接后选择项目，浏览文件。";
    elements.fileTree.append(empty);
    return;
  }
  const rootChildren = treeChildrenCache.get(treeChildrenKey(""));
  if (!rootChildren) {
    const empty = document.createElement("p");
    empty.className = "file-tree-empty";
    empty.textContent = "选择项目后加载文件树。";
    elements.fileTree.append(empty);
    return;
  }
  const rootBox = document.createElement("div");
  rootBox.className = "tree-children";
  for (const entry of rootChildren) {
    rootBox.append(renderEntry(entry));
  }
  elements.fileTree.append(rootBox);
}

async function setProject(name) {
  const normalized = name || null;
  const changed = normalized !== selectedProject;
  selectedProject = normalized;
  treeChildrenCache.clear();
  expandedDirs.clear();
  selectedFilePath = null;
  elements.projectSelect.value = normalized || "";
  renderTree();
  renderFilePreviewPlaceholder();
  if (!changed || !normalized) {
    return;
  }
  elements.fileTree.replaceChildren();
  const loading = document.createElement("p");
  loading.className = "file-tree-empty";
  loading.textContent = "加载中…";
  elements.fileTree.append(loading);
  try {
    await loadChildren("");
    renderTree();
  } catch (error) {
    elements.fileTree.replaceChildren();
    const failed = document.createElement("p");
    failed.className = "preview-error";
    failed.textContent = error.message || "加载文件树失败";
    elements.fileTree.append(failed);
  }
}

async function toggleDir(path) {
  if (!selectedProject) {
    return;
  }
  if (expandedDirs.has(path)) {
    expandedDirs.delete(path);
    renderTree();
    return;
  }
  expandedDirs.add(path);
  if (!treeChildrenCache.has(treeChildrenKey(path))) {
    try {
      await loadChildren(path);
    } catch (error) {
      expandedDirs.delete(path);
      showError(error);
      renderTree();
      return;
    }
  }
  renderTree();
}

function renderFilePreviewPlaceholder() {
  elements.filePreview.replaceChildren();
  const placeholder = document.createElement("p");
  placeholder.className = "preview-placeholder";
  placeholder.textContent = "在文件树中选择文件查看内容。";
  elements.filePreview.append(placeholder);
}

function renderFilePreview(state) {
  elements.filePreview.replaceChildren();
  if (state.loading) {
    const loading = document.createElement("p");
    loading.className = "preview-placeholder";
    loading.textContent = `加载 ${state.path}…`;
    elements.filePreview.append(loading);
    return;
  }
  if (state.error) {
    const failed = document.createElement("p");
    failed.className = "preview-error";
    failed.textContent = state.error;
    elements.filePreview.append(failed);
    return;
  }
  const header = document.createElement("div");
  header.className = "preview-header";
  const code = document.createElement("code");
  code.textContent = state.path;
  const meta = document.createElement("span");
  if (state.binary) {
    meta.textContent = "binary";
  } else {
    const lines = state.content.split("\n").length;
    meta.textContent = `${lines} 行`;
  }
  header.append(code, meta);
  const body = document.createElement("pre");
  body.className = "preview-body";
  body.textContent = state.binary ? "（二进制文件，不可预览）" : state.content || "（空文件）";
  elements.filePreview.append(header, body);
}

async function openFile(path) {
  if (!selectedProject) {
    return;
  }
  selectedFilePath = path;
  renderTree();
  renderFilePreview({ loading: true, path });
  try {
    const payload = await api.readFile(selectedProject, path);
    renderFilePreview(payload);
  } catch (error) {
    renderFilePreview({ error: error.message || "加载文件失败" });
  }
}

async function connect() {
  taskPollGeneration += 1;
  setStatus("正在验证 token…", "busy");
  const instanceState = await refreshChannelInstance();
  await loadTasks();
  await loadProjects();
  connected = true;
  setStatus(
    instanceState === "changed"
      ? "已连接 · 服务已重启，请重新打开 Task"
      : "已连接",
    "ok",
  );
  elements.connectionSettings.open = false;
  startTaskPolling();
  if (conversationStarted) {
    startPolling();
  }
  return instanceState;
}

async function createTaskConversation(task) {
  await refreshChannelInstance();
  const payload = await api.createTaskConversation(
    task.task_id,
    conversationId,
  );
  const threadId = payload.thread_id.trim();
  taskThreads.set(task.task_id, threadId);
  rememberTarget(threadId, task.task_id);
  conversationStarted = true;
  storageSet(storageKeys.started(conversationId), "1");
  return threadId;
}

async function selectTask(taskId) {
  const task = tasks.get(taskId);
  if (!task || taskSelectionBusy) {
    return;
  }

  const historyGeneration = ++taskHistoryGeneration;
  taskSelectionBusy = true;
  renderTaskList();
  let pollingPaused = false;
  try {
    if (
      taskId !== DISPATCHER_TASK_ID &&
      !taskIsTerminal(task) &&
      !taskThreads.has(taskId)
    ) {
      pollGeneration += 1;
      pollingPaused = true;
      setStatus(`正在打开 ${taskName(task)}…`, "busy");
      await createTaskConversation(task);
    }
    selectedTaskId = taskId;
    ensureTimeline(taskId);
    renderSelectedTask();
    setStatus(`正在加载 ${taskName(task)} 的历史…`, "busy");
  } finally {
    taskSelectionBusy = false;
    renderTaskList();
    if (pollingPaused && conversationStarted) {
      startPolling();
    }
  }
  try {
    await loadTaskHistory(taskId, { generation: historyGeneration });
  } catch (error) {
    if (
      historyGeneration === taskHistoryGeneration &&
      selectedTaskId === taskId
    ) {
      throw error;
    }
    return;
  }
  if (
    historyGeneration !== taskHistoryGeneration ||
    selectedTaskId !== taskId
  ) {
    return;
  }
  setStatus(
    taskIsTerminal(task)
      ? `已打开 ${taskName(task)} 的历史`
      : `已选择 ${taskName(task)}`,
    "ok",
  );
}

function persistCursor() {
  storageSet(storageKeys.cursor(conversationId), String(cursor));
  renderMetadata();
}

async function pollOnce(generation) {
  const targetConversationId = conversationId;
  const payload = await api.loadChannelEvents(targetConversationId, cursor);
  if (generation !== pollGeneration || targetConversationId !== conversationId) {
    return false;
  }
  const instanceState = acceptChannelInstance(payload);
  if (instanceState !== "same") {
    if (instanceState === "changed") {
      setStatus("服务已重启，请重新打开 Task", "busy");
    }
    return false;
  }
  for (const event of payload.events || []) {
    if (Number.isSafeInteger(event.cursor)) {
      if (event.cursor > renderedCursor) {
        renderEvent(event);
        renderedCursor = event.cursor;
      }
      if (event.cursor > cursor) {
        cursor = event.cursor;
      }
    } else {
      renderEvent(event);
    }
  }
  if (Number.isSafeInteger(payload.next_cursor) && payload.next_cursor > cursor) {
    cursor = payload.next_cursor;
  }
  persistCursor();
  setStatus(`已连接 · cursor ${cursor}`, "ok");
  return true;
}

function wait(milliseconds) {
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}

function showError(error, source = null) {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, "error", source);
}

function recoverPollingError(error) {
  if (!(error instanceof ApiError)) {
    return "unhandled";
  }
  const errorCode = error.payload?.error;
  if (
    !["unknown_conversation", "cursor_invalid", "cursor_expired"].includes(
      errorCode,
    )
  ) {
    return "unhandled";
  }
  const instanceState = acceptChannelInstance(error.payload, false);
  if (instanceState === "changed" || instanceState === "initial") {
    if (instanceState === "changed") {
      setStatus("服务已重启，请重新打开 Task", "busy");
    }
    return "handled";
  }
  switch (errorCode) {
    case "unknown_conversation":
      clearChannelRuntimeState();
      setStatus("Conversation 已失效，请重新打开 Task", "busy");
      return "handled";
    case "cursor_invalid": {
      const latestCursor = error.payload.latest_cursor;
      if (!Number.isSafeInteger(latestCursor) || latestCursor < 0) {
        return "unhandled";
      }
      cursor = 0;
      persistCursor();
      setStatus(`cursor 已重置（服务端最新 ${latestCursor}）`, "busy");
      return "retry";
    }
    case "cursor_expired": {
      const oldestCursor = error.payload.oldest_cursor;
      if (!Number.isSafeInteger(oldestCursor) || oldestCursor < 1) {
        return "unhandled";
      }
      cursor = oldestCursor - 1;
      persistCursor();
      appendEvent({
        role: "system",
        label: "Event stream",
        text: `较早事件已被淘汰，从 cursor ${cursor} 继续。`,
      });
      setStatus("较早事件已截断，正在继续接收", "busy");
      return "retry";
    }
    default:
      return "unhandled";
  }
}

function startPolling() {
  const generation = ++pollGeneration;
  const loop = async () => {
    let recoveryAttempts = 0;
    while (generation === pollGeneration && connected && conversationStarted) {
      try {
        if (!(await pollOnce(generation))) {
          return;
        }
        recoveryAttempts = 0;
      } catch (error) {
        if (generation !== pollGeneration) {
          return;
        }
        const recovery = recoverPollingError(error);
        if (
          recovery === "retry" &&
          recoveryAttempts < MAX_POLL_RECOVERY_ATTEMPTS
        ) {
          recoveryAttempts += 1;
          continue;
        }
        if (recovery === "handled") {
          return;
        }
        showError(
          recovery === "retry"
            ? new Error("事件 cursor 自动恢复次数已达上限")
            : error,
        );
        return;
      }
      await wait(700);
    }
  };
  void loop();
}

async function pollTasksOnce(generation) {
  const nextTasks = await fetchTasks();
  if (generation !== taskPollGeneration || taskSelectionBusy) {
    return;
  }
  applyTasks(nextTasks);
  if (statusSource === "task-poll") {
    setStatus(conversationStarted ? `已连接 · cursor ${cursor}` : "已连接", "ok");
  }
}

function startTaskPolling() {
  const generation = ++taskPollGeneration;
  const loop = async () => {
    while (generation === taskPollGeneration && connected) {
      await wait(TASK_POLL_INTERVAL_MS);
      if (generation !== taskPollGeneration || !connected) {
        return;
      }
      if (taskSelectionBusy) {
        continue;
      }
      const statusRevisionAtStart = statusRevision;
      try {
        await pollTasksOnce(generation);
      } catch (error) {
        if (
          generation === taskPollGeneration &&
          statusRevision === statusRevisionAtStart &&
          (statusSource === "task-poll" ||
            elements.status.dataset.tone !== "error")
        ) {
          showError(error, "task-poll");
        }
      }
    }
  };
  void loop();
}

async function sendMessage(text) {
  const requestedTaskId = selectedTaskId;
  let instanceState = "same";
  if (taskSelectionBusy) {
    throw new Error("正在打开 Task Conversation，请稍候");
  }
  if (!connected) {
    instanceState = await connect();
  } else {
    instanceState = await refreshChannelInstance();
  }
  if (
    instanceState !== "same" &&
    requestedTaskId !== DISPATCHER_TASK_ID
  ) {
    throw new Error("HTTP Channel 运行态已刷新，请重新打开 Task 后再发送");
  }
  if (taskSelectionBusy || selectedTaskId !== requestedTaskId) {
    throw new Error("当前 Task 已变化，请确认后重新发送");
  }
  const taskId = requestedTaskId;
  const task = tasks.get(taskId);
  if (!task) {
    throw new Error("当前 Task 不存在");
  }
  if (taskIsTerminal(task)) {
    throw new Error("当前 Task 已终止，不能发送");
  }
  let threadId = null;
  if (taskId !== DISPATCHER_TASK_ID) {
    threadId = taskThreads.get(taskId) || null;
    if (!threadId) {
      throw new Error("当前 Task Conversation 尚未打开");
    }
  }

  const targetConversationId = conversationId;
  const messageId = newId("webui-message");
  rememberTarget(messageId, taskId);
  const payload = {
    conversation_id: targetConversationId,
    message_id: messageId,
    thread_id: threadId,
    sender_id: `webui:${targetConversationId}`,
    text,
  };
  try {
    await apiRequest("/api/channel/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (error) {
    if (targetTasks.get(messageId) === taskId) {
      targetTasks.delete(messageId);
    }
    if (targetConversationId === conversationId) {
      throw error;
    }
    return;
  }
  if (targetConversationId !== conversationId) {
    return;
  }
  appendEvent({
    taskId,
    role: "user",
    label: "You",
    text,
    detail: "accepted",
  });
  conversationStarted = true;
  storageSet(storageKeys.started(conversationId), "1");
  setStatus("消息已接收，等待事件…", "busy");
  startPolling();
}

function resetConversation() {
  if (taskSelectionBusy) {
    setStatus("正在打开 Task Conversation，暂时不能重置", "error");
    return;
  }
  pollGeneration += 1;
  taskHistoryGeneration += 1;
  storageRemove(storageKeys.cursor(conversationId));
  storageRemove(storageKeys.started(conversationId));
  conversationId = newId("webui-conversation");
  cursor = 0;
  renderedCursor = 0;
  conversationStarted = false;
  selectedTaskId = DISPATCHER_TASK_ID;
  storageSet(storageKeys.conversation, conversationId);
  outputs.clear();
  taskThreads.clear();
  taskTraceStates.clear();
  targetTasks.clear();
  taskTimelines.clear();
  elements.timelines.replaceChildren();
  ensureTimeline(selectedTaskId);
  renderTaskList();
  renderSelectedTask();
  setStatus(
    connected ? "已连接，新 Conversation" : "请输入 token",
    connected ? "ok" : "idle",
  );
  renderMetadata();
}

const COLUMN_DEFAULTS = Object.freeze({ tasks: 240, tree: 260, preview: 360 });
const COLUMN_RANGES = Object.freeze({
  tasks: Object.freeze([160, 360]),
  tree: Object.freeze([180, 480]),
  preview: Object.freeze([240, 720]),
});

function clampColumnWidth(edge, value) {
  const [min, max] = COLUMN_RANGES[edge];
  return Math.min(max, Math.max(min, Math.round(value)));
}

function storedColumnWidths() {
  const raw = storageGet(storageKeys.columnWidths);
  if (!raw) {
    return {};
  }
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (_error) {
    return {};
  }
}

const columnWidths = (() => {
  const stored = storedColumnWidths();
  const widths = {};
  for (const edge of Object.keys(COLUMN_DEFAULTS)) {
    const value = Number(stored[edge]);
    widths[edge] = Number.isFinite(value)
      ? clampColumnWidth(edge, value)
      : COLUMN_DEFAULTS[edge];
  }
  return widths;
})();

function applyColumnWidths() {
  const rootStyle = document.documentElement.style;
  for (const edge of Object.keys(columnWidths)) {
    rootStyle.setProperty(`--${edge}-w`, `${columnWidths[edge]}px`);
  }
}

function persistColumnWidths() {
  storageSet(storageKeys.columnWidths, JSON.stringify(columnWidths));
}

function insertDividers() {
  const workspace = document.querySelector(".workspace");
  const conversation = document.querySelector(".conversation-column");
  const filePanel = document.querySelector(".file-panel");
  const previewPanel = document.querySelector(".preview-panel");
  const makeDivider = (edge) => {
    const divider = document.createElement("div");
    divider.className = "divider";
    divider.dataset.edge = edge;
    divider.setAttribute("role", "separator");
    divider.setAttribute("aria-orientation", "vertical");
    divider.title = "拖拽调整宽度 · 双击重置";
    return divider;
  };
  workspace.insertBefore(makeDivider("tasks"), conversation);
  workspace.insertBefore(makeDivider("tree"), filePanel);
  workspace.insertBefore(makeDivider("preview"), previewPanel);
}

function bindDivider(divider) {
  divider.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) {
      return;
    }
    const edge = divider.dataset.edge;
    const startX = event.clientX;
    const startWidth = columnWidths[edge];
    document.body.classList.add("dragging");
    const onMove = (moveEvent) => {
      const delta = moveEvent.clientX - startX;
      const nextWidth =
        edge === "tasks" ? startWidth + delta : startWidth - delta;
      columnWidths[edge] = clampColumnWidth(edge, nextWidth);
      applyColumnWidths();
    };
    const onUp = () => {
      document.removeEventListener("pointermove", onMove);
      document.removeEventListener("pointerup", onUp);
      document.body.classList.remove("dragging");
      persistColumnWidths();
    };
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", onUp);
    event.preventDefault();
  });

  divider.addEventListener("dblclick", () => {
    const edge = divider.dataset.edge;
    columnWidths[edge] = COLUMN_DEFAULTS[edge];
    applyColumnWidths();
    persistColumnWidths();
  });
}

elements.connect.addEventListener("click", async () => {
  elements.connect.disabled = true;
  try {
    await connect();
  } catch (error) {
    connected = false;
    showError(error);
  } finally {
    elements.connect.disabled = false;
  }
});

elements.composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = elements.message.value.trim();
  if (!text) {
    setStatus("消息不能为空", "error");
    return;
  }
  elements.send.disabled = true;
  try {
    await sendMessage(text);
    elements.message.value = "";
  } catch (error) {
    showError(error);
  } finally {
    elements.send.disabled = false;
    elements.message.focus();
  }
});

elements.message.addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    (event.ctrlKey || event.metaKey) &&
    !elements.send.disabled
  ) {
    event.preventDefault();
    elements.composer.requestSubmit();
  }
});

elements.resetConversation.addEventListener("click", resetConversation);
elements.projectSelect.addEventListener("change", () => {
  void setProject(elements.projectSelect.value);
});
elements.token.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    elements.connect.click();
  }
});

ensureTimeline(selectedTaskId);
renderSelectedTask();
renderMetadata();
applyColumnWidths();
insertDividers();
document.querySelectorAll(".divider").forEach(bindDivider);
