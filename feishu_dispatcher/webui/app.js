const storageKeys = Object.freeze({
  conversation: "feishu-dispatcher.http-channel.conversation",
  cursor: (conversationId) =>
    `feishu-dispatcher.http-channel.cursor.${conversationId}`,
  started: (conversationId) =>
    `feishu-dispatcher.http-channel.started.${conversationId}`,
});

const elements = Object.freeze({
  composer: document.querySelector("#composer"),
  connect: document.querySelector("#connect"),
  conversationId: document.querySelector("#conversation-id"),
  cursor: document.querySelector("#cursor"),
  message: document.querySelector("#message"),
  resetConversation: document.querySelector("#reset-conversation"),
  send: document.querySelector("#send"),
  status: document.querySelector("#status"),
  timeline: document.querySelector("#timeline"),
  token: document.querySelector("#token"),
});

const outputs = new Map();
let conversationId = storedConversationId();
let cursor = storedCursor(conversationId);
let conversationStarted = storageGet(storageKeys.started(conversationId)) === "1";
let connected = false;
let pollGeneration = 0;

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
  elements.status.dataset.tone = tone;
}

function renderMetadata() {
  elements.conversationId.textContent = conversationId;
  elements.cursor.textContent = String(cursor);
}

function revealTimeline() {
  elements.timeline.querySelector("#empty-state")?.remove();
}

function scrollTimeline() {
  elements.timeline.scrollTop = elements.timeline.scrollHeight;
}

function appendEvent({ role = "assistant", label, text, detail = "" }) {
  revealTimeline();
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
  elements.timeline.append(article);
  scrollTimeline();
}

function ensureOutput(event) {
  const existing = outputs.get(event.output_id);
  if (existing) {
    return existing;
  }

  revealTimeline();
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
  elements.timeline.append(article);

  const output = { article, body, footer, status };
  outputs.set(event.output_id, output);
  scrollTimeline();
  return output;
}

function renderEvent(event) {
  switch (event.type) {
    case "message.created":
      appendEvent({
        label: event.threaded ? "Thread reply" : "Reply",
        text: event.text || "",
        detail: `cursor ${event.cursor}`,
      });
      break;
    case "thread.created":
      appendEvent({
        role: "system",
        label: "Thread created",
        text: event.text || "",
        detail: event.thread_id || "",
      });
      break;
    case "output.started":
      ensureOutput(event);
      break;
    case "output.delta": {
      const output = ensureOutput(event);
      output.body.textContent += event.text || "";
      scrollTimeline();
      break;
    }
    case "output.updated": {
      const output = ensureOutput(event);
      output.footer.textContent = event.footer || "";
      output.status.textContent = event.status || "running";
      output.article.dataset.status = event.status || "running";
      break;
    }
    default:
      appendEvent({
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

async function connect() {
  setStatus("正在验证 token…", "busy");
  await apiRequest("/api/channel/health");
  connected = true;
  setStatus("已连接", "ok");
  if (conversationStarted) {
    startPolling();
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
  if (!connected) {
    await connect();
  }
  const targetConversationId = conversationId;
  const payload = {
    conversation_id: targetConversationId,
    message_id: newId("webui-message"),
    thread_id: null,
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
    if (targetConversationId === conversationId) {
      throw error;
    }
    return;
  }
  if (targetConversationId !== conversationId) {
    return;
  }
  appendEvent({ role: "user", label: "You", text, detail: "accepted" });
  conversationStarted = true;
  storageSet(storageKeys.started(conversationId), "1");
  setStatus("消息已接收，等待事件…", "busy");
  startPolling();
}

function resetConversation() {
  pollGeneration += 1;
  storageRemove(storageKeys.cursor(conversationId));
  storageRemove(storageKeys.started(conversationId));
  conversationId = newId("webui-conversation");
  cursor = 0;
  conversationStarted = false;
  storageSet(storageKeys.conversation, conversationId);
  outputs.clear();
  elements.timeline.replaceChildren();
  const empty = document.createElement("div");
  empty.id = "empty-state";
  empty.className = "empty-state";
  const title = document.createElement("p");
  title.textContent = "这是一个新的 Conversation。";
  const detail = document.createElement("span");
  detail.textContent = "发送 /help 开始。";
  empty.append(title, detail);
  elements.timeline.append(empty);
  setStatus(connected ? "已连接，新 Conversation" : "请输入 token", connected ? "ok" : "idle");
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

renderMetadata();
