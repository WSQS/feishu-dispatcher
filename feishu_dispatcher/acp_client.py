"""ACP 客户端封装：把底层 coding agent（Copilot CLI / OpenCode）作为子进程控制。

设计决策 #3：ACP（Agent Client Protocol），JSON-RPC 2.0 over stdio，
agent 作为子进程运行，用官方 ``agent-client-protocol`` PyPI SDK（import 名 ``acp``）。
不要用 PTY hack。

一个 :class:`AcpAgent` 实例对应一个 agent 进程 + 一个 ACP session，
agent 的流式文本输出通过 ``on_output`` 回调近实时推送（由上层做批量节流）。
"""

from __future__ import annotations

import asyncio
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

    实现 :meth:`session_update`（接收 agent 的流式 notification）与
    :meth:`request_permission`（自动放行，P0 不做交互确认）；fs / terminal /
    elicitation 能力未通告也未实现，被调用时显式报 NotImplementedError。
    """

    def __init__(self, cb: _Callbacks) -> None:
        self._cb = cb
        self._fmt = _StreamFormatter()
        self._suppress = False

    def on_connect(self, conn: Any) -> None:  # noqa: D401
        """Agent 侧握手完成时被 SDK 调用。"""
        logger.debug("ACP client 侧已连接")

    def reset_formatter(self) -> None:
        """每个 prompt 回合开始时重置流式格式化状态（新卡片从头开始）。"""
        self._fmt.reset()

    def set_suppress(self, on: bool) -> None:
        """抑制输出转发。恢复会话时 load_session 会重放历史 session/update，
        这些历史已在旧话题里、不该灌进新卡片，故 load 期间抑制。"""
        self._suppress = on

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if self._suppress:
            return
        text = self._fmt.format(update)
        if text:
            await self._cb.on_output(text)

    async def request_permission(
        self,
        session_id: Any = None,
        tool_call: Any = None,
        options: Any = None,
        **kwargs: Any,
    ) -> Any:
        # P0：自动放行（个人本地工具，agent 与用户同权限）。
        # 响应的 outcome 是 discriminated union：必须是
        # AllowedOutcome(outcome="selected", option_id=...) 或
        # DeniedOutcome(outcome="cancelled")，不能是裸字符串。
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        opts = list(options or [])
        choice = next(
            (
                o
                for kind in ("allow_once", "allow_always")
                for o in opts
                if o.kind == kind
            ),
            opts[0] if opts else None,
        )
        if choice is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        logger.debug("自动放行权限请求: %s", getattr(choice, "name", choice.option_id))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=choice.option_id)
        )

    async def write_text_file(self, *args: Any, **kwargs: Any) -> None:
        # 未通告 fs 能力，agent 不应调用；显式报错比假装成功安全（review R22）
        raise NotImplementedError("client 未提供 fs 能力（write_text_file）")

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("client 未提供 fs 能力（read_text_file）")

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


def _content_text(update: Any) -> str:
    content = getattr(update, "content", None)
    return getattr(content, "text", "") or ""


class _StreamFormatter:
    """把 ACP 流式 session_update 转成可转发文本，跨 chunk 维护状态。

    关键：agent 的思考/回复是**逐 token** 流式（尤其 OpenCode），若每个
    thought chunk 都加 💭 前缀会在卡片里刷成「💭 The💭 user💭 …」。故只在
    一段连续 thought 的**开头**加一次 💭，后续 chunk 原样追加；thought 段
    结束转入正式回复时插一个换行分隔。tool_call / plan 是离散事件，各自
    完整成行并重置连续态。
    """

    def __init__(self) -> None:
        self._last: str | None = None  # "thought" | "text" | None

    def reset(self) -> None:
        self._last = None

    def format(self, update: Any) -> str:
        kind = getattr(update, "session_update", None)
        if kind == "agent_message_chunk":
            text = _content_text(update)
            if not text:
                return ""
            out = ("\n" if self._last == "thought" else "") + text
            self._last = "text"
            return out
        if kind == "agent_thought_chunk":
            text = _content_text(update)
            if not text:
                return ""
            out = ("💭 " if self._last != "thought" else "") + text
            self._last = "thought"
            return out
        if kind == "tool_call":
            title = getattr(update, "title", None)
            if not title:
                return ""
            self._last = None
            return f"\n🔧 {title}\n"
        if kind == "tool_call_update":
            status = getattr(update, "status", None)
            if status in {"completed", "failed"}:
                title = getattr(update, "title", "") or ""
                if not title:
                    return ""
                self._last = None
                mark = "✅" if status == "completed" else "❌"
                return f"{mark} {title}\n"
            return ""
        if kind == "plan":
            marks = {"pending": "⬜", "in_progress": "🔄", "completed": "☑️"}
            lines = [
                f"{marks.get(getattr(e, 'status', ''), '⬜')} {getattr(e, 'content', '')}"
                for e in (getattr(update, "entries", None) or [])
            ]
            if not lines:
                return ""
            self._last = None
            return "\n📋 计划:\n" + "\n".join(lines) + "\n"
        # 其余变体（plan_update/usage_update/current_mode_update/available_commands_update
        # 等）有意忽略：P0 只转发对用户可读的主输出与进度。
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

    def __init__(
        self,
        spawn: AgentSpawn,
        on_output: OnOutput,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        self._spawn = spawn
        self._on_output = on_output
        #: 非 None 则恢复该 ACP 会话（load_session）而非新建（new_session）
        self._resume_session_id = resume_session_id
        self._conn: acp.ClientSideConnection | None = None
        self._session_id: str | None = None
        self._transport_ctx: Any = None
        self._proc: Any = None
        self._closed = False
        self._stderr_task: asyncio.Task[None] | None = None
        self._client_impl: _ClientImpl | None = None

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def start(self) -> None:
        """启动 agent 进程、完成 initialize + new_session 握手。

        每个实例只允许启动一次：进程与 session 跨 turn 存活（上下文
        保留在 session 里），重复 start 会泄漏旧进程，直接报错。
        """
        if self._conn is not None:
            raise RuntimeError("agent 已启动，禁止重复 start()")
        if self._closed:
            raise RuntimeError("agent 已关闭，不能再 start()")
        command = list(self._spawn.command)
        if not command:
            raise ValueError("agent 启动命令为空")
        executable = _resolve_executable(command[0])
        args = command[1:]

        # R9：只传配置里显式给的 env，让 SDK 的 default_environment() 白名单
        # 自动合并（APPDATA/USERPROFILE/PATH 等 copilot 登录态所需变量都在内）。
        # 严禁整份 os.environ 透传——会把本机全部机密暴露给 agent 子进程。
        env = dict(self._spawn.env)

        logger.info(
            "启动 agent 进程: %s %s (cwd=%s)",
            executable,
            _redact_argv(args),
            self._spawn.cwd,
        )
        self._transport_ctx = spawn_stdio_transport(
            executable,
            *args,
            env=env,
            cwd=self._spawn.cwd,
        )
        reader, writer, proc = await self._transport_ctx.__aenter__()
        self._proc = proc

        # R8：spawn_stdio_transport 默认 stderr=PIPE 但无人读 → 缓冲区写满后
        # agent 子进程阻塞在写 stderr 上，prompt() 永不返回。起一个后台 task
        # 持续 drain 到日志，既防卡死又保留诊断信息。
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(proc), name="agent-stderr"
        )

        cb = _Callbacks(on_output=self._on_output)
        self._client_impl = _ClientImpl(cb)
        self._conn = acp.connect_to_agent(self._client_impl, writer, reader)

        init_resp = await self._conn.initialize(
            protocol_version=_PROTOCOL_VERSION,
            client_info={"name": "feishu-dispatcher", "version": "0.0.1"},
        )
        logger.info(
            "ACP 握手成功: agent=%s capabilities=%s",
            init_resp.agent_info,
            init_resp.agent_capabilities,
        )

        if self._resume_session_id is not None:
            # 恢复已有会话。load_session 期间 agent 会重放历史 session/update，
            # 抑制转发避免旧对话灌进新卡片（历史已在旧飞书话题里）。
            self._client_impl.set_suppress(True)
            try:
                await self._conn.load_session(
                    cwd=self._spawn.cwd, session_id=self._resume_session_id
                )
            finally:
                self._client_impl.set_suppress(False)
            self._session_id = self._resume_session_id
            logger.info("已恢复 ACP session: %s", self._session_id)
        else:
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
        # 每个回合从头开始：重置流式格式化状态，让本轮首个 thought 重新加 💭
        if self._client_impl is not None:
            self._client_impl.reset_formatter()
        await self._conn.prompt(session_id=self._session_id, prompt=[text_block(text)])

    async def _drain_stderr(self, proc: Any) -> None:
        """R8：持续读取子进程 stderr 进日志，防止 PIPE 缓冲区满导致 agent 卡死。

        stderr 可能为 None（防御判断）。行超 500 字符截断。
        进程退出或 stderr 关闭时 ``readline`` 返回空串，循环自然结束。
        """
        stream = getattr(proc, "stderr", None)
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    text = repr(line)
                if len(text) > 500:
                    text = text[:500] + "…(truncated)"
                logger.debug("agent stderr: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("stderr drain 异常（忽略）", exc_info=True)

    async def aclose(self) -> None:
        """关闭 session 与进程。"""
        if self._closed:
            return
        self._closed = True
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("stderr task 退出异常（忽略）", exc_info=True)
            self._stderr_task = None
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


_SENSITIVE_ARG_WORDS = ("token", "secret", "password", "apikey", "api-key", "api_key")


def _redact_argv(args: list[str]) -> list[str]:
    """日志用 argv 打码：`--token xxx` / `--token=xxx` 形式的敏感值替换为 ***。"""
    redacted: list[str] = []
    mask_next = False
    for arg in args:
        if mask_next:
            redacted.append("***")
            mask_next = False
            continue
        low = arg.lower()
        if any(w in low for w in _SENSITIVE_ARG_WORDS):
            if "=" in arg:
                redacted.append(arg.split("=", 1)[0] + "=***")
            else:
                redacted.append(arg)
                mask_next = True
            continue
        redacted.append(arg)
    return redacted


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
