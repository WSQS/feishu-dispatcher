from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.feishu import FeishuConversationRef
from feishu_dispatcher.http_channel import HttpConversationRef


def test_conversation_ref_scopes_conversation_id_by_channel() -> None:
    feishu = ConversationRef(channel_key="feishu", conversation_id="main")
    web = ConversationRef(channel_key="web", conversation_id="main")

    assert feishu.channel_key() == "feishu"
    assert web.channel_key() == "web"
    assert feishu.to_log_string() == "feishu:main"
    assert web.to_log_string() == "web:main"
    assert feishu != web
    assert len({feishu, web}) == 2


def test_channels_have_distinct_conversation_ref_types() -> None:
    assert FeishuConversationRef is not HttpConversationRef
    assert FeishuConversationRef.__supertype__ is ConversationRef
    assert HttpConversationRef.__supertype__ is ConversationRef
