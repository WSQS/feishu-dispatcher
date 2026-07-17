"""飞书事件解析的单元测试。"""

from __future__ import annotations

import json

from feishu_dispatcher.feishu import FeishuBridge, IncomingMessage


def _event(*, message_id: str, root_id: str | None, content: dict | str,
           chat_type: str = "group", message_type: str = "text") -> dict:
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