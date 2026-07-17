"""飞书事件解析与合包的单元测试。"""

from __future__ import annotations

import asyncio
import json
import time

from feishu_dispatcher.feishu import FeishuBridge, IncomingMessage


def _event(
    *,
    message_id: str,
    root_id: str | None,
    content: dict | str,
    chat_type: str = "group",
    message_type: str = "text",
) -> dict:
    if isinstance(content, dict):
        content = json.dumps(content)
    return {
        "sender": {"sender_id": {"open_id": "ou_test", "user_id": "u1"}},
        "message": {
            "message_id": message_id,
            "root_id": root_id,
            "chat_id": "oc_chat1",
            "chat_type": chat_type,
            "message_type": message_type,
            "content": content,
        },
    }


def test_parse_root_message_has_no_thread_root():
    msg = FeishuBridge._parse_event_message(
        _event(message_id="om_root", root_id=None, content={"text": "hello"})
    )
    assert msg == IncomingMessage(
        chat_id="oc_chat1",
        message_id="om_root",
        thread_root_id=None,
        text="hello",
        chat_type="group",
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
    assert msg.thread_root_id == "om_root"
    assert msg.message_id == "om_reply"


def test_parse_message_where_root_id_equals_message_id_is_root():
    msg = FeishuBridge._parse_event_message(
        _event(message_id="om_root", root_id="om_root", content={"text": "x"})
    )
    assert msg.thread_root_id is None


def test_parse_non_text_message_returns_none():
    msg = FeishuBridge._parse_event_message(
        _event(
            message_id="om_img",
            root_id=None,
            content={"image_key": "k"},
            message_type="image",
        )
    )
    assert msg is None


def test_parse_p2p_message_returns_none():
    msg = FeishuBridge._parse_event_message(
        _event(
            message_id="om_p2p",
            root_id=None,
            content={"text": "hi"},
            chat_type="p2p",
        )
    )
    assert msg is None


def test_parse_invalid_content_json_still_returns_empty_text():
    msg = FeishuBridge._parse_event_message(
        _event(message_id="om_bad", root_id=None, content="not-json{")
    )
    assert msg is not None
    assert msg.text == ""


# ---------------------------------------------------------------------- #
# 分片合包
# ---------------------------------------------------------------------- #


def make_bridge() -> FeishuBridge:
    async def _noop(_msg):  # pragma: no cover
        pass

    loop = asyncio.new_event_loop()
    try:
        return FeishuBridge(app_id="a", app_secret="b", main_loop=loop, on_event=_noop)
    finally:
        loop.close()


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
