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


def build_project_manager_tools(
    *,
    project_name: str,
    list_sessions: ProjectSessionList,
    get_session: ProjectSessionLookup,
    send_to_session: ProjectSessionMessenger,
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
                "需要让已有 Session 继续工作时使用 send_to_session。\n"
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
