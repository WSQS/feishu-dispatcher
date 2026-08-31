"""DispatcherSessionRuntime 的行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from feishu_dispatcher.scheduler import LLMResponse, SchedulerMemory
from feishu_dispatcher.session import (
    DispatcherSessionRuntime,
    SessionRuntime,
    TurnRequest,
)
from feishu_dispatcher.session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputStarted,
    SessionErrorOccurred,
    SessionInputAccepted,
    SessionStateChanged,
)
from tests.conversation_fakes import ConversationRefFactory as ConversationRef


class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools) -> LLMResponse:
        self.calls.append(list(messages))
        return LLMResponse(content=self.replies.pop(0))


def request(text: str) -> TurnRequest:
    return TurnRequest(text, ConversationRef("test", "conversation"))


def test_dispatcher_runtime_implements_session_runtime() -> None:
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: None,
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )

    assert isinstance(runtime, SessionRuntime)


@pytest.mark.asyncio
async def test_runtime_executes_turn_and_updates_memory() -> None:
    llm = FakeLLM(["reply"])
    seen_conversations: list[ConversationRef] = []
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: llm,
        memory=SchedulerMemory(None),
        tools_provider=lambda conversation: (
            seen_conversations.append(conversation) or []
        ),
    )

    runtime.submit(request("hello"))
    await runtime.wait_idle()

    assert seen_conversations == [ConversationRef("test", "conversation")]
    assert runtime.state == "idle"
    assert runtime._memory.history() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "reply"},
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_does_not_cache_turn_results() -> None:
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: FakeLLM(["reply"]),
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )

    receipt = runtime.submit(request("hello"))
    await runtime.wait_idle()

    assert receipt.turn.session_id == runtime.session_id
    assert not hasattr(runtime, "_results")
    assert not hasattr(runtime, "wait_turn")
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_subscription_can_be_removed() -> None:
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: None,
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )

    def listener(_event) -> None:
        pass

    unsubscribe = runtime.subscribe(listener)
    unsubscribe()
    unsubscribe()

    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_notifies_multiple_subscribers_independently() -> None:
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: FakeLLM(["reply"]),
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )
    first_events = []
    second_events = []
    runtime.subscribe(first_events.append)
    runtime.subscribe(second_events.append)

    runtime.submit(request("hello"))
    await runtime.wait_idle()

    assert first_events == second_events
    assert len({event.event_id for event in first_events}) == len(first_events)
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_isolates_subscriber_failure() -> None:
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: FakeLLM(["reply"]),
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )
    received = []

    def broken_listener(_event) -> None:
        raise RuntimeError("listener boom")

    runtime.subscribe(broken_listener)
    runtime.subscribe(received.append)

    runtime.submit(request("hello"))

    await runtime.wait_idle()
    assert received
    assert all(event.session_id == "dispatcher" for event in received)
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_publishes_turn_lifecycle_events() -> None:
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: FakeLLM(["reply"]),
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )
    events = []
    runtime.subscribe(events.append)

    receipt = runtime.submit(request("hello"))
    await runtime.wait_idle()

    assert [type(event.body) for event in events] == [
        SessionInputAccepted,
        SessionStateChanged,
        AgentOutputStarted,
        AgentOutputDelta,
        AgentOutputFinished,
        SessionStateChanged,
    ]
    assert events[0].turn_id == receipt.turn.turn_id
    assert events[1].body == SessionStateChanged(
        previous_state="idle",
        current_state="running",
    )
    assert events[2].turn_id == receipt.turn.turn_id
    assert events[3].body == AgentOutputDelta(
        stream="message",
        text="reply",
    )
    assert events[4].body == AgentOutputFinished(
        message="reply",
        thought="",
        outcome="completed",
    )
    assert events[5].body == SessionStateChanged(
        previous_state="running",
        current_state="idle",
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_publishes_execution_failure_event() -> None:
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: None,
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )
    events = []
    runtime.subscribe(events.append)

    receipt = runtime.submit(request("hello"))
    await runtime.wait_idle()

    assert runtime._memory.history() == [
        {
            "role": "user",
            "content": "hello",
        },
        {
            "role": "assistant",
            "content": "调度器出错：调度器 LLM 未配置。可用 `/run <项目> <任务>` 直接派发。",
        },
    ]
    error = next(
        event for event in events if isinstance(event.body, SessionErrorOccurred)
    )
    assert error.turn_id == receipt.turn.turn_id
    assert error.body == SessionErrorOccurred(
        phase="execute_turn",
        message="调度器 LLM 未配置",
    )
    finished = next(
        event for event in events if isinstance(event.body, AgentOutputFinished)
    )
    assert finished.turn_id == receipt.turn.turn_id
    assert finished.body.outcome == "failed"
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_processes_pending_turns_in_order() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class BlockingLLM(FakeLLM):
        async def chat(self, messages, tools) -> LLMResponse:
            self.calls.append(list(messages))
            if len(self.calls) == 1:
                first_started.set()
                await release_first.wait()
            return LLMResponse(content=f"reply {len(self.calls)}")

    llm = BlockingLLM([])
    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: llm,
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )

    first = runtime.submit(request("first"))
    await first_started.wait()
    second = runtime.submit(request("second"))

    assert first.placement == "current"
    assert second.placement == "pending"
    release_first.set()
    await runtime.wait_idle()
    second_messages = [
        (message["role"], message.get("content"))
        for message in llm.calls[1]
        if message["role"] != "system"
    ]
    assert second_messages == [
        ("user", "first"),
        ("assistant", "reply 1"),
        ("user", "second"),
    ]
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_publishes_cancellation_event() -> None:
    started = asyncio.Event()

    class BlockingLLM(FakeLLM):
        async def chat(self, messages, tools) -> LLMResponse:
            self.calls.append(list(messages))
            started.set()
            await asyncio.Future()

    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: BlockingLLM([]),
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )
    events = []
    runtime.subscribe(events.append)

    receipt = runtime.submit(request("hello"))
    await started.wait()
    await runtime.cancel()
    await runtime.wait_idle()
    finished = next(
        event for event in events if isinstance(event.body, AgentOutputFinished)
    )
    assert finished.turn_id == receipt.turn.turn_id
    assert finished.body == AgentOutputFinished(
        message="",
        thought="",
        outcome="cancelled",
    )
    await runtime.close()


@pytest.mark.asyncio
async def test_runtime_close_cancels_current_turn() -> None:
    started = asyncio.Event()

    class BlockingLLM(FakeLLM):
        async def chat(self, messages, tools) -> LLMResponse:
            self.calls.append(list(messages))
            started.set()
            await asyncio.Future()

    runtime = DispatcherSessionRuntime(
        session_id="dispatcher",
        llm_provider=lambda: BlockingLLM([]),
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )

    runtime.submit(request("hello"))
    await started.wait()
    await runtime.close()

    assert runtime.state == "stopped"
