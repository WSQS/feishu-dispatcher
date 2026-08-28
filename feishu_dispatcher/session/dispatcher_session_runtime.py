"""Dispatcher 的 SessionRuntime 实现。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone

from ..conversation import ConversationRef
from ..scheduler import LLMClient, SchedulerMemory, ToolSpec, run_tool_loop
from ..session_event import (
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


class DispatcherSessionRuntime(SessionRuntime):
    """以现有 scheduler tool loop 驱动 Dispatcher Session。"""

    def __init__(
        self,
        *,
        session_id: str,
        llm_provider: LLMProvider,
        memory: SchedulerMemory,
        tools_provider: ToolsProvider,
    ) -> None:
        if not session_id:
            raise ValueError("session_id 不能为空")
        self._session_id = session_id
        self._llm_provider = llm_provider
        self._memory = memory
        self._tools_provider = tools_provider
        self._state: SessionState = "idle"
        self._listeners: list[SessionEventListener] = []
        self._pending: deque[TurnRequest] = deque()
        self._results: dict[str, asyncio.Future[str]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._current_task: asyncio.Task[str] | None = None
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
        """接受一轮 Dispatcher 输入，并由单消费者顺序执行。"""
        if self._closed:
            raise RuntimeError(f"Session {self.session_id} 已关闭")
        if not request.text:
            raise ValueError("TurnRequest.text 不能为空")
        if request.turn_id in self._results:
            raise ValueError(f"turn_id 已存在: {request.turn_id}")

        self._emit_event(
            request.turn_id,
            SessionInputAccepted(text=request.text, source=request.conversation),
        )
        placement = (
            "current"
            if self._current_task is None and not self._pending
            else "pending"
        )
        self._results[request.turn_id] = asyncio.get_running_loop().create_future()
        self._pending.append(request)
        self._idle.clear()
        self._ensure_worker()
        return TurnReceipt(
            turn=TurnRef(self.session_id, request.turn_id),
            placement=placement,
        )

    async def wait_turn(self, turn: TurnRef) -> str:
        """等待指定 Dispatcher Turn 完成并返回回复。"""
        if turn.session_id != self.session_id:
            raise ValueError(f"Turn 不属于 Session {self.session_id}: {turn.session_id}")
        try:
            future = self._results[turn.turn_id]
        except KeyError as exc:
            raise ValueError(f"未知 turn_id: {turn.turn_id}") from exc
        try:
            return await asyncio.shield(future)
        finally:
            if future.done():
                self._results.pop(turn.turn_id, None)

    async def cancel(self) -> None:
        """取消当前 Dispatcher Turn；无当前 Turn 时幂等。"""
        current = self._current_task
        if current is not None and not current.done():
            current.cancel()

    async def wait_idle(self) -> None:
        """等待当前和已接受的 Dispatcher Turn 全部完成。"""
        await self._idle.wait()

    async def close(self) -> None:
        """关闭 Runtime，取消当前 Turn 并丢弃尚未执行的输入。"""
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        for future in self._results.values():
            if not future.done():
                future.cancel()
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
                future = self._results[request.turn_id]
                self._set_state("running")
                self._emit_event(request.turn_id, AgentOutputStarted())
                self._current_task = asyncio.create_task(self._execute_turn(request))
                try:
                    result = await self._current_task
                except asyncio.CancelledError:
                    self._emit_event(
                        request.turn_id,
                        AgentOutputFinished(
                            message="",
                            thought="",
                            outcome="cancelled",
                        ),
                    )
                    if not future.done():
                        future.cancel()
                else:
                    if not future.done():
                        future.set_result(result)
                finally:
                    self._current_task = None
        finally:
            self._set_state("stopped" if self._closed else "idle")
            self._idle.set()

    async def _execute_turn(self, request: TurnRequest) -> str:
        turn: list[dict] | None = None
        outcome: OutputOutcome = "completed"
        try:
            llm = self._llm_provider()
            if llm is None:
                raise RuntimeError("调度器 LLM 未配置")
            reply, turn = await run_tool_loop(
                llm,
                request.text,
                self._tools_provider(request.conversation),
                history=self._memory.history(),
            )
        except Exception as exc:
            outcome = "failed"
            logger.exception("调度器 LLM 失败")
            self._emit_event(
                request.turn_id,
                SessionErrorOccurred(phase="execute_turn", message=str(exc)),
            )
            reply = f"调度器出错：{str(exc)[:200]}。可用 `/run <项目> <任务>` 直接派发。"
        reply = reply or "（调度器无输出）"
        if turn:
            self._memory.add_turn(turn)
        else:
            self._memory.add_exchange(request.text, reply)
        self._emit_event(
            request.turn_id,
            AgentOutputFinished(message=reply, thought="", outcome=outcome),
        )
        return reply

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
