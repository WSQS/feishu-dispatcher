"""ProjectManagerSessionRuntime 与最小工具集测试。"""

from __future__ import annotations

import json

import pytest

from feishu_dispatcher.scheduler import LLMResponse, SchedulerMemory, ToolCall
from feishu_dispatcher.session import (
    ProjectManagerSessionRuntime,
    TurnRequest,
    build_project_manager_tools,
)
from feishu_dispatcher.session_event import AgentOutputDelta
from tests.conversation_fakes import ConversationRefFactory as ConversationRef


class FakeLLM:
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[tuple[list[dict], list[dict]]] = []

    async def chat(self, messages, tools) -> LLMResponse:
        self.calls.append((list(messages), tools))
        return self.script.pop(0)


def tools(
    *,
    sessions: list[dict] | None = None,
    lookup: dict[str, dict] | None = None,
    sent: list[tuple[str, str]] | None = None,
):
    sent = sent if sent is not None else []
    lookup = lookup if lookup is not None else {}
    return build_project_manager_tools(
        project_name="demo",
        list_sessions=lambda: sessions or [],
        get_session=lambda session_id: lookup.get(session_id),
        send_to_session=lambda session_id, message: _send(sent, session_id, message),
    )


async def _send(sent: list[tuple[str, str]], session_id: str, message: str) -> str:
    sent.append((session_id, message))
    return f"已转达 {session_id}"


def tool(tool_list, name: str):
    return next(item for item in tool_list if item.name == name)


async def test_project_manager_tools_list_lookup_and_send():
    sent: list[tuple[str, str]] = []
    tool_list = tools(
        sessions=[{"session_id": "s1", "project": "demo", "status": "running"}],
        lookup={
            "s1": {
                "session_id": "s1",
                "project": "demo",
                "description": "build",
            }
        },
        sent=sent,
    )

    assert json.loads(await tool(tool_list, "list_sessions").handler({})) == [
        {"session_id": "s1", "project": "demo", "status": "running"}
    ]
    assert json.loads(
        await tool(tool_list, "get_session").handler({"session_id": "s1"})
    ) == {"session_id": "s1", "project": "demo", "description": "build"}
    assert (
        await tool(tool_list, "send_to_session").handler(
            {"session_id": "s1", "message": "继续"}
        )
        == "已转达 s1"
    )
    assert sent == [("s1", "继续")]


async def test_project_manager_tools_validate_and_scope_missing_session():
    tool_list = tools()

    assert "参数不足" in await tool(tool_list, "get_session").handler({})
    assert "参数不足" in await tool(tool_list, "send_to_session").handler(
        {"session_id": "s1"}
    )
    assert "未找到项目 demo" in await tool(tool_list, "get_session").handler(
        {"session_id": "s1"}
    )


async def test_project_manager_tools_reject_other_projects():
    sent: list[tuple[str, str]] = []
    tool_list = tools(
        sessions=[
            {"session_id": "s1", "project": "demo"},
            {"session_id": "s2", "project": "other"},
        ],
        lookup={"s2": {"session_id": "s2", "project": "other"}},
        sent=sent,
    )

    assert json.loads(await tool(tool_list, "list_sessions").handler({})) == [
        {"session_id": "s1", "project": "demo"}
    ]
    assert "未找到项目 demo" in await tool(tool_list, "get_session").handler(
        {"session_id": "s2"}
    )
    assert "未找到项目 demo" in await tool(tool_list, "send_to_session").handler(
        {"session_id": "s2", "message": "继续"}
    )
    assert sent == []


@pytest.mark.asyncio
async def test_project_manager_runtime_uses_project_policy_and_tools():
    llm = FakeLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall("1", "list_sessions", {}),
                ]
            ),
            LLMResponse(content="项目当前有一个运行中的 Session。"),
        ]
    )
    runtime = ProjectManagerSessionRuntime(
        session_id="manager:demo",
        project_name="demo",
        llm_provider=lambda: llm,
        memory=SchedulerMemory(None),
        list_sessions=lambda: [
            {"session_id": "s1", "project": "demo", "status": "running"}
        ],
        get_session=lambda _session_id: None,
        send_to_session=lambda _session_id, _message: _send([], "", ""),
    )
    events = []
    runtime.subscribe(events.append)

    runtime.submit(
        TurnRequest(
            "查看项目状态",
            ConversationRef("test", "conversation"),
        )
    )
    await runtime.wait_idle()

    assert llm.calls[0][0][0]["content"].startswith(
        "你是项目 demo 的 Project Manager。"
    )
    assert {item["function"]["name"] for item in llm.calls[0][1]} == {
        "list_sessions",
        "get_session",
        "send_to_session",
    }
    assert any(
        isinstance(event.body, AgentOutputDelta)
        and event.body.text == "项目当前有一个运行中的 Session。"
        for event in events
    )
    await runtime.close()
