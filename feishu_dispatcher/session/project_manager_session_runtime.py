"""Project Manager 的 ToolLoopSessionRuntime 配置与最小工具集。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from ..conversation import ConversationRef
from ..scheduler import ToolSpec
from .tool_loop_session_runtime import (
    LLMProvider,
    SessionMemory,
    ToolLoopSessionRuntime,
)

ProjectSessionList = Callable[[], list[dict[str, Any]]]
ProjectSessionLookup = Callable[[str], dict[str, Any] | None]
ProjectSessionMessenger = Callable[[str, str], Awaitable[str]]
ProjectDelegationList = Callable[[], list[dict[str, Any]]]
ProjectDelegationLookup = Callable[[str], dict[str, Any] | None]
ProjectDelegationCreate = Callable[[str, str], Awaitable[str]]
ProjectDelegationContinue = Callable[[str, str], Awaitable[str]]
ProjectDelegationComplete = Callable[[str], Awaitable[str]]


def build_project_manager_tools(
    *,
    project_name: str,
    list_sessions: ProjectSessionList,
    get_session: ProjectSessionLookup,
    send_to_session: ProjectSessionMessenger,
    list_delegations: ProjectDelegationList,
    get_delegation: ProjectDelegationLookup,
    delegate_to_session: ProjectDelegationCreate,
    continue_delegation: ProjectDelegationContinue,
    complete_delegation: ProjectDelegationComplete,
) -> list[ToolSpec]:
    """构建 Project Manager 只操作当前项目 Session 的最小工具集。"""

    def _belongs_to_project(session: dict[str, Any]) -> bool:
        return session.get("project") == project_name

    async def _list_sessions(_args: dict[str, Any]) -> str:
        sessions = [
            session for session in list_sessions() if _belongs_to_project(session)
        ]
        return json.dumps(sessions, ensure_ascii=False)

    async def _get_session(args: dict[str, Any]) -> str:
        session_id = str(args.get("session_id", "")).strip()
        if not session_id:
            return "参数不足：session_id 必填。"
        session = get_session(session_id)
        if session is None or not _belongs_to_project(session):
            return f"未找到项目 {project_name} 下的 Session {session_id}。"
        return json.dumps(session, ensure_ascii=False)

    async def _send_to_session(args: dict[str, Any]) -> str:
        session_id = str(args.get("session_id", "")).strip()
        message = str(args.get("message", "")).strip()
        if not session_id or not message:
            return "参数不足：session_id 和 message 都必填。"
        session = get_session(session_id)
        if session is None or not _belongs_to_project(session):
            return f"未找到项目 {project_name} 下的 Session {session_id}。"
        return await send_to_session(session_id, message)

    async def _delegate_to_session(args: dict[str, Any]) -> str:
        session_id = str(args.get("session_id", "")).strip()
        instruction = str(args.get("instruction", "")).strip()
        if not session_id or not instruction:
            return "参数不足：session_id 和 instruction 都必填。"
        session = get_session(session_id)
        if session is None or not _belongs_to_project(session):
            return f"未找到项目 {project_name} 下的 Session {session_id}。"
        return await delegate_to_session(session_id, instruction)

    async def _list_delegations(_args: dict[str, Any]) -> str:
        return json.dumps(list_delegations(), ensure_ascii=False)

    async def _get_delegation(args: dict[str, Any]) -> str:
        delegation_id = str(args.get("delegation_id", "")).strip()
        if not delegation_id:
            return "参数不足：delegation_id 必填。"
        delegation = get_delegation(delegation_id)
        if delegation is None:
            return f"未找到项目 {project_name} 下的委派 {delegation_id}。"
        return json.dumps(delegation, ensure_ascii=False)

    async def _continue_delegation(args: dict[str, Any]) -> str:
        delegation_id = str(args.get("delegation_id", "")).strip()
        message = str(args.get("message", "")).strip()
        if not delegation_id or not message:
            return "参数不足：delegation_id 和 message 都必填。"
        if get_delegation(delegation_id) is None:
            return f"未找到项目 {project_name} 下的委派 {delegation_id}。"
        return await continue_delegation(delegation_id, message)

    async def _complete_delegation(args: dict[str, Any]) -> str:
        delegation_id = str(args.get("delegation_id", "")).strip()
        if not delegation_id:
            return "参数不足：delegation_id 必填。"
        if get_delegation(delegation_id) is None:
            return f"未找到项目 {project_name} 下的委派 {delegation_id}。"
        return await complete_delegation(delegation_id)

    session_id_param = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "当前项目内的 Session id",
            }
        },
        "required": ["session_id"],
    }
    delegation_id_param = {
        "type": "object",
        "properties": {
            "delegation_id": {
                "type": "string",
                "description": "当前项目内的委派 id",
            }
        },
        "required": ["delegation_id"],
    }

    return [
        ToolSpec(
            name="list_sessions",
            description=f"列出项目 {project_name} 下的所有 Session 及其状态。",
            parameters={"type": "object", "properties": {}},
            handler=_list_sessions,
        ),
        ToolSpec(
            name="get_session",
            description=f"查看项目 {project_name} 下指定 Session 的详细状态。",
            parameters=session_id_param,
            handler=_get_session,
        ),
        ToolSpec(
            name="send_to_session",
            description=(
                f"向项目 {project_name} 下已有的 Session 转达消息。"
                "只能操作已有 Session，不会创建新 Session。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "当前项目内的目标 Session id",
                    },
                    "message": {
                        "type": "string",
                        "description": "要转达给目标 Session 的消息",
                    },
                },
                "required": ["session_id", "message"],
            },
            handler=_send_to_session,
        ),
        ToolSpec(
            name="delegate_to_session",
            description=(
                f"把一项新的、可追踪的工作委派给项目 {project_name} 下已有的 Session。"
                "Worker 会通过 fdx 回报结果，结束后你会收到自动通知。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "当前项目内的目标 Session id",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "希望 Worker 完成的明确工作目标",
                    },
                },
                "required": ["session_id", "instruction"],
            },
            handler=_delegate_to_session,
        ),
        ToolSpec(
            name="list_delegations",
            description=f"列出项目 {project_name} 下的委派及当前状态。",
            parameters={"type": "object", "properties": {}},
            handler=_list_delegations,
        ),
        ToolSpec(
            name="get_delegation",
            description=f"查看项目 {project_name} 下指定委派的报告与状态。",
            parameters=delegation_id_param,
            handler=_get_delegation,
        ),
        ToolSpec(
            name="continue_delegation",
            description=(
                "向已有委派的同一 Worker 发送补充信息或继续指令。"
                "用于回答 Worker 的问题或要求它继续完善结果。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "delegation_id": {
                        "type": "string",
                        "description": "要继续的委派 id",
                    },
                    "message": {
                        "type": "string",
                        "description": "补充信息或下一步指令",
                    },
                },
                "required": ["delegation_id", "message"],
            },
            handler=_continue_delegation,
        ),
        ToolSpec(
            name="complete_delegation",
            description="接受 Worker 的结果并把委派标记为已完成。",
            parameters=delegation_id_param,
            handler=_complete_delegation,
        ),
    ]


class ProjectManagerSessionRuntime(ToolLoopSessionRuntime):
    """以项目范围的 Session 工具驱动一个 Project Manager Session。"""

    def __init__(
        self,
        *,
        session_id: str,
        project_name: str,
        llm_provider: LLMProvider,
        memory: SessionMemory,
        list_sessions: ProjectSessionList,
        get_session: ProjectSessionLookup,
        send_to_session: ProjectSessionMessenger,
        list_delegations: ProjectDelegationList,
        get_delegation: ProjectDelegationLookup,
        delegate_to_session: ProjectDelegationCreate,
        continue_delegation: ProjectDelegationContinue,
        complete_delegation: ProjectDelegationComplete,
    ) -> None:
        project_name = project_name.strip()
        if not project_name:
            raise ValueError("project_name 不能为空")

        def tools_provider(_conversation: ConversationRef) -> list[ToolSpec]:
            return build_project_manager_tools(
                project_name=project_name,
                list_sessions=list_sessions,
                get_session=get_session,
                send_to_session=send_to_session,
                list_delegations=list_delegations,
                get_delegation=get_delegation,
                delegate_to_session=delegate_to_session,
                continue_delegation=continue_delegation,
                complete_delegation=complete_delegation,
            )

        super().__init__(
            session_id=session_id,
            llm_provider=llm_provider,
            memory=memory,
            tools_provider=tools_provider,
            system_prompt=(
                f"你是项目 {project_name} 的 Project Manager。\n"
                "你的职责是了解和协调该项目下已有的 Session。\n"
                "需要了解当前工作时先使用 list_sessions；需要细节时使用 get_session。\n"
                "新的可追踪工作使用 delegate_to_session；普通、不追踪的补充消息才使用 "
                "send_to_session。\n"
                "收到委派结果通知后：接受结果用 complete_delegation；需要 Worker 继续或"
                "回答它的问题用 continue_delegation；需要用户决定时先向用户提问。\n"
                "你不直接修改代码、不创建 Session、不管理 worktree；信息不足时先向用户追问。"
            ),
            llm_unavailable_message=f"项目 {project_name} 的 Manager LLM 未配置",
            error_reply_factory=lambda exc: (
                f"项目 {project_name} 的 Manager 出错：{str(exc)[:200]}"
            ),
            empty_reply="（Project Manager 无输出）",
            max_iterations_reply="（Project Manager 思考步数超限，请把需求说得更具体。）",
            log_context=f"项目 Manager[{project_name}]",
        )
