"""AcpSessionRuntime 的生命周期与 Registry 契约测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import cast

import pytest

from feishu_dispatcher.acp_client import (
    AcpAgent,
    AgentOutputChunk,
    AgentPlanEntryUpdate,
    AgentToolCallUpdate,
)
from feishu_dispatcher.session import (
    AcpSessionRuntime,
    AcpSessionRuntimeHooks,
    AcpTurnResult,
    SessionRuntime,
    TurnRequest,
)
from feishu_dispatcher.session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputMetadata,
    AgentOutputStarted,
    AgentPlanUpdated,
    SessionEvent,
    SessionInputAccepted,
    SessionStateChanged,
    ToolCallObserved,
)
from tests.conversation_fakes import ConversationRefFactory as ConversationRef


class FakeAgent:
    def __init__(self) -> None:
        self.session_id = "acp-1"
        self.model = "model-a"
        self.available_models = ["model-a", "model-b"]
        self.started = False
        self.cancel_calls = 0
        self.closed = False
        self.set_model_calls: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def prompt(self, _text: str) -> str:
        return "end_turn"

    async def cancel(self) -> None:
        self.cancel_calls += 1

    async def set_model(self, name: str) -> None:
        self.set_model_calls.append(name)
        self.model = name

    async def aclose(self) -> None:
        self.closed = True


def runtime(agent: FakeAgent | None = None) -> AcpSessionRuntime:
    return AcpSessionRuntime(
        "demo",
        "copilot",
        ConversationRef("feishu", "thread"),
        session_id="t1",
        agent=cast(AcpAgent, agent or FakeAgent()),
    )


def request(text: str) -> TurnRequest:
    return TurnRequest(text, ConversationRef("feishu", "thread"))


def test_acp_runtime_implements_session_runtime() -> None:
    assert isinstance(runtime(), SessionRuntime)


def test_acp_runtime_rejects_commands_without_running_worker() -> None:
    session = runtime()
    turn = request("hello")

    assert session.try_submit(turn) is None
    assert not session.request_termination()
    assert not session.owns_turn(turn.turn_id)


def test_acp_runtime_does_not_own_channel_projection_state() -> None:
    fields = AcpSessionRuntime.__dataclass_fields__

    assert {
        "current_output",
        "current_conversations",
        "current_message_chunks",
        "current_thought_chunks",
        "session_event_projection_tail",
    }.isdisjoint(fields)


@pytest.mark.asyncio
async def test_acp_runtime_delegates_start_and_exposes_agent_metadata() -> None:
    agent = FakeAgent()
    session = runtime(agent)

    await session.start()
    await session.set_model("model-b")

    assert agent.started
    assert session.agent_session_id == "acp-1"
    assert session.model == "model-b"
    assert session.available_models == ["model-a", "model-b"]
    assert agent.set_model_calls == ["model-b"]
    await session.close()


@pytest.mark.asyncio
async def test_acp_runtime_submit_preserves_fifo_placement() -> None:
    session = runtime()

    first = session.submit(request("first"))
    second = session.submit(request("second"))

    assert first.placement == "current"
    assert second.placement == "pending"
    assert session._queue.qsize() == 2
    await session.close()


@pytest.mark.asyncio
async def test_acp_runtime_wait_idle_tracks_worker_state() -> None:
    session = runtime()
    session.submit(request("first"))

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(session.wait_idle(), 0.01)

    await session.close()
    await session.wait_idle()


@pytest.mark.asyncio
async def test_acp_runtime_owns_worker_task() -> None:
    session = runtime()
    started = asyncio.Event()

    async def on_started() -> bool:
        started.set()
        return True

    async def execute_turn(_request: TurnRequest) -> AcpTurnResult:
        return AcpTurnResult(outcome=None, keep_running=True)

    async def noop() -> None:
        return None

    @asynccontextmanager
    async def turn_context():
        yield

    hooks = AcpSessionRuntimeHooks(
        is_current=lambda: True,
        on_start_failed=lambda _exc: noop(),
        on_started=on_started,
        turn_context=turn_context,
        prepare_turn=lambda _request: noop(),
        execute_turn=execute_turn,
        on_turn_finished=lambda _request: noop(),
        on_idle_timeout=noop,
        on_terminate=lambda _status: noop(),
        on_finished=noop,
    )
    task = session.start_worker(hooks, idle_timeout=None)
    await started.wait()
    with pytest.raises(RuntimeError, match="worker 已启动"):
        session.start_worker(hooks, idle_timeout=None)
    assert session.request_termination()
    await task
    await session.close()


@pytest.mark.asyncio
async def test_acp_runtime_worker_drives_turns_and_idle_state() -> None:
    session = runtime()
    executed: list[tuple[str, bool]] = []
    finished = asyncio.Event()

    async def noop() -> None:
        return None

    @asynccontextmanager
    async def turn_context():
        yield

    async def execute(turn: TurnRequest) -> AcpTurnResult:
        executed.append((turn.text, True))
        return AcpTurnResult(outcome=None, keep_running=True)

    async def _started() -> bool:
        return True

    hooks = AcpSessionRuntimeHooks(
        is_current=lambda: not finished.is_set(),
        on_start_failed=lambda _exc: noop(),
        on_started=lambda: _started(),
        turn_context=turn_context,
        prepare_turn=lambda _request: noop(),
        execute_turn=execute,
        on_turn_finished=lambda _request: noop(),
        on_idle_timeout=noop,
        on_terminate=lambda _status: noop(),
        on_finished=lambda: _finished(finished),
    )

    session.submit(request("one"))
    task = session.start_worker(hooks, idle_timeout=None)
    await asyncio.sleep(0)
    await session.wait_idle()
    assert executed == [("one", True)]
    assert session.state == "idle"

    assert session.request_termination()
    await task
    assert finished.is_set()


async def _finished(event: asyncio.Event) -> None:
    event.set()


@pytest.mark.asyncio
async def test_acp_runtime_produces_turn_events_from_acp_callbacks() -> None:
    session = runtime()
    events: list[SessionEvent] = []

    async def noop() -> None:
        return None

    @asynccontextmanager
    async def turn_context():
        yield

    async def execute(_turn: TurnRequest) -> AcpTurnResult:
        await session.handle_output(
            AgentOutputChunk(
                raw_text="thinking",
                kind="thought",
                display_text="💭 thinking",
            )
        )
        await session.handle_output(
            AgentOutputChunk(
                raw_text=None,
                kind="activity",
                display_text="plan",
                plan_entries=(
                    AgentPlanEntryUpdate(
                        content="inspect",
                        status="in_progress",
                    ),
                ),
            )
        )
        await session.handle_output(
            AgentOutputChunk(
                raw_text="hello",
                kind="message",
                display_text="hello",
            )
        )
        await session.handle_tool_call(
            AgentToolCallUpdate(
                tool_call_id="tool-1",
                kind="execute",
                title="pytest",
                status="started",
                detail=None,
            )
        )
        return AcpTurnResult(
            outcome="completed",
            keep_running=True,
            usage_tokens=321,
        )

    async def started() -> bool:
        return True

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    session.set_event_sink(collect)
    hooks = AcpSessionRuntimeHooks(
        is_current=lambda: True,
        on_start_failed=lambda _exc: noop(),
        on_started=started,
        turn_context=turn_context,
        prepare_turn=lambda _request: noop(),
        execute_turn=execute,
        on_turn_finished=lambda _request: noop(),
        on_idle_timeout=noop,
        on_terminate=lambda _status: noop(),
        on_finished=noop,
    )

    session.submit(request("hello"))
    task = session.start_worker(hooks, idle_timeout=None)
    await session.wait_idle()
    await session.handle_output(
        AgentOutputChunk(
            raw_text="late",
            kind="message",
            display_text="late",
        )
    )
    assert session.request_termination()
    await task

    assert [type(event.body) for event in events] == [
        SessionInputAccepted,
        AgentOutputStarted,
        AgentOutputDelta,
        AgentPlanUpdated,
        AgentOutputDelta,
        ToolCallObserved,
        AgentOutputFinished,
    ]
    assert cast(AgentOutputStarted, events[1].body).metadata == AgentOutputMetadata(
        project_name="demo",
        agent_label="copilot",
        model="model-a",
    )
    assert cast(AgentOutputDelta, events[2].body).text == "thinking"
    assert cast(AgentOutputDelta, events[4].body).text == "hello"
    finished = cast(AgentOutputFinished, events[6].body)
    assert finished.message == "hello"
    assert finished.thought == "thinking"
    assert finished.outcome == "completed"
    assert finished.usage_tokens == 321


@pytest.mark.asyncio
async def test_acp_runtime_finishes_abandoned_turn_as_interrupted() -> None:
    session = runtime()
    events: list[SessionEvent] = []

    async def noop() -> None:
        return None

    @asynccontextmanager
    async def turn_context():
        yield

    session.set_event_sink(lambda event: _collect_event(events, event))
    hooks = AcpSessionRuntimeHooks(
        is_current=lambda: True,
        on_start_failed=lambda _exc: noop(),
        on_started=lambda: _true(),
        turn_context=turn_context,
        prepare_turn=lambda _request: noop(),
        execute_turn=lambda _request: _turn_result(
            AcpTurnResult(outcome=None, keep_running=False)
        ),
        on_turn_finished=lambda _request: noop(),
        on_idle_timeout=noop,
        on_terminate=lambda _status: noop(),
        on_finished=noop,
    )

    session.submit(request("hello"))
    await session.start_worker(hooks, idle_timeout=None)

    finished = [
        event.body for event in events if isinstance(event.body, AgentOutputFinished)
    ]
    assert finished == [
        AgentOutputFinished(
            message="",
            thought="",
            outcome="interrupted",
        )
    ]


@pytest.mark.asyncio
async def test_acp_runtime_cancel_after_started_emits_one_interrupted_finish() -> None:
    session = runtime()
    events: list[SessionEvent] = []
    executing = asyncio.Event()

    async def noop() -> None:
        return None

    async def execute(_request: TurnRequest) -> AcpTurnResult:
        executing.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def turn_context():
        yield

    session.set_event_sink(lambda event: _collect_event(events, event))
    hooks = AcpSessionRuntimeHooks(
        is_current=lambda: True,
        on_start_failed=lambda _exc: noop(),
        on_started=lambda: _true(),
        turn_context=turn_context,
        prepare_turn=lambda _request: noop(),
        execute_turn=execute,
        on_turn_finished=lambda _request: noop(),
        on_idle_timeout=noop,
        on_terminate=lambda _status: noop(),
        on_finished=noop,
    )

    session.submit(request("hello"))
    task = session.start_worker(hooks, idle_timeout=None)
    await executing.wait()
    task.cancel()
    await task

    assert (
        len([event for event in events if isinstance(event.body, AgentOutputStarted)])
        == 1
    )
    assert [
        event.body for event in events if isinstance(event.body, AgentOutputFinished)
    ] == [
        AgentOutputFinished(
            message="",
            thought="",
            outcome="interrupted",
        )
    ]


@pytest.mark.asyncio
async def test_acp_runtime_cancel_before_started_emits_no_output_finish() -> None:
    class BlockingStartAgent(FakeAgent):
        def __init__(self) -> None:
            super().__init__()
            self.starting = asyncio.Event()

        async def start(self) -> None:
            self.starting.set()
            await asyncio.Event().wait()

    agent = BlockingStartAgent()
    session = runtime(agent)
    events: list[SessionEvent] = []

    async def noop() -> None:
        return None

    @asynccontextmanager
    async def turn_context():
        yield

    session.set_event_sink(lambda event: _collect_event(events, event))
    hooks = AcpSessionRuntimeHooks(
        is_current=lambda: True,
        on_start_failed=lambda _exc: noop(),
        on_started=lambda: _true(),
        turn_context=turn_context,
        prepare_turn=lambda _request: noop(),
        execute_turn=lambda _request: _turn_result(
            AcpTurnResult(outcome="completed", keep_running=True)
        ),
        on_turn_finished=lambda _request: noop(),
        on_idle_timeout=noop,
        on_terminate=lambda _status: noop(),
        on_finished=noop,
    )

    session.submit(request("hello"))
    task = session.start_worker(hooks, idle_timeout=None)
    await agent.starting.wait()
    task.cancel()
    await task

    assert not any(
        isinstance(event.body, (AgentOutputStarted, AgentOutputFinished))
        for event in events
    )


async def _collect_event(events: list[SessionEvent], event: SessionEvent) -> None:
    events.append(event)


async def _true() -> bool:
    return True


async def _turn_result(result: AcpTurnResult) -> AcpTurnResult:
    return result


@pytest.mark.asyncio
async def test_acp_runtime_cancel_is_cooperative_and_close_is_idempotent() -> None:
    agent = FakeAgent()
    session = runtime(agent)
    session._current_turn_id = "turn-1"

    await session.cancel()
    await session.close()
    await session.close()

    assert agent.cancel_calls == 2
    assert agent.closed
    assert session.state == "stopped"


@pytest.mark.asyncio
async def test_acp_runtime_cancel_current_turn_queues_replacement_first() -> None:
    agent = FakeAgent()
    session = runtime(agent)
    replacement = request("replacement")
    session._current_turn_id = "turn-1"

    assert session.owns_turn("turn-1")
    assert await session.cancel_current_turn(replacement)
    assert agent.cancel_calls == 1
    assert session._queue.get_nowait() is replacement


def test_acp_runtime_subscribers_are_isolated() -> None:
    session = runtime()
    received: list[SessionEvent] = []
    session.subscribe(lambda _event: (_ for _ in ()).throw(RuntimeError("boom")))
    session.subscribe(received.append)
    event = SessionEvent(
        event_id="event-1",
        session_id="t1",
        turn_id=None,
        occurred_at=datetime.now(timezone.utc),
        body=SessionStateChanged(previous_state="starting", current_state="idle"),
    )

    session.emit(event)

    assert received == [event]


@pytest.mark.asyncio
async def test_acp_runtime_publish_notifies_listener_and_awaits_sink() -> None:
    published: list[SessionEvent] = []

    async def sink(event: SessionEvent) -> None:
        published.append(event)

    session = runtime()
    session.set_event_sink(sink)
    received: list[SessionEvent] = []
    session.subscribe(received.append)
    event = SessionEvent(
        event_id="event-1",
        session_id="t1",
        turn_id=None,
        occurred_at=datetime.now(timezone.utc),
        body=SessionStateChanged(previous_state="starting", current_state="idle"),
    )

    await session.publish(event)

    assert received == [event]
    assert published == [event]
