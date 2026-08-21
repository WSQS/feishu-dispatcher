const DISPATCHER_TASK_ID = "dispatcher";
const TERMINAL_TASK_STATUSES = new Set(["done", "stopped"]);

const storageKeys = Object.freeze({
  conversation: "feishu-dispatcher.http-channel.conversation",
  cursor: (conversationId) =>
    `feishu-dispatcher.http-channel.cursor.${conversationId}`,
  started: (conversationId) =>
    `feishu-dispatcher.http-channel.started.${conversationId}`,
});

const elements = Object.freeze({
  composer: document.querySelector("#composer"),
  composerTarget: document.querySelector("#composer-target"),
  connect: document.querySelector("#connect"),
  connectionSettings: document.querySelector("#connection-settings"),
  conversationId: document.querySelector("#conversation-id"),
  currentTask: document.querySelector("#current-task"),
  currentThread: document.querySelector("#current-thread"),
  cursor: document.querySelector("#cursor"),
  message: document.querySelector("#message"),
  resetConversation: document.querySelector("#reset-conversation"),
  send: document.querySelector("#send"),
  status: document.querySelector("#status"),
  taskList: document.querySelector("#task-list"),
  timelines: document.querySelector("#timelines"),
  token: document.querySelector("#token"),
});

const outputs = new Map();
const tasks = new Map();
const taskThreads = new Map();
const targetTasks = new Map();
const taskTimelines = new Map();
let conversationId = storedConversationId();
let cursor = storedCursor(conversationId);
let conversationStarted = storageGet(storageKeys.started(conversationId)) === "1";
let connected = false;
let pollGeneration = 0;
let selectedTaskId = DISPATCHER_TASK_ID;
let taskSelectionBusy = false;

class ApiError extends Error {
  constructor(status, payload) {
    const code = payload?.error || `http_${status}`;
    const detail = payload?.message ? `：${payload.message}` : "";
    super(`${code}${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function storageGet(key) {
  try {
    return localStorage.getItem(key);
  } catch (_error) {
    return null;
  }
}

function storageSet(key, value) {
  try {
    localStorage.setItem(key, value);
  } catch (_error) {
    return;
  }
}

function storageRemove(key) {
  try {
    localStorage.removeItem(key);
  } catch (_error) {
    return;
  }
}

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

function storedConversationId() {
  const existing = storageGet(storageKeys.conversation)?.trim();
  if (existing) {
    return existing;
  }
  const created = newId("webui-conversation");
  storageSet(storageKeys.conversation, created);
  return created;
}

function storedCursor(id) {
  const value = Number.parseInt(storageGet(storageKeys.cursor(id)) || "0", 10);
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function setStatus(text, tone = "idle") {
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
  timeline.append(createEmptyState(taskId));
  elements.timelines.append(timeline);
  taskTimelines.set(taskId, timeline);
  return timeline;
}

function renderSelectedTask() {
  const task = tasks.get(selectedTaskId);
  const threadId =
    selectedTaskId === DISPATCHER_TASK_ID
      ? null
      : taskThreads.get(selectedTaskId) || null;
  elements.currentTask.textContent = taskName(task);
  elements.currentThread.textContent = threadId || "root";
  elements.composerTarget.textContent = `发送给 ${taskName(task)}`;
  for (const [taskId, timeline] of taskTimelines) {
    timeline.hidden = taskId !== selectedTaskId;
  }
  scrollTimeline(selectedTaskId);
}

function revealTimeline(taskId) {
  ensureTimeline(taskId).querySelector(".empty-state")?.remove();
}

function scrollTimeline(taskId) {
  const timeline = taskTimelines.get(taskId);
  if (timeline && !timeline.hidden) {
    timeline.scrollTop = timeline.scrollHeight;
  }
}

function appendEvent({
  taskId = selectedTaskId,
  role = "assistant",
  label,
  text,
  detail = "",
}) {
  revealTimeline(taskId);
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

async function apiRequest(path, options = {}) {
  const token = elements.token.value.trim();
  if (!token) {
    throw new Error("请输入 http-channel.token");
  }
  const headers = new Headers(options.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(path, { ...options, headers });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_error) {
    payload = {};
  }
  if (!response.ok) {
    throw new ApiError(response.status, payload);
  }
  return payload;
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
    button.disabled = taskSelectionBusy || terminal;
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
      binding.textContent = "已终止";
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

async function loadTasks() {
  const payload = await apiRequest("/api/tasks");
  const items = Array.isArray(payload.tasks) ? payload.tasks : [];
  tasks.clear();
  for (const task of items) {
    if (task && typeof task.task_id === "string" && task.task_id) {
      tasks.set(task.task_id, task);
    }
  }
  if (!tasks.has(DISPATCHER_TASK_ID)) {
    throw new Error("Task 列表缺少 Dispatcher");
  }
  const selected = tasks.get(selectedTaskId);
  if (!selected || taskIsTerminal(selected)) {
    selectedTaskId = DISPATCHER_TASK_ID;
  }
  renderTaskList();
  ensureTimeline(selectedTaskId);
  renderSelectedTask();
}

async function connect() {
  setStatus("正在验证 token…", "busy");
  await apiRequest("/api/channel/health");
  await loadTasks();
  connected = true;
  setStatus("已连接", "ok");
  elements.connectionSettings.open = false;
  if (conversationStarted) {
    startPolling();
  }
}

async function createTaskConversation(task) {
  const payload = await apiRequest(
    `/api/tasks/${encodeURIComponent(task.task_id)}/conversations`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId }),
    },
  );
  if (typeof payload.thread_id !== "string" || !payload.thread_id.trim()) {
    throw new Error("创建 Task Conversation 未返回 thread_id");
  }
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
  if (taskIsTerminal(task)) {
    setStatus(`${taskName(task)} 已终止，不能打开`, "error");
    return;
  }

  taskSelectionBusy = true;
  renderTaskList();
  let pollingPaused = false;
  try {
    if (taskId !== DISPATCHER_TASK_ID && !taskThreads.has(taskId)) {
      pollGeneration += 1;
      pollingPaused = true;
      setStatus(`正在打开 ${taskName(task)}…`, "busy");
      await createTaskConversation(task);
    }
    selectedTaskId = taskId;
    ensureTimeline(taskId);
    renderSelectedTask();
    setStatus(`已选择 ${taskName(task)}`, "ok");
  } finally {
    taskSelectionBusy = false;
    renderTaskList();
    if (pollingPaused && conversationStarted) {
      startPolling();
    }
  }
}

function persistCursor() {
  storageSet(storageKeys.cursor(conversationId), String(cursor));
  renderMetadata();
}

async function pollOnce(generation) {
  const targetConversationId = conversationId;
  const query = new URLSearchParams({
    conversation_id: targetConversationId,
    after: String(cursor),
  });
  const payload = await apiRequest(`/api/channel/events?${query}`);
  if (generation !== pollGeneration || targetConversationId !== conversationId) {
    return;
  }
  for (const event of payload.events || []) {
    renderEvent(event);
    if (Number.isSafeInteger(event.cursor) && event.cursor > cursor) {
      cursor = event.cursor;
    }
  }
  if (Number.isSafeInteger(payload.next_cursor) && payload.next_cursor > cursor) {
    cursor = payload.next_cursor;
  }
  persistCursor();
  setStatus(`已连接 · cursor ${cursor}`, "ok");
}

function wait(milliseconds) {
  return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
}

function showError(error) {
  const message = error instanceof Error ? error.message : String(error);
  setStatus(message, "error");
}

function startPolling() {
  const generation = ++pollGeneration;
  const loop = async () => {
    while (generation === pollGeneration && connected && conversationStarted) {
      try {
        await pollOnce(generation);
      } catch (error) {
        if (generation === pollGeneration) {
          showError(error);
        }
        return;
      }
      await wait(700);
    }
  };
  void loop();
}

async function sendMessage(text) {
  if (taskSelectionBusy) {
    throw new Error("正在打开 Task Conversation，请稍候");
  }
  if (!connected) {
    await connect();
  }
  const taskId = selectedTaskId;
  const task = tasks.get(taskId);
  if (!task) {
    throw new Error("当前 Task 不存在");
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
  storageRemove(storageKeys.cursor(conversationId));
  storageRemove(storageKeys.started(conversationId));
  conversationId = newId("webui-conversation");
  cursor = 0;
  conversationStarted = false;
  selectedTaskId = DISPATCHER_TASK_ID;
  storageSet(storageKeys.conversation, conversationId);
  outputs.clear();
  taskThreads.clear();
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
elements.token.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    elements.connect.click();
  }
});

ensureTimeline(selectedTaskId);
renderSelectedTask();
renderMetadata();
