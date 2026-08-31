"""Conversation 引用的领域接口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ConversationRef(Protocol):
    """由具体 Channel 实现的、对外不透明的会话引用。"""

    def channel_key(self) -> str:
        """返回持有该会话的 Channel 路由键。"""
        ...

    def to_log_string(self) -> str:
        """返回用于日志记录的稳定会话标识。"""
        ...
