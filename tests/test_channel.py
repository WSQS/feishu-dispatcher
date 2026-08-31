import pytest

from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.feishu import FeishuConversationRef
from feishu_dispatcher.http_channel import HttpConversationRef


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
