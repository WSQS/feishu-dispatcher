"""Dispatcher 的 ToolLoopSessionRuntime 配置。"""

from __future__ import annotations

from ..scheduler import SYSTEM_PROMPT, SchedulerMemory
from .tool_loop_session_runtime import (
    LLMProvider,
    ToolLoopSessionRuntime,
    ToolsProvider,
)


class DispatcherSessionRuntime(ToolLoopSessionRuntime):
    """以 scheduler prompt 与工具驱动 Dispatcher Session。"""

    def __init__(
        self,
        *,
        session_id: str,
        llm_provider: LLMProvider,
        memory: SchedulerMemory,
        tools_provider: ToolsProvider,
    ) -> None:
        super().__init__(
            session_id=session_id,
            llm_provider=llm_provider,
            memory=memory,
            tools_provider=tools_provider,
            system_prompt=SYSTEM_PROMPT,
            llm_unavailable_message="调度器 LLM 未配置",
            error_reply_factory=_dispatcher_error_reply,
            empty_reply="（调度器无输出）",
            max_iterations_reply=(
                "（调度器思考步数超限，请把需求说得更具体，"
                "或用 `/run <项目> <任务>` 直接派发。）"
            ),
            log_context="调度器",
        )


def _dispatcher_error_reply(exc: Exception) -> str:
    return f"调度器出错：{str(exc)[:200]}。可用 `/run <项目> <任务>` 直接派发。"
