"""交互通道的最小协议。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from .conversation import ConversationRef
from .session_event import SessionEvent


@dataclass(frozen=True)
class ChannelMessage:
    """通道收到的、已规整的消息。"""

    conversation_id: str
    message_id: str
    thread_id: str | None
    text: str
    sender_id: str


MessageHandler = Callable[[ChannelMessage], Awaitable[None]]
OutputStatus = Literal["running", "done", "error", "stopped"]


class StreamingOutput(Protocol):
    """一个 agent 回合的流式输出呈现。"""

    def feed(self, text: str) -> None: ...

    def set_footer(self, footer: str) -> None: ...

    async def flush(self) -> None: ...

    async def set_status(self, status: OutputStatus) -> None: ...

    async def aclose(self) -> None: ...


class Channel(Protocol):
    """一个交互通道实例的最小能力。"""

    def start(self, on_message: MessageHandler) -> None: ...

    def stop(self) -> None: ...

    def is_alive(self) -> bool: ...

    def restart(self) -> None: ...

    def create_thread(self, conversation_id: str, initial_text: str) -> str:
        """在指定会话中创建交互线程并返回线程标识。"""
        ...

    def send_text(self, conversation_id: str, text: str) -> str: ...

    def reply_text(
        self,
        target_id: str,
        text: str,
        *,
        threaded: bool = False,
    ) -> str: ...

    def handle_session_event(
        self,
        conversation_id: str,
        event: SessionEvent,
        *,
        trace_sequence: int | None = None,
    ) -> None: ...

    def open_output(
        self,
        conversation: ConversationRef,
        title: str,
        *,
        footer: str = "",
    ) -> StreamingOutput: ...
