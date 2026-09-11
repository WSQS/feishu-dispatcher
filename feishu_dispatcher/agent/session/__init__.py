"""Session 运行时实现包（候选实现）。

契约定于 :mod:`feishu_dispatcher.session_runtime` 与
:mod:`feishu_dispatcher.agent.session_event`；本包只提供 Acp / ToolLoop / Project
Manager / Dispatcher 等候选实现，不重新导出契约。
"""

from .acp_session_runtime import (
    AcpPromptResult,
    AcpSessionRuntime,
    AcpSessionRuntimeHooks,
    AcpTurnResult,
    BackgroundTurnPlacement,
)
from .dispatcher_session_runtime import DispatcherSessionRuntime
from .project_manager_session_runtime import (
    ProjectManagerSessionRuntime,
    build_project_manager_tools,
)
from .session_runtime_registry import SessionRuntimeRegistry
from .tool_loop_session_runtime import SessionMemory, ToolLoopSessionRuntime

__all__ = [
    "DispatcherSessionRuntime",
    "AcpPromptResult",
    "AcpSessionRuntime",
    "AcpSessionRuntimeHooks",
    "AcpTurnResult",
    "BackgroundTurnPlacement",
    "ProjectManagerSessionRuntime",
    "SessionRuntimeRegistry",
    "build_project_manager_tools",
    "SessionMemory",
    "ToolLoopSessionRuntime",
]
