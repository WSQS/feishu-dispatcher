from feishu_dispatcher.conversation import ConversationRef


def test_conversation_ref_scopes_conversation_id_by_channel() -> None:
    feishu = ConversationRef(channel_key="feishu", conversation_id="main")
    web = ConversationRef(channel_key="web", conversation_id="main")

    assert feishu != web
    assert len({feishu, web}) == 2
