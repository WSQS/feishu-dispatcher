"""基于 LLM 工具循环的通用 SessionRuntime 实现。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol

from ..conversation import ConversationRef
from ..llm import llm_log_context
from ..scheduler import LLMClient, ToolSpec, run_tool_loop
from ..session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputStarted,
    OutputOutcome,
    SessionErrorOccurred,
    SessionEvent,
    SessionEventBody,
    SessionInputAccepted,
    SessionState,
    SessionStateChanged,
)
from .session_runtime import (
    SessionEventListener,
    SessionRuntime,
    TurnReceipt,
    TurnRef,
    TurnRequest,
)

logger = logging.getLogger(__name__)

LLMProvider = Callable[[], LLMClient | None]
ToolsProvider = Callable[[ConversationRef], list[ToolSpec]]
ErrorReplyFactory = Callable[[Exception], str]


class SessionMemory(Protocol):
    """Tool Loop Runtime 所需的最小会话记忆能力。"""

    def history(self) -> list[dict[str, Any]]: ...

    def add_turn(self, messages: list[dict[str, Any]]) -> None: ...

    def add_exchange(self, user_message: str, assistant_reply: str) -> None: ...


class ToolLoopSessionRuntime(SessionRuntime):
    """以可注入的 LLM、工具和提示词驱动一个 Session。"""

    def __init__(
        self,
        *,
        session_id: str,
        llm_provider: LLMProvider,
        memory: SessionMemory,
        tools_provider: ToolsProvider,
        system_prompt: str,
        llm_unavailable_message: str,
        error_reply_factory: ErrorReplyFactory,
        empty_reply: str,
        max_iterations_reply: str,
        log_context: str,
    ) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        self._session_id = session_id
        self._llm_provider = llm_provider
        self._memory = memory
        self._tools_provider = tools_provider
        self._system_prompt = system_prompt
        self._llm_unavailable_message = llm_unavailable_message
        self._error_reply_factory = error_reply_factory
        self._empty_reply = empty_reply
        self._max_iterations_reply = max_iterations_reply
        self._log_context = log_context
        self._state: SessionState = "idle"
        self._listeners: list[SessionEventListener] = []
        self._pending: deque[TurnRequest] = deque()
        self._worker: asyncio.Task[None] | None = None
        self._current_task: asyncio.Task[None] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def state(self) -> SessionState:
        return self._state

    def subscribe(self, listener: SessionEventListener) -> Callable[[], None]:
        """订阅 Session 事件并返回取消订阅函数。"""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def submit(self, request: TurnRequest) -> TurnReceipt:
        """接受一轮输入，并由单消费者顺序执行。"""
        if self._closed:
            raise RuntimeError(f"Session {self.session_id} 已关闭")
        if not request.text:
            raise ValueError("TurnRequest.text 不能为空")

        self._emit_event(
            request.turn_id,
            SessionInputAccepted(text=request.text, source=request.conversation),
        )
        placement = (
            "current" if self._current_task is None and not self._pending else "pending"
        )
        self._pending.append(request)
        self._idle.clear()
        self._ensure_worker()
        return TurnReceipt(
            turn=TurnRef(self.session_id, request.turn_id),
            placement=placement,
        )

    async def cancel(self) -> None:
        """取消当前 Turn；无当前 Turn 时幂等。"""
        current = self._current_task
        if current is not None and not current.done():
            current.cancel()

    async def wait_idle(self) -> None:
        """等待当前和已接受的 Turn 全部完成。"""
        await self._idle.wait()

    async def close(self) -> None:
        """关闭 Runtime，取消当前 Turn 并丢弃尚未执行的输入。"""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        await self.cancel()
        if self._worker is not None:
            await self._worker
        self._set_state("stopped")
        self._idle.set()

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while self._pending and not self._closed:
                request = self._pending.popleft()
                self._set_state("running")
                self._emit_event(request.turn_id, AgentOutputStarted())
                self._current_task = asyncio.create_task(self._execute_turn(request))
                try:
                    await self._current_task
                except asyncio.CancelledError:
                    self._emit_event(
                        request.turn_id,
                        AgentOutputFinished(
                            message="",
                            thought="",
                            outcome="cancelled",
                        ),
                    )
                finally:
                    self._current_task = None
        finally:
            self._set_state("stopped" if self._closed else "idle")
            self._idle.set()

    async def _execute_turn(self, request: TurnRequest) -> None:
        turn: list[dict] | None = None
        outcome: OutputOutcome = "completed"
        try:
            llm = self._llm_provider()
            if llm is None:
                raise RuntimeError(self._llm_unavailable_message)
            with llm_log_context(f"{self._log_context} session={self.session_id}"):
                reply, turn = await run_tool_loop(
                    llm,
                    request.text,
                    self._tools_provider(request.conversation),
                    system_prompt=self._system_prompt,
                    history=self._memory.history(),
                    max_iterations_reply=self._max_iterations_reply,
                    log_context=self._log_context,
                )
        except Exception as exc:
            outcome = "failed"
            logger.exception(
                "%s Session 执行失败 session=%s",
                self._log_context,
                self.session_id,
            )
            self._emit_event(
                request.turn_id,
                SessionErrorOccurred(phase="execute_turn", message=str(exc)),
            )
            reply = self._error_reply_factory(exc)
        reply = reply or self._empty_reply
        if turn:
            self._memory.add_turn(turn)
        else:
            self._memory.add_exchange(request.text, reply)
        self._emit_event(
            request.turn_id,
            AgentOutputDelta(stream="message", text=reply),
        )
        self._emit_event(
            request.turn_id,
            AgentOutputFinished(message=reply, thought="", outcome=outcome),
        )

    def _set_state(self, state: SessionState) -> None:
        previous_state = self._state
        if previous_state == state:
            return
        self._state = state
        self._emit_event(
            None,
            SessionStateChanged(
                previous_state=previous_state,
                current_state=state,
            ),
        )

    def _emit_event(self, turn_id: str | None, body: SessionEventBody) -> None:
        event = SessionEvent(
            event_id=secrets.token_hex(16),
            session_id=self.session_id,
            turn_id=turn_id,
            occurred_at=datetime.now(timezone.utc),
            body=body,
        )
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                logger.exception(
                    "SessionEvent 订阅者处理失败 event=%s",
                    event.event_id,
                )
