"""ACP agent 的 SessionRuntime 实现。"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from typing import Literal

from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.session_runtime import (
    SessionEventListener,
    SessionRuntime,
    TurnReceipt,
    TurnRef,
    TurnRequest,
)

from ..acp_client import AcpAgent, AgentOutputChunk, AgentToolCallUpdate
from ..session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputMetadata,
    AgentOutputStarted,
    AgentPlanEntry,
    AgentPlanUpdated,
    OutputOutcome,
    SessionEvent,
    SessionEventBody,
    SessionInputAccepted,
    SessionState,
    ToolCallObserved,
)

logger = logging.getLogger(__name__)
EventSink = Callable[[SessionEvent], Awaitable[None]]
StartFailedHandler = Callable[[Exception], Awaitable[None]]
StartedHandler = Callable[[], Awaitable[bool]]
TurnPreparer = Callable[[TurnRequest], Awaitable[None]]
TurnContext = Callable[[], AbstractAsyncContextManager[None]]
IdleHandler = Callable[[], Awaitable[None]]
TerminateHandler = Callable[[Literal["stopped", "done"]], Awaitable[None]]
FinishedHandler = Callable[[], Awaitable[None]]
TurnFinishedHandler = Callable[[TurnRequest], Awaitable[None]]


@dataclass(frozen=True)
class AcpTurnResult:
    """daemon 执行一轮 ACP Turn 后交还 Runtime 的机械结果。"""

    outcome: OutputOutcome | None
    keep_running: bool
    usage_tokens: int | None = None


@dataclass(frozen=True)
class AcpPromptResult:
    """一次 ACP prompt 的完整机械结果。"""

    stop_reason: str
    message: str
    usage_tokens: int | None


@dataclass
class _BackgroundTurnBatch:
    """可在队尾继续合并的后台结果 Turn。"""

    blocks: list[str]
    guidance: str

    def add(self, block: str) -> None:
        self.blocks.append(block)

    def to_request(self, conversation: ConversationRef) -> TurnRequest:
        return TurnRequest(
            "\n\n".join((*self.blocks, self.guidance)),
            conversation,
        )


BackgroundTurnPlacement = Literal["queued", "merged"]
QueueItem = TurnRequest | _BackgroundTurnBatch | None


@dataclass(frozen=True)
class AcpSessionRuntimeHooks:
    """Runtime 机械状态机调用的应用层生命周期端口。"""

    is_current: Callable[[], bool]
    on_start_failed: StartFailedHandler
    on_started: StartedHandler
    turn_context: TurnContext
    prepare_turn: TurnPreparer
    execute_turn: Callable[[TurnRequest], Awaitable[AcpTurnResult]]
    on_turn_finished: TurnFinishedHandler
    on_idle_timeout: IdleHandler
    on_terminate: TerminateHandler
    on_finished: FinishedHandler


@dataclass(eq=False)
class AcpSessionRuntime(SessionRuntime):
    """一个 ACP agent 进程及其单消费者 Turn 队列。

    daemon 仍负责 SessionStore、Channel 投影、用户通知与后台任务策略；
    本类持有一代 ACP 运行实例的可变状态，并提供统一 Runtime 生命周期接口。
    """

    project_name: str
    agent_label: str
    conversation: ConversationRef
    session_id: str = ""
    cwd: str = ""
    resumed: bool = False
    attached: bool = False
    issue_url: str = ""
    agent: InitVar[AcpAgent | None] = None
    event_sink: InitVar[EventSink | None] = None
    _agent: AcpAgent | None = field(default=None, init=False)
    _queue: asyncio.Queue[QueueItem] = field(default_factory=asyncio.Queue)
    _pending_background_batch: _BackgroundTurnBatch | None = None
    _terminate_status: Literal["stopped", "done"] = "stopped"
    _worker: asyncio.Task[None] | None = None
    _current_turn_id: str | None = None
    _message_chunks: list[str] = field(default_factory=list)
    _thought_chunks: list[str] = field(default_factory=list)
    _state: SessionState = "starting"
    _closed: bool = False
    _idle: asyncio.Event = field(default_factory=asyncio.Event)
    _listeners: list[SessionEventListener] = field(default_factory=list)
    _event_sink: EventSink | None = field(default=None, init=False)

    def __post_init__(
        self,
        agent: AcpAgent | None,
        event_sink: EventSink | None,
    ) -> None:
        self._agent = agent
        self._event_sink = event_sink
        self._idle.set()

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def agent_session_id(self) -> str | None:
        return self._agent.session_id if self._agent is not None else None

    @property
    def model(self) -> str:
        return self._agent.model if self._agent is not None else ""

    @property
    def available_models(self) -> list[str]:
        return list(self._agent.available_models) if self._agent is not None else []

    def subscribe(self, listener: SessionEventListener) -> Callable[[], None]:
        """订阅 Runtime 事件；daemon 通过 event_sink 接入持久化与投影。"""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return unsubscribe

    def set_event_sink(self, event_sink: EventSink | None) -> None:
        """设置宿主事件出口；应在启动 worker 前调用。"""
        if self._worker is not None and not self._worker.done():
            raise RuntimeError("Runtime worker 已启动，不能更换 event sink")
        self._event_sink = event_sink

    def submit(self, request: TurnRequest) -> TurnReceipt:
        """接受一个 Turn，并复用 ACP worker 的 FIFO 队列。"""
        if self._closed:
            raise RuntimeError(f"Session {self.session_id} 已关闭")
        if not request.text:
            raise ValueError("TurnRequest.text 不能为空")
        placement = (
            "current"
            if self._current_turn_id is None and self._queue.empty()
            else "pending"
        )
        self.enqueue(request)
        return TurnReceipt(
            turn=TurnRef(self.session_id, request.turn_id),
            placement=placement,
        )

    def try_submit(self, request: TurnRequest) -> TurnReceipt | None:
        """仅在当前 worker 仍可消费 Turn 时入队。"""
        if not self._accepts_turns():
            return None
        return self.submit(request)

    def owns_turn(self, turn_id: str) -> bool:
        """当前正在执行的 Turn 是否为 ``turn_id``。"""
        return self._current_turn_id == turn_id

    def has_pending_turns(self) -> bool:
        """当前 Turn 之后是否已有排队输入。"""
        return not self._queue.empty()

    def submit_background_turn(
        self,
        block: str,
        *,
        guidance: str,
    ) -> BackgroundTurnPlacement | None:
        """提交后台结果；相邻结果合并进同一 Turn，不可接单时返回 ``None``。"""
        if not self._accepts_turns():
            return None
        pending = self._pending_background_batch
        if pending is not None and pending.guidance == guidance:
            pending.add(block)
            return "merged"
        batch = _BackgroundTurnBatch([block], guidance)
        self._pending_background_batch = batch
        self._idle.clear()
        self._queue.put_nowait(batch)
        return "queued"

    def start_worker(
        self,
        hooks: AcpSessionRuntimeHooks,
        *,
        idle_timeout: float | None,
    ) -> asyncio.Task[None]:
        """创建并持有本代 Runtime 的唯一 worker task。"""
        if self._worker is not None and not self._worker.done():
            raise RuntimeError(f"Session {self.session_id} worker 已启动")
        self._worker = asyncio.create_task(
            self._run(hooks, idle_timeout=idle_timeout),
            name=f"agent-{self.session_id}",
        )
        return self._worker

    async def _run(
        self,
        hooks: AcpSessionRuntimeHooks,
        *,
        idle_timeout: float | None,
    ) -> None:
        """启动 ACP agent，并以单消费者顺序驱动本代 Runtime。"""
        try:
            try:
                await self.start()
            except Exception as exc:
                await hooks.on_start_failed(exc)
                return
            if not hooks.is_current() or not await hooks.on_started():
                return
            if self._queue.empty():
                self._mark_state("idle")
            while hooks.is_current():
                try:
                    queued = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=idle_timeout,
                    )
                except asyncio.TimeoutError:
                    if hooks.is_current():
                        self._mark_state("suspended")
                        await hooks.on_idle_timeout()
                    return
                if not hooks.is_current():
                    return
                if queued is None:
                    self._mark_state(self._terminate_status)
                    await hooks.on_terminate(self._terminate_status)
                    return
                async with hooks.turn_context():
                    if not hooks.is_current():
                        return
                    request, mirror_input = self._resolve_queue_item(queued)
                    self._mark_state("running")
                    self._begin_turn(request)
                    output_started = False
                    output_finished = False

                    async def finish_output(
                        outcome: OutputOutcome,
                        *,
                        usage_tokens: int | None = None,
                    ) -> None:
                        nonlocal output_finished
                        if output_finished or not output_started:
                            return
                        # Set the guard before publishing: a failing event sink must
                        # not cause a second Finished event during exception cleanup.
                        output_finished = True
                        await self._finish_turn(
                            outcome,
                            usage_tokens=usage_tokens,
                        )

                    try:
                        if mirror_input:
                            await self._publish_body(
                                SessionInputAccepted(
                                    text=request.text,
                                    source=request.conversation,
                                )
                            )
                        await hooks.prepare_turn(request)
                        # _publish_body emits to local subscribers before awaiting
                        # the host sink. Marking first ensures an event that was
                        # emitted but whose sink failed still receives one cleanup
                        # Finished attempt.
                        output_started = True
                        await self._publish_body(
                            AgentOutputStarted(
                                metadata=AgentOutputMetadata(
                                    project_name=self.project_name,
                                    agent_label=self.agent_label,
                                    model=self.model,
                                    issue_url=self.issue_url,
                                )
                            )
                        )
                        result = await hooks.execute_turn(request)
                        if result.outcome is not None:
                            await finish_output(
                                result.outcome,
                                usage_tokens=result.usage_tokens,
                            )
                        elif not result.keep_running:
                            await finish_output("interrupted")
                        if not result.keep_running:
                            return
                    except asyncio.CancelledError:
                        try:
                            await finish_output("interrupted")
                        except asyncio.CancelledError:
                            logger.warning(
                                "ACP Runtime 收尾事件发布被取消 session=%s turn=%s",
                                self.session_id,
                                request.turn_id,
                            )
                        except Exception:
                            logger.exception(
                                "ACP Runtime 收尾事件发布失败 session=%s turn=%s",
                                self.session_id,
                                request.turn_id,
                            )
                        raise
                    except Exception:
                        try:
                            await finish_output("interrupted")
                        except Exception:
                            logger.exception(
                                "ACP Runtime 收尾事件发布失败 session=%s turn=%s",
                                self.session_id,
                                request.turn_id,
                            )
                        raise
                    finally:
                        try:
                            await hooks.on_turn_finished(request)
                        finally:
                            self._reset_turn()
                if self._queue.empty():
                    self._mark_state("idle")
        except asyncio.CancelledError:
            logger.debug("ACP Runtime worker 被取消 session=%s", self.session_id)
        finally:
            await hooks.on_finished()

    async def cancel(self) -> None:
        """请求 ACP 取消当前 Turn；未启动或已关闭时幂等。"""
        if self._closed or self._agent is None or self._current_turn_id is None:
            return
        await self._agent.cancel()

    async def cancel_current_turn(
        self,
        replacement: TurnRequest | None = None,
    ) -> bool:
        """取消当前 Turn；可在取消前原子排入替代 Turn。

        返回是否确有在途 Turn 被请求取消。无在途 Turn 时不排入替代 Turn。
        """
        if self._current_turn_id is None:
            return False
        if replacement is not None:
            self.submit(replacement)
        await self.cancel()
        return True

    async def start(self) -> None:
        """启动底层 ACP agent；每个 Runtime 只能启动一次。"""
        if self._closed:
            raise RuntimeError(f"Session {self.session_id} 已关闭")
        if self._agent is None:
            raise RuntimeError(f"Session {self.session_id} 缺少 ACP agent")
        await self._agent.start()

    async def prompt(self, text: str) -> AcpPromptResult:
        """执行一轮 ACP prompt，并返回其完整机械结果。"""
        if self._agent is None:
            raise RuntimeError(f"Session {self.session_id} 缺少 ACP agent")
        stop_reason = await self._agent.prompt(text)
        return AcpPromptResult(
            stop_reason=stop_reason,
            message=getattr(self._agent, "last_message", ""),
            usage_tokens=getattr(self._agent, "last_usage_tokens", None),
        )

    async def handle_output(self, output: AgentOutputChunk) -> None:
        """把 ACP 输出回调转换成当前 Turn 的 SessionEvent。"""
        if self._current_turn_id is None:
            return
        if output.raw_text is None:
            if not output.plan_entries:
                return
            await self._publish_body(
                AgentPlanUpdated(
                    entries=tuple(
                        AgentPlanEntry(
                            content=entry.content,
                            status=entry.status,
                        )
                        for entry in output.plan_entries
                    )
                )
            )
            return
        if output.kind == "message":
            self._message_chunks.append(output.raw_text)
            stream = "message"
        elif output.kind == "thought":
            self._thought_chunks.append(output.raw_text)
            stream = "thought"
        else:
            return
        await self._publish_body(AgentOutputDelta(stream=stream, text=output.raw_text))

    async def handle_tool_call(self, update: AgentToolCallUpdate) -> None:
        """把 ACP 工具调用回调转换成当前 Turn 的 SessionEvent。"""
        if self._current_turn_id is None:
            return
        await self._publish_body(
            ToolCallObserved(
                tool_call_id=update.tool_call_id,
                kind=update.kind,
                title=update.title,
                status=update.status,
                detail=update.detail,
            )
        )

    async def set_model(self, name: str) -> None:
        """切换 ACP 会话模型。"""
        if self._agent is None:
            raise RuntimeError(f"Session {self.session_id} 缺少 ACP agent")
        await self._agent.set_model(name)

    async def wait_idle(self) -> None:
        """等待当前 worker 空闲。"""
        await self._idle.wait()

    async def close(self) -> None:
        """关闭本代 Runtime 及其 ACP 资源，重复调用幂等。"""
        if self._closed:
            return
        self._pending_background_batch = None
        while not self._queue.empty():
            self._queue.get_nowait()
        agent = self._agent
        if agent is not None and self._current_turn_id is not None:
            try:
                await agent.cancel()
            except Exception:
                logger.exception("取消当前轮失败 session=%s", self.session_id)
        self._closed = True
        worker = self._worker
        if (
            worker is not None
            and not worker.done()
            and worker is not asyncio.current_task()
        ):
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._agent = None
        if agent is not None:
            try:
                await agent.aclose()
            except Exception:
                logger.debug("agent aclose 异常（忽略）", exc_info=True)
        self._mark_state("stopped")
        self._idle.set()

    def request_worker_stop(self) -> None:
        """同步请求停止 worker，避免宿主在等待前留下继续执行窗口。"""
        worker = self._worker
        if worker is None or worker.done() or worker is asyncio.current_task():
            return
        worker.cancel()

    async def wait_worker_stopped(self) -> None:
        """等待先前请求停止的 worker 完成收尾。"""
        worker = self._worker
        if worker is None or worker is asyncio.current_task():
            return
        try:
            await worker
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("agent worker 退出异常 session=%s", self.session_id)

    def enqueue(self, request: TurnRequest) -> None:
        """入队普通 Turn，并断开后台批次合并。"""
        self._pending_background_batch = None
        self._idle.clear()
        self._queue.put_nowait(request)

    def request_termination(
        self,
        *,
        status: Literal["stopped", "done"] = "stopped",
    ) -> bool:
        """请求 worker 以指定状态终止；不可接单时返回 ``False``。"""
        if not self._accepts_turns():
            return False
        self._terminate_status = status
        kept: list[QueueItem] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, TurnRequest) or item is None:
                kept.append(item)
        for item in kept:
            self._queue.put_nowait(item)
        self._pending_background_batch = None
        self._queue.put_nowait(None)
        return True

    def _accepts_turns(self) -> bool:
        """当前 worker 是否仍可消费新 Turn 或终止请求。"""
        return not self._closed and self._worker is not None and not self._worker.done()

    def _resolve_queue_item(self, queued: QueueItem) -> tuple[TurnRequest, bool]:
        if isinstance(queued, _BackgroundTurnBatch):
            if self._pending_background_batch is queued:
                self._pending_background_batch = None
            return queued.to_request(self.conversation), False
        if not isinstance(queued, TurnRequest):
            raise TypeError(f"不支持的 ACP 队列项: {type(queued).__name__}")
        return queued, True

    def _mark_state(self, state: SessionState) -> None:
        """更新 Runtime 机械状态，并同步 wait_idle 边界。"""
        self._state = state
        if state in {"idle", "suspended", "done", "stopped", "failed"}:
            self._idle.set()
        else:
            self._idle.clear()

    def _begin_turn(self, request: TurnRequest) -> None:
        self._current_turn_id = request.turn_id
        self._message_chunks.clear()
        self._thought_chunks.clear()

    async def _finish_turn(
        self,
        outcome: OutputOutcome,
        *,
        usage_tokens: int | None = None,
    ) -> None:
        await self._publish_body(
            AgentOutputFinished(
                message="".join(self._message_chunks),
                thought="".join(self._thought_chunks),
                outcome=outcome,
                usage_tokens=usage_tokens,
            )
        )

    def _reset_turn(self) -> None:
        self._current_turn_id = None
        self._message_chunks.clear()
        self._thought_chunks.clear()

    async def _publish_body(self, body: SessionEventBody) -> None:
        await self.publish(
            SessionEvent(
                event_id=secrets.token_hex(16),
                session_id=self.session_id,
                turn_id=self._current_turn_id,
                occurred_at=datetime.now(timezone.utc),
                body=body,
            )
        )

    def emit(self, event: SessionEvent) -> None:
        """向订阅者发布 Runtime 事件，并隔离单个订阅者失败。"""
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:
                # 与 ToolLoopSessionRuntime 保持一致：单个订阅者失败不影响其它订阅者。
                logger.exception(
                    "ACP SessionEvent 订阅者处理失败 event=%s",
                    event.event_id,
                )

    async def publish(self, event: SessionEvent) -> None:
        """发布事件并等待宿主完成持久化与投影。"""
        self.emit(event)
        if self._event_sink is not None:
            await self._event_sink(event)
