"""飞书事件解析与合包的单元测试。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import pytest

from feishu_dispatcher.channel import ChannelMessage
from feishu_dispatcher.feishu import FeishuBridge, _RateLimiter
from feishu_dispatcher.livecard import LiveCard
from feishu_dispatcher.session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputStarted,
    AgentPlanEntry,
    AgentPlanUpdated,
    SessionEvent,
    SessionInputAccepted,
    ToolCallObserved,
)
from feishu_dispatcher.throttler import StreamThrottler
from tests.conversation_fakes import (
    ChannelConversationRefFactory as ConversationRef,
)


def _event(
    *,
    message_id: str,
    root_id: str | None,
    content: dict | str,
    chat_type: str = "group",
    message_type: str = "text",
    chat_id: str = "oc_chat1",
    sender_id: str = "ou_test",
) -> dict:
    if isinstance(content, dict):
        content = json.dumps(content)
    return {
        "sender": {"sender_id": {"open_id": sender_id, "user_id": "u1"}},
        "message": {
            "message_id": message_id,
            "root_id": root_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "message_type": message_type,
            "content": content,
        },
    }


def _event_payload(event: dict) -> bytes:
    return json.dumps(
        {
            "header": {"event_type": "im.message.receive_v1"},
            "event": event,
        }
    ).encode()


def test_parse_root_message_has_no_thread_root():
    msg = FeishuBridge._parse_event_message(
        _event(message_id="om_root", root_id=None, content={"text": "hello"})
    )
    assert msg == ChannelMessage(
        conversation=ConversationRef("feishu", "oc_chat1"),
        message_id="om_root",
        text="hello",
        sender_id="ou_test",
    )


def test_parse_thread_reply_thread_root_is_root_id():
    msg = FeishuBridge._parse_event_message(
        _event(
            message_id="om_reply",
            root_id="om_root",
            content={"text": "agent plz do X"},
        )
    )
    assert msg.conversation == ConversationRef("feishu", "om_root")
    assert msg.message_id == "om_reply"


def test_parse_message_where_root_id_equals_message_id_is_root():
    msg = FeishuBridge._parse_event_message(
        _event(message_id="om_root", root_id="om_root", content={"text": "x"})
    )
    assert msg.conversation == ConversationRef("feishu", "oc_chat1")


def test_parse_non_text_message_returns_none_and_logs(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="feishu_dispatcher.feishu"):
        msg = FeishuBridge._parse_event_message(
            _event(
                message_id="om_img",
                root_id=None,
                content={"image_key": "k"},
                message_type="image",
            )
        )
    assert msg is None
    # 打日志而非静默丢弃：能看出「发了图但没反应」是因为暂不支持非文本
    assert "非文本" in caplog.text
    assert "image" in caplog.text and "om_img" in caplog.text


def test_parse_p2p_message_returns_none_and_logs(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="feishu_dispatcher.feishu"):
        msg = FeishuBridge._parse_event_message(
            _event(
                message_id="om_p2p",
                root_id=None,
                content={"text": "hi"},
                chat_type="p2p",
            )
        )
    assert msg is None
    assert "非群" in caplog.text and "om_p2p" in caplog.text


def _post_content(paragraphs: list, *, title: str = "", locale: str = "zh_cn") -> dict:
    return {"post": {locale: {"title": title, "content": paragraphs}}}


def test_parse_post_message_extracts_text():
    # 富文本：两段，含 text / a / at 混排
    content = _post_content(
        [
            [
                {"tag": "text", "text": "帮我改一下 "},
                {"tag": "a", "text": "这个文件", "href": "http://x"},
            ],
            [{"tag": "at", "user_id": "ou_bot"}, {"tag": "text", "text": "加日志"}],
        ]
    )
    msg = FeishuBridge._parse_event_message(
        _event(
            message_id="om_post",
            root_id="om_root",
            content=content,
            message_type="post",
        )
    )
    assert msg is not None
    assert msg.text == "帮我改一下 这个文件\n加日志"  # 段落间换行、run 文本拼接
    assert msg.conversation == ConversationRef("feishu", "om_root")


def test_parse_post_direct_body_received_shape():
    # 收到的 post 事件多为「直接 body」（无 {"post":{"<locale>":...}} 包裹）
    content = {
        "title": "",
        "content": [[{"tag": "text", "text": "1. 测试一下你可以收到吗？"}]],
    }
    msg = FeishuBridge._parse_event_message(
        _event(
            message_id="om_direct",
            root_id="om_root",
            content=content,
            message_type="post",
        )
    )
    assert msg is not None
    assert msg.text == "1. 测试一下你可以收到吗？"


def test_parse_post_with_title_and_other_locale():
    content = _post_content(
        [[{"tag": "text", "text": "正文"}]], title="标题", locale="en_us"
    )
    msg = FeishuBridge._parse_event_message(
        _event(message_id="om_p2", root_id=None, content=content, message_type="post")
    )
    assert msg is not None
    assert msg.text == "标题\n正文"  # title 作首行；locale 非 zh_cn 也能取


def test_parse_invalid_content_json_still_returns_empty_text():
    msg = FeishuBridge._parse_event_message(
        _event(message_id="om_bad", root_id=None, content="not-json{")
    )
    assert msg is not None
    assert msg.text == ""


# ---------------------------------------------------------------------- #
# #36：出站令牌桶
# ---------------------------------------------------------------------- #


class _FakeClock:
    """可控时钟：sleep 直接推进时间，令牌桶测试无需真等。"""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, d: float) -> None:
        self.sleeps.append(d)
        self.t += d


def test_rate_limiter_paces_after_capacity():
    c = _FakeClock()
    rl = _RateLimiter(5.0, capacity=1.0, _now=c.now, _sleep=c.sleep)
    rl.acquire()  # 首个令牌免费（capacity=1）
    rl.acquire()  # 空了 → 等 1/5 = 0.2s
    rl.acquire()  # 再等 0.2s
    assert c.sleeps == [0.2, 0.2]


def test_rate_limiter_bursts_up_to_capacity():
    c = _FakeClock()
    rl = _RateLimiter(5.0, capacity=3.0, _now=c.now, _sleep=c.sleep)
    for _ in range(3):
        rl.acquire()  # 3 个突发令牌，不睡
    assert c.sleeps == []
    rl.acquire()  # 第 4 个 → 睡 0.2s
    assert c.sleeps == [0.2]


def test_rate_limiter_disabled_when_zero():
    c = _FakeClock()
    rl = _RateLimiter(0, _now=c.now, _sleep=c.sleep)
    for _ in range(20):
        rl.acquire()
    assert c.sleeps == []  # rate<=0 关闭限流，从不 sleep


def test_bridge_has_limiter_wired():
    bridge = make_bridge()
    assert bridge._limiter._rate == 5.0  # 默认 qps


# ---------------------------------------------------------------------- #
# Channel 接口映射
# ---------------------------------------------------------------------- #


def test_channel_start_registers_handler_before_starting(monkeypatch):
    bridge = make_bridge()

    async def handler(_msg):
        pass

    started: list[bool] = []

    def start_background() -> None:
        assert bridge._on_event is handler
        started.append(True)

    monkeypatch.setattr(bridge, "start_background", start_background)
    bridge.start(handler)

    assert started == [True]


async def test_channel_rejects_non_whitelisted_chat():
    received: list[ChannelMessage] = []

    async def handler(msg: ChannelMessage) -> None:
        received.append(msg)

    bridge = FeishuBridge(
        app_id="a",
        app_secret="b",
        main_loop=asyncio.get_running_loop(),
        on_event=handler,
        chat_whitelist="oc_allowed",
        sender_whitelist=["ou_allowed"],
    )

    bridge._dispatch_event(
        _event_payload(
            _event(
                message_id="om_other_chat",
                root_id=None,
                content={"text": "hello"},
                chat_id="oc_other",
                sender_id="ou_allowed",
            )
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert received == []


async def test_channel_rejects_non_whitelisted_sender():
    received: list[ChannelMessage] = []

    async def handler(msg: ChannelMessage) -> None:
        received.append(msg)

    bridge = FeishuBridge(
        app_id="a",
        app_secret="b",
        main_loop=asyncio.get_running_loop(),
        on_event=handler,
        chat_whitelist="oc_allowed",
        sender_whitelist=["ou_allowed"],
    )

    bridge._dispatch_event(
        _event_payload(
            _event(
                message_id="om_other_sender",
                root_id=None,
                content={"text": "hello"},
                chat_id="oc_allowed",
                sender_id="ou_other",
            )
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert received == []


async def test_channel_accepts_whitelisted_chat_and_sender():
    received: list[ChannelMessage] = []
    delivered = asyncio.Event()

    async def handler(msg: ChannelMessage) -> None:
        received.append(msg)
        delivered.set()

    bridge = FeishuBridge(
        app_id="a",
        app_secret="b",
        main_loop=asyncio.get_running_loop(),
        on_event=handler,
        chat_whitelist="oc_allowed",
        sender_whitelist=["ou_allowed"],
    )

    bridge._dispatch_event(
        _event_payload(
            _event(
                message_id="om_allowed",
                root_id=None,
                content={"text": "hello"},
                chat_id="oc_allowed",
                sender_id="ou_allowed",
            )
        )
    )
    await asyncio.wait_for(delivered.wait(), timeout=1)

    assert received == [
        ChannelMessage(
            conversation=ConversationRef("feishu", "oc_allowed"),
            message_id="om_allowed",
            text="hello",
            sender_id="ou_allowed",
        )
    ]


def test_channel_stop_cancels_websocket_task():
    bridge = make_bridge()

    class FakeTask:
        cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    class FakeLoop:
        def call_soon_threadsafe(self, callback) -> None:
            callback()

    task = FakeTask()
    bridge._ws_loop = FakeLoop()
    bridge._ws_task = task

    bridge.stop()

    assert bridge._stopping.is_set()
    assert task.cancelled


def test_channel_send_text_delegates_to_root_message(monkeypatch):
    bridge = make_bridge(chat_whitelist="oc_chat")
    calls: list[tuple[str, str]] = []

    def send_root_message(conversation_id: str, text: str) -> str:
        calls.append((conversation_id, text))
        return "om_root"

    monkeypatch.setattr(bridge, "send_root_message", send_root_message)

    assert bridge.send_text(ConversationRef("feishu", "oc_chat"), "hello") == "om_root"
    assert calls == [("oc_chat", "hello")]


def test_channel_create_thread_delegates_to_root_message(monkeypatch):
    bridge = make_bridge(chat_whitelist="oc_chat")
    calls: list[tuple[str, str]] = []

    def send_root_message(conversation_id: str, initial_text: str) -> str:
        calls.append((conversation_id, initial_text))
        return "om_root"

    monkeypatch.setattr(bridge, "send_root_message", send_root_message)

    assert bridge.create_thread("hello") == ConversationRef("feishu", "om_root")
    assert calls == [("oc_chat", "hello")]


def test_feishu_conversation_ref_codec_round_trips() -> None:
    bridge = make_bridge()
    conversation = ConversationRef("feishu", "om_root")

    payload = bridge.serialize_conversation_ref(conversation)

    assert payload == {"conversation_id": "om_root"}
    assert bridge.deserialize_conversation_ref(payload) == conversation


def test_feishu_conversation_ref_codec_normalizes_serialized_id() -> None:
    bridge = make_bridge()

    assert bridge.serialize_conversation_ref(
        ConversationRef("feishu", "  om_root  ")
    ) == {"conversation_id": "om_root"}


def test_feishu_conversation_ref_codec_rejects_other_channel() -> None:
    bridge = make_bridge()

    with pytest.raises(ValueError, match="不属于 Feishu Channel"):
        bridge.serialize_conversation_ref(ConversationRef("http", "http-thread"))


def test_feishu_conversation_ref_codec_rejects_blank_serialized_id() -> None:
    bridge = make_bridge()

    with pytest.raises(ValueError, match="ConversationRef 不能为空"):
        bridge.serialize_conversation_ref(ConversationRef("feishu", " "))


@pytest.mark.parametrize("payload", [{}, {"conversation_id": " "}])
def test_feishu_conversation_ref_codec_rejects_invalid_payload(
    payload: dict[str, object],
) -> None:
    bridge = make_bridge()

    with pytest.raises(ValueError, match="ConversationRef payload 无效"):
        bridge.deserialize_conversation_ref(payload)


def test_channel_send_text_selects_root_or_thread(monkeypatch):
    bridge = make_bridge(chat_whitelist="oc_chat")
    calls: list[tuple[str, str, str]] = []

    def reply(target_id: str, text: str) -> str:
        calls.append(("plain", target_id, text))
        return "om_plain"

    def reply_in_thread(target_id: str, text: str) -> str:
        calls.append(("threaded", target_id, text))
        return "om_threaded"

    monkeypatch.setattr(bridge, "reply_in_thread", reply_in_thread)

    monkeypatch.setattr(bridge, "send_root_message", reply)
    assert bridge.send_text(ConversationRef("feishu", "oc_chat"), "plain") == "om_plain"
    assert (
        bridge.send_text(ConversationRef("feishu", "om_root"), "threaded")
        == "om_threaded"
    )
    assert calls == [
        ("plain", "oc_chat", "plain"),
        ("threaded", "om_root", "threaded"),
    ]


def test_channel_projects_session_input_event_to_thread(monkeypatch):
    bridge = make_bridge()
    calls: list[tuple[str, str, bool]] = []

    def send_text(conversation: ConversationRef, text: str) -> str:
        calls.append((conversation.conversation_id, text, True))
        return "om_reply"

    monkeypatch.setattr(bridge, "send_text", send_text)
    event = SessionEvent(
        event_id="event-1",
        session_id="t1",
        turn_id="turn-1",
        occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        body=SessionInputAccepted(
            text="检查状态",
            source=ConversationRef("http", "http-thread"),
        ),
    )

    bridge.handle_session_event(ConversationRef("feishu", "om_root"), event)

    assert calls == [("om_root", "↪️ 同步自 http：检查状态", True)]


def test_channel_projects_session_input_event_to_root_chat(monkeypatch):
    loop = asyncio.new_event_loop()
    try:
        bridge = FeishuBridge(
            app_id="a",
            app_secret="b",
            main_loop=loop,
            chat_whitelist="oc_main",
        )
        calls: list[tuple[str, str]] = []

        def send_text(conversation: ConversationRef, text: str) -> str:
            calls.append((conversation.conversation_id, text))
            return "om_root"

        monkeypatch.setattr(bridge, "send_text", send_text)
        event = SessionEvent(
            event_id="event-root",
            session_id="dispatcher",
            turn_id="turn-root",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=SessionInputAccepted(
                text="检查状态",
                source=ConversationRef("http", "http-thread"),
            ),
        )

        bridge.handle_session_event(ConversationRef("feishu", "oc_main"), event)

        assert calls == [("oc_main", "↪️ 同步自 http：检查状态")]
    finally:
        loop.close()


def test_channel_skips_empty_session_input_event(monkeypatch):
    bridge = make_bridge()
    calls: list[tuple[str, str, bool]] = []

    def send_text(conversation: ConversationRef, text: str) -> str:
        calls.append((conversation.conversation_id, text, True))
        return "om_reply"

    monkeypatch.setattr(bridge, "send_text", send_text)
    event = SessionEvent(
        event_id="event-empty",
        session_id="t1",
        turn_id="turn-empty",
        occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        body=SessionInputAccepted(text=""),
    )

    bridge.handle_session_event(ConversationRef("feishu", "om_root"), event)

    assert calls == []


def test_channel_accepts_agent_output_events_without_rendering(monkeypatch):
    bridge = make_bridge()
    calls: list[tuple[str, str, bool]] = []

    def send_text(conversation: ConversationRef, text: str) -> str:
        calls.append((conversation.conversation_id, text, True))
        return "om_reply"

    monkeypatch.setattr(bridge, "send_text", send_text)
    bodies = [
        AgentOutputStarted(),
        AgentOutputDelta(stream="message", text="answer"),
        AgentOutputFinished(message="answer", thought="", outcome="completed"),
        ToolCallObserved(
            tool_call_id="tc1",
            kind="execute",
            title="pytest",
            status="completed",
            detail="pytest -q",
        ),
    ]

    for index, body in enumerate(bodies, start=1):
        bridge.handle_session_event(
            ConversationRef("feishu", "om_root"),
            SessionEvent(
                event_id=f"event-{index}",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=body,
            ),
        )

    assert calls == []


def test_channel_projects_dispatcher_finished_without_open_output(monkeypatch):
    bridge = make_bridge()
    calls: list[tuple[str, str]] = []

    def send_text(conversation: ConversationRef, text: str) -> str:
        calls.append((conversation.conversation_id, text))
        return "om_reply"

    monkeypatch.setattr(bridge, "send_text", send_text)
    bridge.handle_session_event(
        ConversationRef("feishu", "om_root"),
        SessionEvent(
            event_id="event-dispatcher-finished",
            session_id="dispatcher",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=AgentOutputFinished(
                message="answer",
                thought="",
                outcome="completed",
            ),
        ),
    )

    assert calls == [("om_root", "answer")]


def test_feishu_card_methods_delegate_to_feishu_card_methods(monkeypatch):
    bridge = make_bridge()
    calls: list[tuple[str, str, dict]] = []

    def reply_card(thread_id: str, card: dict) -> str:
        calls.append(("send", thread_id, card))
        return "om_card"

    def patch_card(message_id: str, card: dict) -> None:
        calls.append(("update", message_id, card))

    monkeypatch.setattr(bridge, "reply_card", reply_card)
    monkeypatch.setattr(bridge, "patch_card", patch_card)
    card = {"header": {"title": "status"}}

    assert bridge.send_card("om_root", card) == "om_card"
    bridge.update_card("om_card", card)
    assert calls == [
        ("send", "om_root", card),
        ("update", "om_card", card),
    ]


def test_channel_open_output_uses_card_mode():
    bridge = make_bridge(stream_mode="card")

    output = bridge.open_output(
        ConversationRef("feishu", "om_root"), "demo", footer="project"
    )

    assert isinstance(output._output, LiveCard)


async def test_channel_open_output_uses_text_mode(monkeypatch):
    bridge = make_bridge(stream_mode="text", throttle_window=60.0)
    calls: list[tuple[str, str, bool]] = []

    def send_text(conversation: ConversationRef, text: str) -> str:
        calls.append((conversation.conversation_id, text, True))
        return "om_text"

    monkeypatch.setattr(bridge, "send_text", send_text)
    output = bridge.open_output(ConversationRef("feishu", "om_root"), "demo")

    assert isinstance(output._output, StreamThrottler)
    output.feed("legacy")
    await output.flush()
    await output.handle_event(
        SessionEvent(
            event_id="event-started",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=AgentOutputStarted(),
        )
    )
    await output.handle_event(
        SessionEvent(
            event_id="event-delta",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=AgentOutputDelta(stream="message", text="hello"),
        )
    )
    await output.handle_event(
        SessionEvent(
            event_id="event-finished",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=AgentOutputFinished(message="hello", thought="", outcome="completed"),
        )
    )
    await output.aclose()

    assert calls == [("om_root", "hello", True)]


async def test_text_output_is_driven_by_session_events(monkeypatch):
    bridge = FeishuBridge(
        app_id="a",
        app_secret="b",
        main_loop=asyncio.get_running_loop(),
        stream_mode="text",
        throttle_window=60.0,
    )
    calls: list[tuple[str, str, bool]] = []

    def send_text(conversation: ConversationRef, text: str) -> str:
        calls.append((conversation.conversation_id, text, True))
        return "om_text"

    monkeypatch.setattr(bridge, "send_text", send_text)
    output = bridge.open_output(ConversationRef("feishu", "om_root"), "demo")
    output.feed("legacy")

    events = [
        AgentOutputStarted(),
        AgentOutputDelta(stream="thought", text="thinking"),
        AgentOutputDelta(stream="message", text="answer"),
        AgentPlanUpdated(
            entries=(AgentPlanEntry(content="run tests", status="in_progress"),)
        ),
        ToolCallObserved(
            tool_call_id="tc1",
            kind="edit",
            title="Edit",
            status="started",
        ),
        ToolCallObserved(
            tool_call_id="tc1",
            kind="edit",
            title="Edit",
            status="completed",
        ),
        AgentOutputFinished(
            message="answer",
            thought="thinking",
            outcome="completed",
        ),
    ]
    for index, body in enumerate(events, start=1):
        await asyncio.to_thread(
            bridge.handle_session_event,
            ConversationRef("feishu", "om_root"),
            SessionEvent(
                event_id=f"event-{index}",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=body,
            ),
        )

    await output.aclose()

    assert calls == [
        (
            "om_root",
            "💭 thinking\nanswer\n📋 计划:\n🔄 run tests\n\n🔧 Edit\n✅ Edit\n",
            True,
        )
    ]


async def test_card_output_is_driven_by_session_events(monkeypatch):
    bridge = FeishuBridge(
        app_id="a",
        app_secret="b",
        main_loop=asyncio.get_running_loop(),
        stream_mode="card",
    )
    replies: list[tuple[str, dict]] = []
    patches: list[tuple[str, dict]] = []

    def reply_card(root_message_id: str, card: dict) -> str:
        replies.append((root_message_id, card))
        return "om_card"

    def patch_card(message_id: str, card: dict) -> None:
        patches.append((message_id, card))

    monkeypatch.setattr(bridge, "reply_card", reply_card)
    monkeypatch.setattr(bridge, "patch_card", patch_card)
    output = bridge.open_output(ConversationRef("feishu", "om_root"), "demo")
    output.feed("legacy")

    await asyncio.to_thread(
        bridge.handle_session_event,
        ConversationRef("feishu", "om_root"),
        SessionEvent(
            event_id="event-started",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=AgentOutputStarted(),
        ),
    )
    await asyncio.to_thread(
        bridge.handle_session_event,
        ConversationRef("feishu", "om_root"),
        SessionEvent(
            event_id="event-delta",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=AgentOutputDelta(stream="message", text="answer"),
        ),
    )
    await asyncio.to_thread(
        bridge.handle_session_event,
        ConversationRef("feishu", "om_root"),
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
    await asyncio.to_thread(
        bridge.handle_session_event,
        ConversationRef("feishu", "om_root"),
        SessionEvent(
            event_id="event-finished",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
            body=AgentOutputFinished(
                message="answer",
                thought="",
                outcome="completed",
            ),
        ),
    )
    await output.aclose()

    assert len(replies) == 1
    assert "answer" in replies[0][1]["body"]["elements"][0]["content"]
    assert "📋 计划:\n🔄 run tests" in replies[0][1]["body"]["elements"][0]["content"]
    assert "legacy" not in str(replies + patches)
    assert replies[0][1]["header"]["template"] == "green"


@pytest.mark.parametrize(
    ("outcome", "template"),
    [
        ("completed", "green"),
        ("cancelled", "grey"),
        ("failed", "red"),
    ],
)
async def test_card_output_maps_session_outcome(monkeypatch, outcome, template):
    bridge = FeishuBridge(
        app_id="a",
        app_secret="b",
        main_loop=asyncio.get_running_loop(),
        stream_mode="card",
    )
    cards: list[dict] = []

    def reply_card(_root_message_id: str, card: dict) -> str:
        cards.append(card)
        return "om_card"

    monkeypatch.setattr(bridge, "reply_card", reply_card)
    output = bridge.open_output(ConversationRef("feishu", "om_root"), "demo")
    for index, body in enumerate(
        [
            AgentOutputStarted(),
            AgentOutputFinished(message="answer", thought="", outcome=outcome),
        ],
        start=1,
    ):
        await asyncio.to_thread(
            bridge.handle_session_event,
            ConversationRef("feishu", "om_root"),
            SessionEvent(
                event_id=f"event-{index}",
                session_id="t1",
                turn_id="turn-1",
                occurred_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
                body=body,
            ),
        )
    await output.aclose()

    assert cards[-1]["header"]["template"] == template


# ---------------------------------------------------------------------- #
# 分片合包
# ---------------------------------------------------------------------- #


def make_bridge(
    *,
    stream_mode: str = "card",
    throttle_window: float = 0.5,
    chat_whitelist: str = "",
) -> FeishuBridge:
    async def _noop(_msg):  # pragma: no cover
        pass

    loop = asyncio.new_event_loop()
    try:
        return FeishuBridge(
            app_id="a",
            app_secret="b",
            main_loop=loop,
            on_event=_noop,
            stream_mode=stream_mode,
            throttle_window=throttle_window,
            chat_whitelist=chat_whitelist,
        )
    finally:
        loop.close()


def test_control_conversation_uses_whitelisted_chat(monkeypatch):
    bridge = make_bridge(chat_whitelist=" oc-control ")
    roots: list[tuple[str, str]] = []
    replies: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bridge,
        "send_root_message",
        lambda conversation_id, text: roots.append((conversation_id, text)) or "root",
    )
    monkeypatch.setattr(
        bridge,
        "reply_in_thread",
        lambda conversation_id, text: (
            replies.append((conversation_id, text)) or "reply"
        ),
    )

    conversation = bridge.control_conversation()

    assert conversation == ConversationRef("feishu", "oc-control")
    assert bridge.send_text(conversation, "notice") == "root"
    assert roots == [("oc-control", "notice")]
    assert replies == []


def test_control_conversation_is_optional():
    bridge = make_bridge()

    assert bridge.control_conversation() is None


def test_combine_assembles_fragments_in_seq_order():
    bridge = make_bridge()
    assert bridge._combine("m1", 3, 1, b"BB") is None
    assert bridge._combine("m1", 3, 0, b"AA") is None
    assert bridge._combine("m1", 3, 2, b"CC") == b"AABBCC"
    assert bridge._frag_cache == {}


def test_combine_accepts_empty_fragment():
    bridge = make_bridge()
    assert bridge._combine("m1", 2, 0, b"") is None
    assert bridge._combine("m1", 2, 1, b"X") == b"X"


def test_combine_prunes_expired_entries():
    bridge = make_bridge()
    assert bridge._combine("old", 2, 0, b"A") is None
    ts, buf = bridge._frag_cache["old"]
    bridge._frag_cache["old"] = (ts - bridge._FRAG_TTL - 1, buf)  # 人为过期
    assert bridge._combine("new", 2, 0, b"N") is None
    assert "old" not in bridge._frag_cache
    assert "new" in bridge._frag_cache


def test_combine_isolated_per_instance():
    b1, b2 = make_bridge(), make_bridge()
    assert b1._combine("m1", 2, 0, b"A") is None
    assert b2._frag_cache == {}  # 不再是类属性共享（R7）


def test_combine_uses_monotonic_timestamps():
    bridge = make_bridge()
    before = time.monotonic()
    bridge._combine("m1", 2, 0, b"A")
    ts, _ = bridge._frag_cache["m1"]
    assert before <= ts <= time.monotonic()


# ---------------------------------------------------------------------- #
# R13: WS 线程看门狗
# ---------------------------------------------------------------------- #


def test_is_alive_false_when_no_thread():
    bridge = make_bridge()
    assert bridge.is_alive() is False


def test_restart_noop_when_stopping():
    bridge = make_bridge()
    bridge._stopping.set()
    bridge.restart()  # 不应抛异常，也不应启动线程
    assert bridge._ws_thread is None


def test_restart_noop_when_already_alive():
    import threading

    bridge = make_bridge()
    # 模拟一个活着的线程
    bridge._ws_thread = threading.Thread(target=lambda: None, daemon=True)
    bridge._ws_thread.start()
    try:
        bridge.restart()
        # 线程引用不变（没有重启）
        assert bridge._ws_thread.is_alive()
    finally:
        bridge._ws_thread.join(timeout=1)


# ---------------------------------------------------------------------- #
# R14: HTTP 重试 Session
# ---------------------------------------------------------------------- #


def test_retry_session_configured():
    from urllib3.util.retry import Retry

    bridge = make_bridge()
    adapter = bridge._session.get_adapter("https://open.feishu.cn")
    retry = adapter.max_retries
    # requests 把 int 包成 Retry，把 Retry 原样保留
    assert isinstance(retry, Retry)
    assert retry.total == 3
    assert 429 in retry.status_forcelist
    assert 500 in retry.status_forcelist
    assert 503 in retry.status_forcelist
    assert "POST" in retry.allowed_methods
