"""Conversation 领域值对象。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConversationRef:
    """一个 Channel 内会话的稳定引用。"""

    channel_key: str
    conversation_id: str
