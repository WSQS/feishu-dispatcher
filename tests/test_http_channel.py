"""HTTP Channel 的鉴权、消息、事件与生命周期测试。"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from feishu_dispatcher import __version__
from feishu_dispatcher.channel import ChannelMessage
from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.http_channel import HttpChannel, ensure_token
from feishu_dispatcher.session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputStarted,
    AgentPlanEntry,
    AgentPlanUpdated,
    SessionEvent,
    SessionInputAccepted,
    ToolCallObserved,
    session_event_to_dict,
)


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        return holder.getsockname()[1]


def _raw_request(url: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, dict(response.headers.items()), response.read()


def _webui_javascript() -> str:
    return (
        Path(__file__).parents[1] / "feishu_dispatcher" / "webui" / "app.js"
    ).read_text(encoding="utf-8")


def _webui_storage_javascript() -> str:
    return (
        Path(__file__).parents[1] / "feishu_dispatcher" / "webui" / "storage.js"
    ).read_text(encoding="utf-8")


def _request(
    method: str,
    url: str,
    token: str | None,
    payload: object | None = None,
) -> tuple[int, dict]:
    headers = {}
    data = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _message(
    conversation_id: str = "browser-a",
    message_id: str = "message-a",
    *,
    thread_id: str | None = None,
    sender_id: str = "user-a",
    text: str = "/help",
) -> dict:
    return {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "thread_id": thread_id,
        "sender_id": sender_id,
        "text": text,
    }


def _events_url(channel: HttpChannel, conversation_id: str, after: int = 0) -> str:
    query = urllib.parse.urlencode({"conversation_id": conversation_id, "after": after})
    return channel.base_url + "/api/channel/events?" + query


async def _wait_for_events(
    channel: HttpChannel,
    conversation_id: str,
    *,
    minimum: int = 1,
    timeout: float = 3.0,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status, payload = await asyncio.to_thread(
            _request,
            "GET",
            _events_url(channel, conversation_id),
            "tok-http",
        )
        if status == 200 and len(payload["events"]) >= minimum:
            return payload
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError((status, payload))
        await asyncio.sleep(0.01)


def test_webui_task_description_truncates_only_on_desktop():
    stylesheet = (
        Path(__file__).parents[1] / "feishu_dispatcher" / "webui" / "style.css"
    ).read_text(encoding="utf-8")
    description = re.search(r"\.task-description\s*\{(?P<rules>[^}]*)\}", stylesheet)
    assert description
    for declaration in (
        "min-width: 0;",
        "overflow: hidden;",
        "text-overflow: ellipsis;",
        "white-space: nowrap;",
    ):
        assert declaration in description["rules"]

    mobile = stylesheet.split("@media (max-width: 640px)", maxsplit=1)[1]
    mobile_description = re.search(
        r"\.task-description\s*\{(?P<rules>[^}]*)\}",
        mobile,
    )
    assert mobile_description
    for declaration in (
        "overflow: visible;",
        "text-overflow: clip;",
        "white-space: normal;",
    ):
        assert declaration in mobile_description["rules"]


def test_webui_polls_task_snapshots_independently():
    javascript = _webui_javascript()

    assert "const TASK_POLL_INTERVAL_MS = 2000;" in javascript
    assert "let taskPollGeneration = 0;" in javascript
    assert "let taskRequestTail = Promise.resolve();" in javascript
    assert "let taskSnapshot = null;" in javascript
    assert "let statusRevision = 0;" in javascript
    assert "nextSnapshot === taskSnapshot" in javascript
    assert re.search(
        r"taskRequestTail = request\.catch\(\(\) => \{\s*\}\);",
        javascript,
    )
    assert "statusRevision === statusRevisionAtStart" in javascript
    assert 'statusSource === "task-poll"' in javascript
    assert 'showError(error, "task-poll");' in javascript
    assert "generation !== taskPollGeneration || taskSelectionBusy" in javascript
    assert "while (generation === taskPollGeneration && connected)" in javascript
    assert "await wait(TASK_POLL_INTERVAL_MS);" in javascript
    assert "startTaskPolling();" in javascript
    assert "await wait(700);" in javascript


def test_webui_recovers_http_channel_instance_and_cursor():
    javascript = _webui_javascript()
    storage_javascript = _webui_storage_javascript()

    def section(start: str, end: str) -> str:
        return javascript.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]

    clear_state = section(
        "function clearChannelRuntimeState()",
        "function acceptChannelInstance",
    )
    accept_instance = section(
        "function acceptChannelInstance",
        "async function refreshChannelInstance",
    )
    connect = section(
        "async function connect()", "async function createTaskConversation"
    )
    poll_once = section("async function pollOnce", "function wait")
    recovery = section("function recoverPollingError", "function startPolling")
    polling = section("function startPolling", "async function pollTasksOnce")
    send = section("async function sendMessage", "function resetConversation")
    reset = section("function resetConversation", "const COLUMN_DEFAULTS")

    assert (
        'channelInstance: "feishu-dispatcher.http-channel.instance"'
        in storage_javascript
    )
    assert "const MAX_POLL_RECOVERY_ATTEMPTS = 2;" in javascript
    for expected in (
        "pollGeneration += 1;",
        "storageRemove(storageKeys.cursor(conversationId));",
        "storageRemove(storageKeys.started(conversationId));",
        "taskThreads.clear();",
        "targetTasks.clear();",
        "outputs.clear();",
    ):
        assert expected in clear_state
    assert (
        "storageSet(storageKeys.channelInstance, channelInstanceId);" in accept_instance
    )
    assert "clearChannelRuntimeState();" in accept_instance
    assert "const instanceState = await refreshChannelInstance();" in connect
    assert "return instanceState;" in connect
    assert "const instanceState = acceptChannelInstance(payload);" in poll_once
    assert "event.cursor > renderedCursor" in poll_once
    for expected in (
        'case "unknown_conversation":',
        'case "cursor_invalid":',
        'case "cursor_expired":',
        '"unknown_conversation", "cursor_invalid", "cursor_expired"',
        "cursor = oldestCursor - 1;",
    ):
        assert expected in recovery
    for expected in (
        "const generation = ++pollGeneration;",
        "if (!(await pollOnce(generation)))",
        "recoveryAttempts < MAX_POLL_RECOVERY_ATTEMPTS",
    ):
        assert expected in polling
    for expected in (
        "instanceState = await connect();",
        "instanceState = await refreshChannelInstance();",
        'instanceState !== "same"',
        "requestedTaskId !== DISPATCHER_TASK_ID",
        "selectedTaskId !== requestedTaskId",
        "const taskId = requestedTaskId;",
        "if (taskIsTerminal(task))",
    ):
        assert expected in send
    assert send.index("selectedTaskId !== requestedTaskId") < send.index(
        "const taskId = requestedTaskId;"
    )
    assert "renderedCursor = 0;" in reset


def test_webui_consumes_session_events_without_duplicate_unknown_entries():
    javascript = _webui_javascript()

    def section(start: str, end: str) -> str:
        return javascript.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]

    task_routing = section("function taskForEvent", "function renderEvent")
    event_rendering = section("function renderEvent", "function renderTaskList")

    assert 'event.type === "session.event"' in task_routing
    assert "event.event?.session_id" in task_routing
    assert 'case "session.event":' in event_rendering
    for event_type in (
        "session.input.accepted",
        "agent.output.started",
        "agent.output.delta",
        "agent.plan.updated",
        "agent.output.finished",
        "tool.call.observed",
    ):
        assert f'sessionEvent.type === "{event_type}"' in event_rendering
    assert "event.presentation" in event_rendering
    assert "ensureOutput(presentation, taskId)" in event_rendering
    assert "收到无法识别的 SessionEvent。" in event_rendering
    assert "text: sessionEvent.type" in event_rendering


def test_webui_loads_and_paginates_persisted_task_history():
    javascript = _webui_javascript()

    def section(start: str, end: str) -> str:
        return javascript.split(start, maxsplit=1)[1].split(end, maxsplit=1)[0]

    timeline = section("function ensureTimeline", "function renderSelectedTask")
    live_events = section("function renderEvent", "function traceState")
    history = section("function traceState", "function renderTaskList")
    task_selection = section("async function selectTask", "function persistCursor")

    assert "const TASK_HISTORY_LIMIT = 100;" in javascript
    assert "const taskTraceStates = new Map();" in javascript
    assert "let taskHistoryGeneration = 0;" in javascript
    assert "timeline.scrollTop > 24" in timeline
    assert "loadEarlierTaskHistory(taskId, generation)" in timeline
    assert 'historyLoad.className = "history-load";' in timeline
    assert "event.trace_sequence" in live_events
    assert "!claimTraceSequence(taskId, event.trace_sequence)" in live_events
    assert "state.seenSequences.has(sequence)" in history
    assert 'timeline.querySelector(".history-load")?.after(fragment);' in history
    assert "preserveScroll: before !== null" in history
    assert "state.finishedTurns.add(turnId);" in history
    assert 'query.set("before", String(before));' in history
    assert "`/api/tasks/${encodeURIComponent(taskId)}/events?${query}`" in history
    assert (
        "generation !== taskHistoryGeneration || selectedTaskId !== taskId" in history
    )
    for event_type in (
        "session.input.accepted",
        "agent.output.finished",
        "agent.plan.updated",
        "tool.call.observed",
        "session.state.changed",
        "session.error.occurred",
    ):
        assert f'case "{event_type}"' in history
    assert "!taskIsTerminal(task)" in task_selection
    assert "await loadTaskHistory(taskId" in task_selection
    assert "historyGeneration !== taskHistoryGeneration" in task_selection
    assert "button.disabled = taskSelectionBusy;" in javascript
    assert "taskSelectionBusy || terminal" not in javascript


def test_webui_api_logic_isolated_from_app_source():
    app_source = (
        Path(__file__).parents[1] / "feishu_dispatcher" / "webui" / "app.ts"
    ).read_text(encoding="utf-8")
    api_source = (
        Path(__file__).parents[1] / "feishu_dispatcher" / "webui" / "api.ts"
    ).read_text(encoding="utf-8")

    assert "fetch(" not in app_source
    assert "new ApiError" not in app_source
    assert "fetch(" in api_source
    assert "new ApiError" in api_source
    for endpoint in (
        "/api/projects",
        "/tree/children",
        "/file?",
    ):
        assert endpoint not in app_source
        assert endpoint in api_source


def test_webui_storage_logic_isolated_from_app_source():
    app_source = (
        Path(__file__).parents[1] / "feishu_dispatcher" / "webui" / "app.ts"
    ).read_text(encoding="utf-8")
    storage_source = (
        Path(__file__).parents[1] / "feishu_dispatcher" / "webui" / "storage.ts"
    ).read_text(encoding="utf-8")

    assert "localStorage." not in app_source
    assert "feishu-dispatcher.http-channel.conversation" not in app_source
    assert "localStorage.getItem" in storage_source
    assert "localStorage.setItem" in storage_source
    assert "localStorage.removeItem" in storage_source
    assert "feishu-dispatcher.http-channel.conversation" in storage_source


async def test_webui_assets_are_same_origin_and_do_not_require_token():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        expected = {
            "/": "text/html; charset=utf-8",
            "/webui/app.js": "text/javascript; charset=utf-8",
            "/webui/api.js": "text/javascript; charset=utf-8",
            "/webui/storage.js": "text/javascript; charset=utf-8",
            "/webui/style.css": "text/css; charset=utf-8",
        }
        assets: dict[str, bytes] = {}
        for path, content_type in expected.items():
            status, headers, body = await asyncio.to_thread(
                _raw_request, channel.base_url + path
            )
            assert status == 200
            assert headers["Content-Type"] == content_type
            assert headers["Cache-Control"] == "no-store"
            assert headers["X-Content-Type-Options"] == "nosniff"
            assert "default-src 'self'" in headers["Content-Security-Policy"]
            assets[path] = body

        html = assets["/"].decode("utf-8")
        javascript = assets["/webui/app.js"].decode("utf-8")
        api_javascript = assets["/webui/api.js"].decode("utf-8")
        storage_javascript = assets["/webui/storage.js"].decode("utf-8")
        stylesheet = assets["/webui/style.css"].decode("utf-8")
        assert 'src="/webui/app.js"' in html
        assert 'href="/webui/style.css"' in html
        assert 'id="floating-controls"' in html
        assert '<details id="connection-settings" class="connection-settings">' in html
        assert 'class="hero"' not in html
        assert 'class="connection panel"' not in html
        assert ".floating-controls" in stylesheet
        assert "padding: 0 0 56px;" in stylesheet
        assert re.search(
            r"\.task-panel\s*\{\s*position: sticky;\s*top: 0;",
            stylesheet,
        )
        assert "max-height: calc(100vh - 210px);" not in stylesheet
        desktop_layout = re.search(
            r"@media \(min-width: 641px\) \{(?P<rules>.*)"
            r"@media \(max-width: 640px\)",
            stylesheet,
            re.DOTALL,
        )
        assert desktop_layout
        assert "max-height: 54vh;" not in desktop_layout["rules"]
        for declaration in (
            "height: 100%;",
            "overflow: hidden;",
            "padding-bottom: 0;",
            "align-items: stretch;",
            "grid-template-rows: auto minmax(0, 1fr);",
            "grid-template-rows: minmax(0, 1fr) auto;",
            "grid-template-rows: auto auto minmax(0, 1fr);",
            "min-height: 0;",
        ):
            assert declaration in desktop_layout["rules"]
        assert 'id="task-list"' in html
        assert 'id="timelines"' in html
        assert 'import { ApiError, createApiClient } from "./api.js";' in javascript
        assert 'from "./storage.js";' in javascript
        assert "export class ApiError" in api_javascript
        assert "export function createApiRequest" in api_javascript
        assert "export function createApiClient" in api_javascript
        assert "export const storageKeys" in storage_javascript
        assert "export function storageGet" in storage_javascript
        for element_id in re.findall(
            r'document\.querySelector\("#([^"]+)"\)',
            javascript,
        ):
            assert f'id="{element_id}"' in html
        assert "/api/channel/messages" in javascript
        assert "/api/channel/events" in javascript
        assert "api.listTasks()" in javascript
        assert 'apiRequest("/api/tasks")' not in javascript
        assert "async listTasks()" in api_javascript
        assert 'request("/api/tasks")' in api_javascript
        assert "/events?${query}`" in javascript
        assert "elements.connectionSettings.open = false;" in javascript
        assert (
            'task.task_id !== DISPATCHER_TASK_ID && task.status === "stopped"'
            in javascript
        )
        assert "/conversations`" in javascript
        assert "thread_id: threadId" in javascript
        assert "const taskThreads = new Map();" in javascript
        assert "feishu-dispatcher.http-channel.conversation" in storage_javascript
        assert "feishu-dispatcher.http-channel.cursor" in storage_javascript
        assert "feishu-dispatcher.http-channel.thread" not in storage_javascript
    finally:
        channel.stop()


async def test_health_requires_token_and_returns_channel_version():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        missing_status, _ = await asyncio.to_thread(
            _request, "GET", channel.base_url + "/api/channel/health", None
        )
        bad_status, _ = await asyncio.to_thread(
            _request, "GET", channel.base_url + "/api/channel/health", "bad"
        )
        status, payload = await asyncio.to_thread(
            _request, "GET", channel.base_url + "/api/channel/health", "tok-http"
        )
        assert missing_status == 401
        assert bad_status == 401
        assert status == 200
        assert payload == {
            "ok": True,
            "channel": "http",
            "version": __version__,
            "instance_id": channel._instance_id,
        }
    finally:
        channel.stop()


async def test_application_post_route_requires_token_and_marshals_body():
    main_thread_id = threading.get_ident()
    seen: list[tuple[dict, dict, int]] = []

    async def create_conversation(context: dict, request: dict) -> tuple[int, dict]:
        seen.append((context, request, threading.get_ident()))
        return 201, {
            "task_id": request["segments"]["task_id"],
            "conversation_id": request["body"]["conversation_id"],
        }

    channel = HttpChannel(
        "tok-http",
        asyncio.get_running_loop(),
        host="127.0.0.1",
        port=0,
        routes={
            (
                "POST",
                "/api/tasks/{task_id}/conversations",
            ): create_conversation
        },
        route_context={"channel_key": "http"},
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        for token in (None, "bad"):
            status, payload = await asyncio.to_thread(
                _request,
                "POST",
                channel.base_url + "/api/tasks/t1/conversations?source=webui",
                token,
                {"conversation_id": "browser-a"},
            )
            assert status == 401
            assert payload == {"error": "invalid_token"}
            assert seen == []

        status, payload = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/tasks/t1/conversations?source=webui",
            "tok-http",
            {"conversation_id": "browser-a"},
        )
        assert status == 201
        assert payload == {
            "task_id": "t1",
            "conversation_id": "browser-a",
        }
        assert seen == [
            (
                {"channel_key": "http"},
                {
                    "path": "/api/tasks/t1/conversations",
                    "query": {"source": "webui"},
                    "segments": {"task_id": "t1"},
                    "body": {"conversation_id": "browser-a"},
                },
                main_thread_id,
            )
        ]
    finally:
        channel.stop()


async def test_application_post_route_preserves_channel_capacity_error():
    async def create_conversation(_context: dict, request: dict) -> tuple[int, dict]:
        thread_id = channel.create_thread(
            request["body"]["conversation_id"],
            "task",
        )
        return 201, {"thread_id": thread_id}

    channel = HttpChannel(
        "tok-http",
        asyncio.get_running_loop(),
        host="127.0.0.1",
        port=0,
        routes={
            (
                "POST",
                "/api/tasks/{task_id}/conversations",
            ): create_conversation
        },
        max_conversations=1,
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        channel.send_text("browser-a", "existing")
        status, payload = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/tasks/t1/conversations",
            "tok-http",
            {"conversation_id": "browser-b"},
        )
        assert status == 429
        assert payload == {
            "error": "conversation_capacity",
            "message": "HTTP Channel Conversation 数量已达上限",
        }
    finally:
        channel.stop()


async def test_message_dispatch_and_reply_round_trip():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )
    received: list[ChannelMessage] = []
    handled = asyncio.Event()

    async def handle(message: ChannelMessage) -> None:
        received.append(message)
        channel.reply_text(message.message_id, "help reply")
        handled.set()

    channel.start(handle)
    try:
        status, payload = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message(),
        )
        assert status == 202
        assert payload == {"accepted": True}
        await asyncio.wait_for(handled.wait(), timeout=3)
        events = await _wait_for_events(channel, "browser-a")
        assert received == [
            ChannelMessage(
                conversation_id="browser-a",
                message_id="message-a",
                thread_id=None,
                text="/help",
                sender_id="user-a",
            )
        ]
        assert events["events"][0]["type"] == "message.created"
        assert events["events"][0]["target_id"] == "message-a"
        assert events["events"][0]["text"] == "help reply"
    finally:
        channel.stop()


async def test_conversations_are_isolated_and_target_conflicts_are_rejected():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )
    handled = asyncio.Event()
    seen: list[str] = []

    async def handle(message: ChannelMessage) -> None:
        seen.append(message.conversation_id)
        channel.reply_text(message.message_id, f"reply:{message.conversation_id}")
        if len(seen) == 2:
            handled.set()

    channel.start(handle)
    try:
        first_status, _ = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message("browser-a", "shared-message"),
        )
        conflict_status, conflict = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message("browser-b", "shared-message"),
        )
        second_status, _ = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message("browser-b", "message-b"),
        )
        assert first_status == 202
        assert conflict_status == 409
        assert conflict["error"] == "target_conflict"
        assert second_status == 202
        await asyncio.wait_for(handled.wait(), timeout=3)
        events_a = await _wait_for_events(channel, "browser-a")
        events_b = await _wait_for_events(channel, "browser-b")
        assert [event["text"] for event in events_a["events"]] == ["reply:browser-a"]
        assert [event["text"] for event in events_b["events"]] == ["reply:browser-b"]
        assert seen == ["browser-a", "browser-b"]
    finally:
        channel.stop()


async def test_invalid_requests_do_not_enter_dispatcher():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )
    seen: list[ChannelMessage] = []

    async def handle(message: ChannelMessage) -> None:
        seen.append(message)

    channel.start(handle)
    try:
        unauthenticated_empty, _ = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            None,
        )
        bad_token, _ = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "bad",
            _message(),
        )
        blank_sender, payload = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message(sender_id=" "),
        )
        list_body, _ = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            [],
        )
        unknown_thread, unknown_payload = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message(thread_id="unknown-thread"),
        )
        assert unauthenticated_empty == 401
        assert bad_token == 401
        assert blank_sender == 400
        assert payload["error"] == "invalid_request"
        assert list_body == 400
        assert unknown_thread == 404
        assert unknown_payload["error"] == "unknown_target"
        await asyncio.sleep(0)
        assert seen == []
    finally:
        channel.stop()


async def test_cursor_expiry_invalid_cursor_and_conversation_capacity():
    channel = HttpChannel(
        "tok-http",
        asyncio.get_running_loop(),
        host="127.0.0.1",
        port=0,
        max_conversations=1,
        max_events=2,
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        unknown_status, unknown = await asyncio.to_thread(
            _request,
            "GET",
            _events_url(channel, "missing"),
            "tok-http",
        )
        channel.send_text("browser-a", "one")
        channel.send_text("browser-a", "two")
        channel.send_text("browser-a", "three")
        expired_status, expired = await asyncio.to_thread(
            _request,
            "GET",
            _events_url(channel, "browser-a", after=0),
            "tok-http",
        )
        valid_status, valid = await asyncio.to_thread(
            _request,
            "GET",
            _events_url(channel, "browser-a", after=1),
            "tok-http",
        )
        invalid_status, invalid = await asyncio.to_thread(
            _request,
            "GET",
            _events_url(channel, "browser-a", after=4),
            "tok-http",
        )
        capacity_status, capacity = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message("browser-b", "message-b"),
        )
        assert unknown_status == 404
        assert unknown["error"] == "unknown_conversation"
        assert unknown["instance_id"] == channel._instance_id
        assert expired_status == 409
        assert expired["error"] == "cursor_expired"
        assert expired["instance_id"] == channel._instance_id
        assert valid_status == 200
        assert valid["instance_id"] == channel._instance_id
        assert [event["text"] for event in valid["events"]] == ["two", "three"]
        assert invalid_status == 409
        assert invalid["error"] == "cursor_invalid"
        assert invalid["instance_id"] == channel._instance_id
        assert capacity_status == 429
        assert capacity["error"] == "conversation_capacity"
    finally:
        channel.stop()


async def test_thread_reply_session_event_output_and_restart():
    port = _available_port()
    channel = HttpChannel(
        "tok-http",
        asyncio.get_running_loop(),
        host="127.0.0.1",
        port=port,
        throttle_window=0.01,
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        thread_id = channel.create_thread("browser-a", "start")
        channel.reply_text(thread_id, "reply", threaded=True)
        output = channel.open_output(thread_id, "demo", footer="model:a")
        channel.handle_session_event(
            thread_id,
            SessionEvent(
                event_id="event-started",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=AgentOutputStarted(),
            ),
        )
        channel.handle_session_event(
            thread_id,
            SessionEvent(
                event_id="event-delta",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=AgentOutputDelta(stream="message", text="hello world"),
            ),
        )
        output.set_footer("model:b")
        await output.set_status("done")
        channel.handle_session_event(
            thread_id,
            SessionEvent(
                event_id="event-finished",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=AgentOutputFinished(
                    message="hello world",
                    thought="",
                    outcome="completed",
                ),
            ),
        )
        await output.aclose()
        events = await _wait_for_events(channel, "browser-a", minimum=5)
        event_types = [event["type"] for event in events["events"]]
        assert event_types == [
            "thread.created",
            "message.created",
            "session.event",
            "session.event",
            "session.event",
        ]
        output_id = events["events"][2]["presentation"]["output_id"]
        assert events["events"][2]["presentation"] == {
            "output_id": output_id,
            "target_id": thread_id,
            "title": "demo",
            "footer": "model:a",
            "status": "running",
        }
        assert events["events"][3]["presentation"] == {
            "output_id": output_id,
            "text": "hello world",
        }
        assert events["events"][4]["presentation"] == {
            "output_id": output_id,
            "text": "",
            "footer": "model:b",
            "status": "done",
        }

        first_thread = channel._thread
        first_instance_id = channel._instance_id
        assert first_thread is not None
        channel.stop()
        assert not channel.is_alive()
        assert not first_thread.is_alive()
        channel.restart()
        assert channel.is_alive()
        assert channel.base_url == f"http://127.0.0.1:{port}"
        second_thread = channel._thread
        assert second_thread is not None
        assert second_thread is not first_thread
        health_status, health = await asyncio.to_thread(
            _request,
            "GET",
            channel.base_url + "/api/channel/health",
            "tok-http",
        )
        assert health_status == 200
        assert health["instance_id"] == first_instance_id
    finally:
        final_thread = channel._thread
        channel.stop()
        if final_thread is not None:
            assert not final_thread.is_alive()


async def test_session_input_event_projects_to_thread_message():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        thread_id = channel.create_thread("browser-a", "start")
        event = SessionEvent(
            event_id="event-1",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=SessionInputAccepted(
                text="检查状态",
                source=ConversationRef("feishu", "om_root"),
            ),
        )

        channel.handle_session_event(thread_id, event, trace_sequence=42)

        events = await _wait_for_events(channel, "browser-a", minimum=3)
        raw_event = events["events"][1]
        assert raw_event == {
            "cursor": 2,
            "type": "session.event",
            "event": session_event_to_dict(event),
            "trace_sequence": 42,
        }
        projected = events["events"][2]
        assert projected["type"] == "message.created"
        assert projected["target_id"] == thread_id
        assert projected["text"] == "↪️ 同步自 feishu：检查状态"
        assert projected["threaded"] is True
    finally:
        channel.stop()


async def test_empty_session_input_event_does_not_create_message():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        thread_id = channel.create_thread("browser-a", "start")
        event = SessionEvent(
            event_id="event-empty",
            session_id="t1",
            turn_id="turn-empty",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=SessionInputAccepted(text=""),
        )

        channel.handle_session_event(thread_id, event)

        events = channel._events_after("browser-a", 0)
        assert len(events["events"]) == 2
        assert events["events"][0]["type"] == "thread.created"
        assert events["events"][1] == {
            "cursor": 2,
            "type": "session.event",
            "event": session_event_to_dict(event),
        }
    finally:
        channel.stop()


async def test_agent_output_events_project_as_session_events():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        thread_id = channel.create_thread("browser-a", "start")
        bodies = [
            AgentOutputStarted(),
            AgentOutputDelta(stream="thought", text="thinking"),
            AgentPlanUpdated(
                entries=(
                    AgentPlanEntry(content="read files", status="completed"),
                    AgentPlanEntry(content="write code", status="in_progress"),
                )
            ),
            AgentOutputDelta(stream="message", text="answer"),
            AgentOutputFinished(
                message="answer",
                thought="thinking",
                outcome="completed",
            ),
            ToolCallObserved(
                tool_call_id="tc1",
                kind="execute",
                title="pytest",
                status="completed",
                detail="pytest -q",
            ),
        ]
        expected = []
        for index, body in enumerate(bodies, start=1):
            event = SessionEvent(
                event_id=f"event-{index}",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=body,
            )
            expected.append(session_event_to_dict(event))
            channel.handle_session_event(thread_id, event)

        events = channel._events_after("browser-a", 0)["events"]
        assert [event["type"] for event in events] == [
            "thread.created",
            "session.event",
            "session.event",
            "session.event",
            "session.event",
            "session.event",
            "session.event",
        ]
        assert [event["event"] for event in events[1:]] == expected
    finally:
        channel.stop()


async def test_session_event_presentation_is_the_only_live_output_path():
    channel = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        thread_id = channel.create_thread("browser-a", "start")
        output = channel.open_output(thread_id, "demo", footer="model:a")
        output.feed("legacy text must not be emitted")
        await output.flush()
        channel.handle_session_event(
            thread_id,
            SessionEvent(
                event_id="event-started",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=AgentOutputStarted(),
            ),
        )
        channel.handle_session_event(
            thread_id,
            SessionEvent(
                event_id="event-tool",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=ToolCallObserved(
                    tool_call_id="tc1",
                    kind="execute",
                    title="pytest",
                    status="completed",
                    detail="pytest -q",
                ),
            ),
        )
        channel.handle_session_event(
            thread_id,
            SessionEvent(
                event_id="event-plan",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=AgentPlanUpdated(
                    entries=(AgentPlanEntry(content="run tests", status="in_progress"),)
                ),
            ),
        )

        events = channel._events_after("browser-a", 0)["events"]
        assert [event["type"] for event in events] == [
            "thread.created",
            "session.event",
            "session.event",
            "session.event",
        ]
        assert events[2]["presentation"]["text"] == "\n✅ pytest: pytest -q\n"
        assert events[3]["presentation"]["text"] == "\n📋 计划:\n🔄 run tests\n"
        assert "legacy text must not be emitted" not in str(events)
    finally:
        channel.stop()


async def test_new_http_channel_object_gets_new_instance_id():
    first = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )
    second = HttpChannel(
        "tok-http", asyncio.get_running_loop(), host="127.0.0.1", port=0
    )
    try:
        assert first._instance_id != second._instance_id
    finally:
        first.stop()
        second.stop()


async def test_start_failure_releases_listener(monkeypatch):
    port = _available_port()
    channel = HttpChannel(
        "tok-http",
        asyncio.get_running_loop(),
        host="127.0.0.1",
        port=port,
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    def fail_start(_thread) -> None:
        raise RuntimeError("thread start boom")

    with monkeypatch.context() as patch:
        patch.setattr(
            "feishu_dispatcher.http_channel.threading.Thread.start", fail_start
        )
        with pytest.raises(RuntimeError, match="thread start boom"):
            channel.start(ignore)

    assert not channel.is_alive()
    with pytest.raises(RuntimeError, match="尚未监听"):
        _ = channel.base_url

    channel.start(ignore)
    try:
        assert channel.base_url == f"http://127.0.0.1:{port}"
    finally:
        channel.stop()


async def test_target_capacity_is_bounded():
    channel = HttpChannel(
        "tok-http",
        asyncio.get_running_loop(),
        host="127.0.0.1",
        port=0,
        max_targets=1,
    )

    async def ignore(_message: ChannelMessage) -> None:
        return None

    channel.start(ignore)
    try:
        first_status, _ = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message(message_id="message-a"),
        )
        status, payload = await asyncio.to_thread(
            _request,
            "POST",
            channel.base_url + "/api/channel/messages",
            "tok-http",
            _message(message_id="message-b"),
        )
        assert first_status == 202
        assert status == 429
        assert payload["error"] == "target_capacity"
        known_status, known = await asyncio.to_thread(
            _request,
            "GET",
            _events_url(channel, "browser-a"),
            "tok-http",
        )
        assert known_status == 200
        assert known["events"] == []
    finally:
        channel.stop()


def test_http_channel_token_persists_independently(tmp_path: Path):
    path = tmp_path / "http-channel.token"
    first = ensure_token(path)
    second = ensure_token(path)
    assert first == second
    assert path.read_text(encoding="utf-8").strip() == first
    assert not (tmp_path / "http-channel.token.tmp").exists()


def test_http_channel_rejects_blank_token():
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError, match="token"):
            HttpChannel(" ", loop, host="127.0.0.1", port=0)
    finally:
        loop.close()
