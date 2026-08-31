"""ToolLoopSessionRuntime 的通用行为测试。"""

from __future__ import annotations

import pytest

from feishu_dispatcher.scheduler import LLMResponse, SchedulerMemory
from feishu_dispatcher.session import ToolLoopSessionRuntime, TurnRequest
from feishu_dispatcher.session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
)
from tests.conversation_fakes import ConversationRefFactory as ConversationRef


class FakeLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, messages, tools) -> LLMResponse:
        self.calls.append(list(messages))
        return LLMResponse(content=self.replies.pop(0))


@pytest.mark.asyncio
async def test_tool_loop_runtime_uses_injected_session_policy() -> None:
    llm = FakeLLM(["manager reply"])
    runtime = ToolLoopSessionRuntime(
        session_id="manager:demo",
        llm_provider=lambda: llm,
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
        system_prompt="You manage one project.",
        llm_unavailable_message="manager LLM unavailable",
        error_reply_factory=lambda exc: f"manager error: {exc}",
        empty_reply="manager empty",
        max_iterations_reply="manager iteration limit",
        log_context="Manager",
    )
    events = []
    runtime.subscribe(events.append)

    receipt = runtime.submit(
        TurnRequest("inspect the project", ConversationRef("test", "conversation"))
    )
    await runtime.wait_idle()

    assert llm.calls[0][0] == {
        "role": "system",
        "content": "You manage one project.",
    }
    output_events = [
        event
        for event in events
        if isinstance(event.body, (AgentOutputDelta, AgentOutputFinished))
    ]
    assert output_events[0].body == AgentOutputDelta(
        stream="message",
        text="manager reply",
    )
    assert output_events[1].body == AgentOutputFinished(
        message="manager reply",
        thought="",
        outcome="completed",
    )
    assert output_events[0].turn_id == receipt.turn.turn_id
    await runtime.close()
