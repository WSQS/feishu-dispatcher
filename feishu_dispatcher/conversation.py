"""Conversation 领域值对象。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, init=False)
class ConversationRef:
    """一个 Channel 内会话的稳定引用。"""

    _channel_key: str
    conversation_id: str

    def __init__(self, channel_key: str, conversation_id: str) -> None:
        object.__setattr__(self, "_channel_key", channel_key)
        object.__setattr__(self, "conversation_id", conversation_id)

    def channel_key(self) -> str:
        """返回持有该会话的 Channel 路由键。"""
        return self._channel_key
