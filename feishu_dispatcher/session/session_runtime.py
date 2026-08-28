"""Session Runtime 的统一输入与生命周期协议。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Literal, Protocol

from ..conversation import ConversationRef
from ..session_event import SessionState


@dataclass(frozen=True)
class TurnRequest:
    """一轮 agent 输入及其完整会话引用。"""

    text: str
    conversation: ConversationRef
    turn_id: str = field(default_factory=lambda: secrets.token_hex(16))


@dataclass(frozen=True)
class TurnRef:
    """一个已接受 Turn 的稳定引用。"""

    session_id: str
    turn_id: str


TurnDisposition = Literal["running", "pending"]


@dataclass(frozen=True)
class TurnReceipt:
    """Runtime 接受 Turn 后返回的排队结果。"""

    turn: TurnRef
    disposition: TurnDisposition
    queue_position: int | None = None


class AgentLoop(Protocol):
    """由 Runtime 驱动的一种具体 Agent 执行策略。"""

    async def run_turn(self, request: TurnRequest) -> None: ...

    async def cancel(self) -> None: ...

    async def close(self) -> None: ...


class SessionRuntime(Protocol):
    """统一的 Session 输入与运行时生命周期接口。"""

    @property
    def session_id(self) -> str: ...

    @property
    def state(self) -> SessionState: ...

    def submit(self, request: TurnRequest) -> TurnReceipt:
        """接受并排队一轮输入，不等待执行完成。"""
        ...

    async def cancel(self, turn_id: str | None = None) -> None: ...

    async def wait_idle(self) -> None: ...

    async def close(self) -> None: ...
