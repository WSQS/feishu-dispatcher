from importlib.util import find_spec
from typing import get_type_hints

import pytest

import feishu_dispatcher.agent.channel as implementations
import feishu_dispatcher.agent.channel.feishu as feishu_module
import feishu_dispatcher.agent.channel.http as http_module
from feishu_dispatcher.agent.channel.feishu import FeishuConversationRef
from feishu_dispatcher.agent.channel.http import HttpConversationRef
from feishu_dispatcher.agent.session_event import SessionEvent
from feishu_dispatcher.channel import Channel, ChannelMessage
from feishu_dispatcher.conversation import ConversationRef


def test_promoted_contracts_have_one_authoritative_definition() -> None:
    assert ConversationRef.__module__ == "feishu_dispatcher.conversation"
    assert Channel.__module__ == "feishu_dispatcher.channel"
    assert ChannelMessage.__module__ == "feishu_dispatcher.channel"
    assert get_type_hints(ChannelMessage)["conversation"] is ConversationRef
    assert find_spec("feishu_dispatcher.agent.conversation") is None


def test_channel_implementations_use_promoted_message_type() -> None:
    assert feishu_module.ChannelMessage is ChannelMessage
    assert http_module.ChannelMessage is ChannelMessage
    assert feishu_module.ConversationRef is ConversationRef
    assert http_module.ConversationRef is ConversationRef


def test_channel_implementation_package_does_not_reexport_contracts() -> None:
    for name in ("Channel", "ChannelMessage", "MessageHandler", "OutputStatus"):
        assert not hasattr(implementations, name)


def test_channel_keeps_session_event_in_agent_zone() -> None:
    assert SessionEvent.__module__ == "feishu_dispatcher.agent.session_event"
    assert get_type_hints(Channel.handle_session_event)["event"] is SessionEvent
    assert find_spec("feishu_dispatcher.session_event") is None


def test_conversation_ref_scopes_conversation_id_by_channel() -> None:
    feishu = FeishuConversationRef("main")
    web = HttpConversationRef("main")

    assert feishu.channel_key() == "feishu"
    assert web.channel_key() == "http"
    assert feishu.to_log_string() == "feishu:main"
    assert web.to_log_string() == "http:main"
    assert feishu.conversation_id == "main"
    assert web.conversation_id == "main"
    assert feishu != web
    assert len({feishu, web}) == 2


def test_channels_have_distinct_conversation_ref_types() -> None:
    assert FeishuConversationRef is not HttpConversationRef
    assert isinstance(FeishuConversationRef("main"), ConversationRef)
    assert isinstance(HttpConversationRef("main"), ConversationRef)


def test_conversation_ref_is_an_interface() -> None:
    with pytest.raises(TypeError):
        ConversationRef()
