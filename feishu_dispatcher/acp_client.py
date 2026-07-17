"""ACP 客户端封装：把底层 coding agent（Copilot CLI / OpenCode）作为子进程控制。

设计决策 #3：ACP（Agent Client Protocol），JSON-RPC 2.0 over stdio，
agent 作为子进程运行，用官方 ``agent-client-protocol`` PyPI SDK（import 名 ``acp``）。
不要用 PTY hack。

一个 :class:`AcpAgent` 实例对应一个 agent 进程 + 一个 ACP session，
agent 的流式文本输出通过 ``on_output`` 回调近实时推送（由上层做批量节流）。
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import acp
from acp import text_block
from acp.transports import spawn_stdio_transport

logger = logging.getLogger(__name__)

#: ACP 协议版本（当前 SDK 与 Copilot/OpenCode 实测握手成功值）
_PROTOCOL_VERSION = 1

OnOutput = Callable[[str], Awaitable[None]]


@dataclass
class _Callbacks:
    """收集 session_update 里需要转发的文本片段。

    ACP 把 agent 输出分为 agent_message（给用户的最终文本）和
    agent_thought（思考过程）。P0 原型两者都转发，让飞书话题里能看到
    agent 的完整思考链路（设计文档决策 #8：全量转发）。
    """

    on_output: OnOutput


class _ClientImpl:
    """``acp.Client`` Protocol 的最小实现。

    只实现 :meth:`session_update`（接收 agent 的流式 notification）；
    其余方法（request_permission / read_text_file / ...）返回安全默认值——
    P0 原型不处理 permission 交互，让 agent 用各自默认策略。
    """

    def __init__(self, cb: _Callbacks) -> None:
        self._cb = cb

    def on_connect(self, conn: Any) -> None:  # noqa: D401
        """Agent 侧握手完成时被 SDK 调用。"""
        logger.debug("ACP client 侧已连接")

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        text = _extract_text(update)
        if text:
            await self._cb.on_output(text)

    async def request_permission(self, *args: Any, **kwargs: Any) -> Any:
        # P0：自动允许（原型不交互）。返回 allow。
        from acp.schema import RequestPermissionResponse

        return RequestPermissionResponse(outcome="allow")

    async def write_text_file(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        from acp.schema import ReadTextFileResponse

        return ReadTextFileResponse(content="")

    async def create_terminal(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("create_terminal 不支持")

    async def terminal_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("terminal_output 不支持")

    async def release_terminal(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def kill_terminal(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def create_elicitation(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("elicitation 不支持")

    async def complete_elicitation(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None


def _extract_text(update: Any) -> str:
    """从 session_update 的 discriminated union 里抽出可转发文本。"""
    session_update = getattr(update, "session_update", None)
    if session_update == "agent_message_chunk":
        content = getattr(update, "content", None)
        return getattr(content, "text", "") or ""
    if session_update == "agent_thought_chunk":
        content = getattr(update, "content", None)
        thought = getattr(content, "text", "") or ""
        return f"💭 {thought}" if thought else ""
    if session_update == "tool_call":
        title = getattr(update, "title", None)
        return f"\n🔧 {title}\n" if title else ""
    if session_update == "tool_call_update":
        status = getattr(update, "status", None)
        if status in {"completed", "failed"}:
            title = getattr(update, "title", "") or ""
            mark = "✅" if status == "completed" else "❌"
            return f"{mark} {title}\n" if title else ""
    return ""


@dataclass
class AgentSpawn:
    """启动一个 agent 所需的参数���"""

    command: list[str]
    cwd: str
    #: 透传给子进程的环境变量（如 GITHUB_TOKEN）
    env: dict[str, str] = field(default_factory=dict)


class AcpAgent:
    """一个 agent 进程 + 一个 session 的生命周期封装。

    用法::

        agent = AcpAgent(spawn, on_output)
        await agent.start()
        await agent.prompt("帮我写个 hello world")
        ...
        await agent.aclose()
    """

    def __init__(self, spawn: AgentSpawn, on_output: OnOutput) -> None:
        self._spawn = spawn
        self._on_output = on_output
        self._conn: acp.ClientSideConnection | None = None
        self._session_id: str | None = None
        self._transport_ctx: Any = None
        self._proc: Any = None
        self._closed = False

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def start(self) -> None:
        """启动 agent 进程、完成 initialize + new_session 握手。"""
        command = list(self._spawn.command)
        if not command:
            raise ValueError("agent 启动命令为空")
        executable = _resolve_executable(command[0])
        args = command[1:]

        env = dict(os.environ)
        env.update(self._spawn.env)

        logger.info("启动 agent 进程: %s %s (cwd=%s)", executable, args, self._spawn.cwd)
        self._transport_ctx = spawn_stdio_transport(
            executable,
            *args,
            env=env,
            cwd=self._spawn.cwd,
        )
        reader, writer, proc = await self._transport_ctx.__aenter__()
        self._proc = proc

        cb = _Callbacks(on_output=self._on_output)
        self._conn = acp.connect_to_agent(_ClientImpl(cb), writer, reader)

        init_resp = await self._conn.initialize(
            protocol_version=_PROTOCOL_VERSION,
            client_info={"name": "feishu-dispatcher", "version": "0.0.1"},
        )
        logger.info(
            "ACP 握手成功: agent=%s capabilities=%s",
            init_resp.agent_info,
            init_resp.agent_capabilities,
        )

        session = await self._conn.new_session(cwd=self._spawn.cwd)
        self._session_id = session.session_id
        logger.info("已创建 ACP session: %s", self._session_id)

    async def prompt(self, text: str) -> None:
        """向 agent 发送一条 prompt 并等待其处理完毕。

        agent 的流式输出在期间通过 ``on_output`` 回调推送；
        本方法在 ``prompt()`` 的 response 返回（agent 结束本轮）后返回。
        """
        if self._conn is None or self._session_id is None:
            raise RuntimeError("agent 尚未启动")
        await self._conn.prompt(session_id=self._session_id, prompt=[text_block(text)])

    async def aclose(self) -> None:
        """关闭 session 与进程。"""
        if self._closed:
            return
        self._closed = True
        if self._conn is not None and self._session_id is not None:
            try:
                await self._conn.close_session(session_id=self._session_id)
            except Exception:
                logger.debug("close_session 异常（忽略）", exc_info=True)
        if self._conn is not None:
            try:
                await self._conn.close()
            except Exception:
                logger.debug("conn.close 异常（忽略）", exc_info=True)
        if self._transport_ctx is not None:
            try:
                await self._transport_ctx.__aexit__(None, None, None)
            except Exception:
                logger.debug("transport 退出异常（忽略）", exc_info=True)


def _resolve_executable(cmd: str) -> str:
    """Windows 上 ``asyncio.create_subprocess_exec`` 不会查 PATHEXT，
    所以 npm 装的 ``copilot`` / ``opencode`` 必须用 ``.cmd`` shim 启动。
    其他平台原样返回。
    """
    if sys.platform != "win32":
        return cmd
    if os.path.sep in cmd or os.path.altsep and os.path.altsep in cmd:
        # 已是路径：补 .cmd 后缀（如果给出的是无扩展名 shim）
        if not os.path.splitext(cmd)[1]:
            cmd_cmd = cmd + ".cmd"
            if os.path.exists(cmd_cmd):
                return cmd_cmd
        return cmd
    # 裸命令名：在 PATH 里找 .cmd / .bat
    for ext in (".cmd", ".bat", ".exe"):
        found = _which(cmd + ext)
        if found:
            return found
    return cmd


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)
