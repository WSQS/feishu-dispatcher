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
from pathlib import Path

import pytest

from feishu_dispatcher import __version__
from feishu_dispatcher.channel import ChannelMessage
from feishu_dispatcher.http_channel import HttpChannel, ensure_token


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        return holder.getsockname()[1]


def _raw_request(url: str) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, dict(response.headers.items()), response.read()


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
        assert 'id="task-list"' in html
        assert 'id="timelines"' in html
        for element_id in re.findall(
            r'document\.querySelector\("#([^"]+)"\)',
            javascript,
        ):
            assert f'id="{element_id}"' in html
        assert "/api/channel/messages" in javascript
        assert "/api/channel/events" in javascript
        assert 'apiRequest("/api/tasks")' in javascript
        assert "elements.connectionSettings.open = false;" in javascript
        assert "/conversations`" in javascript
        assert "thread_id: threadId" in javascript
        assert "const taskThreads = new Map();" in javascript
        assert "feishu-dispatcher.http-channel.conversation" in javascript
        assert "feishu-dispatcher.http-channel.cursor" in javascript
        assert "feishu-dispatcher.http-channel.thread" not in javascript
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
        assert payload == {"ok": True, "channel": "http", "version": __version__}
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
        assert expired_status == 409
        assert expired["error"] == "cursor_expired"
        assert valid_status == 200
        assert [event["text"] for event in valid["events"]] == ["two", "three"]
        assert invalid_status == 409
        assert invalid["error"] == "cursor_invalid"
        assert capacity_status == 429
        assert capacity["error"] == "conversation_capacity"
    finally:
        channel.stop()


async def test_thread_reply_streaming_output_and_restart():
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
        output.feed("hello")
        output.feed(" world")
        await asyncio.sleep(0.03)
        output.set_footer("model:b")
        await output.set_status("done")
        await output.aclose()
        events = await _wait_for_events(channel, "browser-a", minimum=5)
        event_types = [event["type"] for event in events["events"]]
        assert event_types == [
            "thread.created",
            "message.created",
            "output.started",
            "output.delta",
            "output.updated",
        ]
        assert events["events"][3]["text"] == "hello world"
        assert events["events"][4]["footer"] == "model:b"
        assert events["events"][4]["status"] == "done"

        first_thread = channel._thread
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
        health_status, _ = await asyncio.to_thread(
            _request,
            "GET",
            channel.base_url + "/api/channel/health",
            "tok-http",
        )
        assert health_status == 200
    finally:
        final_thread = channel._thread
        channel.stop()
        if final_thread is not None:
            assert not final_thread.is_alive()


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
