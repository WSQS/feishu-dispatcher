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
    delegations: list[dict] | None = None,
    delegation_lookup: dict[str, dict] | None = None,
    created: list[tuple[str, str, str, str]] | None = None,
):
    sent = sent if sent is not None else []
    lookup = lookup if lookup is not None else {}
    delegation_lookup = delegation_lookup if delegation_lookup is not None else {}
    created = created if created is not None else []
    return build_project_manager_tools(
        project_name="demo",
        conversation=ConversationRef("test", "manager"),
        list_sessions=lambda: sessions or [],
        get_session=lambda session_id: lookup.get(session_id),
        send_to_session=lambda session_id, message: _send(sent, session_id, message),
        create_session=lambda conversation, agent, description, initial_task: _create(
            created, conversation, agent, description, initial_task
        ),
        list_delegations=lambda: delegations or [],
        get_delegation=lambda delegation_id: delegation_lookup.get(delegation_id),
        delegate_to_session=lambda session_id, instruction: _send(
            sent, session_id, instruction
        ),
        continue_delegation=lambda delegation_id, message: _send(
            sent, delegation_id, message
        ),
        complete_delegation=lambda delegation_id: _complete(delegation_id),
    )


async def _send(sent: list[tuple[str, str]], session_id: str, message: str) -> str:
    sent.append((session_id, message))
    return f"已转达 {session_id}"


async def _complete(delegation_id: str) -> str:
    return f"已完成 {delegation_id}"


async def _create(
    created: list[tuple[str, str, str, str]],
    conversation: ConversationRef,
    agent: str,
    description: str,
    initial_task: str,
) -> dict:
    created.append((conversation.channel_key(), agent, description, initial_task))
    return {
        "session_id": "s-created",
        "agent": agent or "copilot",
        "status": "starting",
        "description": description,
    }


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


async def test_project_manager_delegation_tools_create_inspect_continue_and_complete():
    sent: list[tuple[str, str]] = []
    tool_list = tools(
        sessions=[{"session_id": "s1", "project": "demo"}],
        lookup={"s1": {"session_id": "s1", "project": "demo"}},
        delegations=[
            {
                "delegation_id": "d1",
                "project": "demo",
                "status": "waiting_manager",
            }
        ],
        delegation_lookup={
            "d1": {
                "delegation_id": "d1",
                "project": "demo",
                "status": "waiting_manager",
            }
        },
        sent=sent,
    )

    assert json.loads(await tool(tool_list, "list_delegations").handler({})) == [
        {
            "delegation_id": "d1",
            "project": "demo",
            "status": "waiting_manager",
        }
    ]
    assert (
        json.loads(
            await tool(tool_list, "get_delegation").handler({"delegation_id": "d1"})
        )["status"]
        == "waiting_manager"
    )
    assert (
        await tool(tool_list, "delegate_to_session").handler(
            {"session_id": "s1", "instruction": "修复测试"}
        )
        == "已转达 s1"
    )
    assert (
        await tool(tool_list, "continue_delegation").handler(
            {"delegation_id": "d1", "message": "继续检查"}
        )
        == "已转达 d1"
    )
    assert (
        await tool(tool_list, "complete_delegation").handler({"delegation_id": "d1"})
        == "已完成 d1"
    )
    assert sent == [("s1", "修复测试"), ("d1", "继续检查")]


async def test_project_manager_tools_validate_and_scope_missing_session():
    tool_list = tools()

    assert "参数不足" in await tool(tool_list, "get_session").handler({})
    assert "参数不足" in await tool(tool_list, "send_to_session").handler(
        {"session_id": "s1"}
    )
    assert "未找到项目 demo" in await tool(tool_list, "get_session").handler(
        {"session_id": "s1"}
    )


async def test_project_manager_create_session_returns_tracking_info():
    created: list[tuple[str, str, str, str]] = []
    tool_list = tools(created=created)

    result = json.loads(
        await tool(tool_list, "create_session").handler(
            {
                "agent": "opencode",
                "description": "新 Worker",
                "initial_task": "检查项目状态",
            }
        )
    )

    assert result == {
        "session_id": "s-created",
        "agent": "opencode",
        "status": "starting",
        "description": "新 Worker",
    }
    assert created == [("test", "opencode", "新 Worker", "检查项目状态")]


async def test_project_manager_create_session_validates_required_and_lengths():
    create_tool = tool(tools(), "create_session")

    assert "description 和 initial_task 都必填" in await create_tool.handler({})
    assert "description 最多 200" in await create_tool.handler(
        {"description": "x" * 201, "initial_task": "任务"}
    )
    assert "initial_task 最多 4000" in await create_tool.handler(
        {"description": "名称", "initial_task": "x" * 4001}
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
        create_session=lambda conversation, agent, description, initial_task: _create(
            [], conversation, agent, description, initial_task
        ),
        list_delegations=lambda: [],
        get_delegation=lambda _delegation_id: None,
        delegate_to_session=lambda _session_id, _instruction: _send([], "", ""),
        continue_delegation=lambda _delegation_id, _message: _send([], "", ""),
        complete_delegation=lambda _delegation_id: _complete(""),
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
        "create_session",
        "send_to_session",
        "delegate_to_session",
        "list_delegations",
        "get_delegation",
        "continue_delegation",
        "complete_delegation",
    }
    assert any(
        isinstance(event.body, AgentOutputDelta)
        and event.body.text == "项目当前有一个运行中的 Session。"
        for event in events
    )
    await runtime.close()
