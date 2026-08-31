"""测试用的通用 ConversationRef 实现。"""

from __future__ import annotations

from dataclasses import dataclass

from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.feishu import FeishuConversationRef
from feishu_dispatcher.http_channel import HttpConversationRef


@dataclass(frozen=True)
class FakeConversationRef:
    channel: str
    conversation_id: str

    def channel_key(self) -> str:
        return self.channel

    def to_log_string(self) -> str:
        return f"{self.channel}:{self.conversation_id}"


def conversation_ref(channel: str, conversation_id: str) -> ConversationRef:
    if channel == "feishu":
        return FeishuConversationRef(conversation_id)
    if channel == "http":
        return HttpConversationRef(conversation_id)
    return FakeConversationRef(channel, conversation_id)


class ConversationRefFactory:
    def __new__(
        cls,
        channel: str,
        conversation_id: str,
    ) -> FakeConversationRef:
        return FakeConversationRef(channel, conversation_id)


class ChannelConversationRefFactory:
    def __new__(
        cls,
        channel: str,
        conversation_id: str,
    ) -> ConversationRef:
        return conversation_ref(channel, conversation_id)
