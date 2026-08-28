"""DispatcherSessionRuntime 的行为测试。"""

from __future__ import annotations

import asyncio

import pytest

from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.scheduler import LLMResponse, SchedulerMemory
from feishu_dispatcher.session import (
    DispatcherSessionRuntime,
    TurnRequest,
)


class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools) -> LLMResponse:
        self.calls.append(list(messages))
        return LLMResponse(content=self.replies.pop(0))


def request(text: str) -> TurnRequest:
    return TurnRequest(text, ConversationRef("test", "conversation"))


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

    receipt = runtime.submit(request("hello"))
    reply = await runtime.wait_turn(receipt.turn)

    assert reply == "reply"
    assert seen_conversations == [ConversationRef("test", "conversation")]
    assert runtime.state == "idle"
    assert runtime._memory.history() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "reply"},
    ]
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
    assert await runtime.wait_turn(first.turn) == "reply 1"
    assert await runtime.wait_turn(second.turn) == "reply 2"
    await runtime.wait_idle()
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

    receipt = runtime.submit(request("hello"))
    await started.wait()
    waiter = asyncio.create_task(runtime.wait_turn(receipt.turn))
    await runtime.close()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert runtime.state == "stopped"
