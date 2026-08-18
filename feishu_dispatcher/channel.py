"""交互通道的最小协议。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChannelMessage:
    """通道收到的、已规整的消息。"""

    conversation_id: str
    message_id: str
    thread_id: str | None
    text: str
    sender_id: str


MessageHandler = Callable[[ChannelMessage], Awaitable[None]]


class Channel(Protocol):
    """一个交互通道实例的最小能力。"""

    def start(self, on_message: MessageHandler) -> None: ...

    def stop(self) -> None: ...

    def is_alive(self) -> bool: ...

    def restart(self) -> None: ...

    def send_text(self, conversation_id: str, text: str) -> str: ...

    def reply_text(
        self,
        target_id: str,
        text: str,
        *,
        threaded: bool = False,
    ) -> str: ...

    def send_card(self, thread_id: str, card: dict) -> str: ...

    def update_card(self, message_id: str, card: dict) -> None: ...
