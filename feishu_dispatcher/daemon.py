"""daemon 主循环：飞书消息 → ACP agent → 飞书话题 完整闭环。

P0 原型范围（设计文档）：
- 硬编码项目匹配（不做 LLM 规划）
- 根消息 `/run` 触发 spawn，话题回复排队追加给同一 agent

生命周期模型（review R2/R3 修复后的设计）：
- 一个 `/run` = 一个 `_AgentSessionRunner`：agent 进程与 ACP session **跨 turn 存活**，
  上下文保留在 session 里
- 每个 session 一个 Turn 队列 + 单消费者 worker task，turn 串行执行
- 话题回复只入队；`/stop`（入队 None 哨兵）、执行出错或 daemon 退出才关闭 agent
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from . import forge
from ._scan_executor import ScanExecutor
from .acp_client import (
    AcpAgent,
    AgentOutputChunk,
    AgentSpawn,
    AgentToolCallUpdate,
    OnAction,
    OnOutput,
    OnToolCall,
    resolve_executable,
)
from .channel import Channel, ChannelMessage, OutputStatus, StreamingOutput
from .channel.feishu import FeishuBridge
from .channel.http import HttpChannel
from .channel.http import ensure_token as ensure_http_channel_token
from .config import DEFAULT_CONFIG_PATH, Config, Project
from .control import ControlServer
from .conversation import ConversationRef
from .llm import build_llm_client
from .scheduler import (
    LLMClient,
    SchedulerMemory,
    build_scheduler_tools,
)
from .session import (
    DispatcherSessionRuntime,
    ProjectManagerSessionRuntime,
    SessionRuntime,
    SessionRuntimeRegistry,
    TurnRequest,
)
from .session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputStarted,
    AgentPlanEntry,
    AgentPlanUpdated,
    OutputOutcome,
    SessionEvent,
    SessionInputAccepted,
    ToolCallObserved,
    session_event_to_dict,
)
from .store import (
    DELEGATION_REPORT_STATUSES,
    Delegation,
    DelegationStore,
    Job,
    JobStore,
    ManagerConversationStore,
    ModelStore,
    ProjectStore,
    Session,
    SessionStore,
)
from .trace_store import (
    SessionTraceRecord,
    SessionTraceStore,
    SessionTraceStoreClosed,
)
from .util.git import create_worktree, delete_branch, remove_worktree
from .workspace_api import (
    file as workspace_file,
)
from .workspace_api import (
    health as workspace_health,
)
from .workspace_api import (
    list_projects as workspace_list_projects,
)
from .workspace_api import (
    tree_children as workspace_tree_children,
)

logger = logging.getLogger(__name__)

SessionEventHandler = Callable[[SessionEvent], Awaitable[None]]

_DISPATCHER_SESSION_ID = "dispatcher"
_PROJECT_MANAGER_SESSION_PREFIX = "manager:"
_DISPATCH_PREFIX = "/run "
_TASK_PREFIX = "/task "
_LIST_CMD = "/agents"
_STOP_CMD = "/stop"
# 话题内：停当前轮但保留 agent；/cancel <新输入> = 停当前轮 + 改做新输入
_CANCEL_CMD = "/cancel"
_DONE_CMD = "/done"
_CLEAR_CMD = "/clear"
_MODEL_CMD = "/model"  # 话题内：/model 列出可选，/model <名> 切换
_RAW_CMD = "/raw"  # 话题内：/raw <文本> 把 <文本> 逐字转发给 agent，绕过话题命令解释
_PROJECT_CMD = "/project"  # root：/project 列出，/project add|remove 增删
_MODELS_CMD = "/models"  # root：/models 列缓存，/models refresh [agent] 主动刷新
_LLM_CMD = "/llm"  # root：/llm 列出调度器 LLM profile，/llm <名> 运行时切换（#74）
_REBOOT_CMD = "/reboot"  # root：重启整个 daemon 进程（cli.py re-exec）
_ATTACH_CMD = "/attach"  # root：附着 daemon 外部的 agent 会话为新 Task
_MANAGER_CMD = "/manager"  # root：创建项目 Manager Conversation
_HELP_CMDS = ("/help", "/?", "/usage")  # root 与话题内通用


def _worktree_slug(name: str) -> str:
    """把项目名压成适合 Windows 路径与 Git ref 的短片段。"""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return slug or "project"


async def _create_session_worktree(project: Project, session_id: str) -> Path:
    """按 Session workspace 约定创建 sibling worktree。"""
    repository = project.path.resolve()
    if not repository.is_dir():
        raise RuntimeError(f"项目路径不是目录：{repository}")
    slug = _worktree_slug(project.name)
    workspace = repository.parent / ".fdx-worktrees" / f"{slug}-{session_id}"
    await create_worktree(
        repository=repository,
        workspace=workspace,
        branch=f"fdx/{slug}/{session_id}",
    )
    return workspace


async def _remove_session_worktree(
    project: Project,
    session_id: str,
    workspace: Path,
) -> None:
    """按 Session workspace 约定删除 worktree 与分支。"""
    slug = _worktree_slug(project.name)
    repository = project.path.resolve()
    await remove_worktree(
        repository=repository,
        workspace=workspace,
    )
    await delete_branch(
        repository=repository,
        branch=f"fdx/{slug}/{session_id}",
    )


#: message_id 去重窗口大小（飞书 ACK 异常时服务端会重推事件）
_DEDUP_CAPACITY = 512

#: 关闭时等控制面停下的上限（秒）；超时即放弃继续关（serve_forever 是 daemon 线程）。#81
_CONTROL_STOP_TIMEOUT = 5.0
_TRACE_EVENTS_LIMIT_MAX = 500
_DELEGATION_REPORT_MAX = 4000

_USAGE = (
    "用法：\n"
    "• `/run <项目名> <任务描述> [--agent <名>]`  派发任务给 agent（可选覆盖默认 agent）\n"
    "• `/attach <项目名> <agent> <session_id> [描述]`  附着外部 agent 会话为新任务"
    "（假定原会话已停止）\n"
    "• `/manager <项目名>`  创建该项目的 Project Manager 话题\n"
    "• `/agents`  列出活跃 + 历史任务\n"
    "• `/task <任务id>`  查看某任务详情与动作日志\n"
    "• `/project`  列出项目；`/project add <名> <agent> <路径>` 注册，`/project remove <名>` 删除\n"
    "• `/models`  列出各 agent 已知模型；`/models refresh [agent]` 主动刷新缓存\n"
    "• `/llm`  列出调度器 LLM profile；`/llm <名>` 运行时切换调度器后端\n"
    "• `/clear`  清理已结束任务的历史\n"
    "• `/reboot`  重启整个 daemon（任务自动恢复）\n"
    "• 在 agent 话题内直接回复 = 追加指令（排队串行执行）\n"
    "• 在 agent 话题内发 `/cancel [新指令]` = 停当前轮（保留 agent），`/stop` = 停并结束，"
    "`/done` = 归档，`/model [名]` = 查看/切换模型"
)

#: 话题内用法（在某个 agent 话题里发 /help 时展示；命令随新增同步维护于此）
_THREAD_USAGE = (
    "话题内用法（你正在某个 agent 的话题里）：\n"
    "• 直接回复 = 追加指令给这个 agent（排队串行执行）\n"
    "• `/cancel [新指令]`  停当前轮但保留 agent；带新指令则停完接着做它\n"
    "• `/stop`  停当前轮并结束该 agent\n"
    "• `/done`  归档该任务（标记完成）\n"
    "• `/model [名]`  查看 / 切换模型\n"
    "• `/raw <指令>`  把 <指令> 原样发给 agent（如 `/raw /model` 让 agent 自己执行 /model）\n"
    "• `/help`  显示本说明\n"
    "（`/run`、`/agents`、`/task` 等控制台命令请回到群主线发送）"
)

#: Session.last_output 截断上限（收尾回复只留精华，防 tasks.json 涨）
_LAST_OUTPUT_MAX = 800

#: Session.error_message 截断上限（turn 异常诊断，异常类型 + 片段）
_ERROR_MSG_MAX = 200


def _clip(text: str, limit: int) -> str:
    """去首尾空白 + 截断到 limit 字符（超出加省略号）。"""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _one_line(text: str, limit: int) -> str:
    """压成一行（合并所有空白）再截断，用于主线通知里的摘要片段。"""
    s = " ".join((text or "").split())
    return s if len(s) <= limit else s[:limit] + "…"


def _short_sid(session_id: str, limit: int = 16) -> str:
    """session_id 截断展示：不完整暴露，防日志/卡片/摘要泄露。"""
    s = (session_id or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def _attach_probe_error(exc: Exception) -> str:
    """把 load_session 探测失败转成人读错误，**尽力**区分三种原因。

    - 超时：``_await_start`` 抛 ``TimeoutError``（``start_timeout`` 兜底）。
    - backend 不支持：ACP SDK 对 ``load_session`` 收到 JSON-RPC ``-32601 Method
      not found`` 时抛 ``RequestError(code=-32601, data={"method": ...})``。
    - 其余失败（参数错、session 过期/损坏、backend 拒绝）统一归为「session 无效或
      过期」，附异常片段供用户自查。
    """
    msg = str(exc)
    if isinstance(exc, TimeoutError):
        return "❌ 恢复外部 session 超时（backend 卡住或会话过大）。未创建任何任务。"
    code = getattr(exc, "code", None)
    if code == -32601 or "method not found" in msg.lower():
        return (
            "❌ 该 agent backend 不支持 load_session（无法恢复外部会话），无法附着。"
            "请换用支持会话恢复的 backend。"
        )
    return (
        f"❌ 无法恢复该外部 session（可能无效或已过期）：{_clip(msg, 120)}\n"
        "未创建任何任务。请确认 session_id 与该 agent、项目 cwd 匹配。"
    )


def _fmt_tokens(n: int) -> str:
    """token 数压成人读的小字（`~850 tok` / `~3.2k tok` / `~1.2M tok`）。"""
    for unit, div in (("M", 1_000_000), ("k", 1000)):
        if n >= div:
            s = f"{n / div:.1f}".rstrip("0").rstrip(".")
            return f"~{s}{unit} tok"
    return f"~{n} tok"


def _with_tokens(footer: str, tokens: int) -> str:
    """把 token 用量拼到既有 footer 尾部（`项目 · 模型：X · ~3.2k tok`）。"""
    tok = _fmt_tokens(tokens)
    return f"{footer} · {tok}" if footer else tok


def _fmt_ts(ts: float) -> str:
    """epoch 秒 → 本地 `MM-DD HH:MM`；0/无 → 「未知」。"""
    if not ts:
        return "未知"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def _fmt_duration(seconds: float) -> str:
    """时长秒 → 人读 `42s` / `12m03s` / `3h12m`（后台任务耗时展示）。"""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _read_tail(path: str, nbytes: int = 2000) -> str:
    """读一个文件的末尾 ``nbytes`` 字节并解码（后台任务输出尾部）；读不到返回空串。"""
    try:
        if path and Path(path).exists():
            raw = Path(path).read_bytes()[-nbytes:]
            return raw.decode("utf-8", errors="replace").strip()
    except Exception:
        logger.debug("读后台任务输出失败 %s", path, exc_info=True)
    return ""


def _read_tail_lines(path: str, lines: int, *, max_bytes: int = 200_000) -> str:
    """读文件末尾 ``lines`` 行（供 bg logs 按需查看）。先只读末尾 ``max_bytes`` 防大文件
    全量载入；读不到返回空串。"""
    try:
        if path and Path(path).exists():
            raw = Path(path).read_bytes()[-max_bytes:]
            text = raw.decode("utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
    except Exception:
        logger.debug("读后台任务输出行失败 %s", path, exc_info=True)
    return ""


def _issue_tag(issue_url: str) -> str:
    """从 issue URL 提末段编号拼成 `#N`（GitHub `/issues/3`、GitLab `/-/issues/3`）。

    只用于展示。取不到数字则返回空串（不显示，不猜）。
    """
    if not issue_url:
        return ""
    last = issue_url.rstrip("/").rsplit("/", 1)[-1]
    return f"#{last}" if last.isdigit() else ""


def _parse_agent_flag(text: str) -> tuple[str, str]:
    """从 /run 的任务文本里剥离 ``--agent <name>``，返回 (任务, agent)。

    agent 为空 = 未指定（用项目 default_agent）。``--agent`` 可在任意位置，但推荐末尾。
    """
    m = re.search(r"\s*--agent\s+(\S+)", text)
    if not m:
        return text.strip(), ""
    task = (text[: m.start()] + " " + text[m.end() :]).strip()
    return task, m.group(1)


@dataclass(frozen=True)
class DaemonRunResult:
    """daemon 运行循环结束后交给启动 facade 的结果。"""

    reboot_requested: bool = False


async def run(
    cfg: Config,
    *,
    discover: bool = False,
    store_path: Path | None = None,
    channel: Channel | None = None,
    channel_key: str | None = None,
    rebooted: bool = False,
) -> bool:
    """启动 daemon：飞书 WS 长连接 + agent 调度。阻塞直到收到退出信号。

    ``discover=True`` 时只打印收到消息的 chat_id，不执行任何命令
    （帮助用户发现群 id 后填进配置）。``store_path`` 是会话持久化文件
    （默认 config 同目录的 sessions.json）。``channel`` 未传时装配现有 Feishu
    Channel；配置启用 ``[http_channel]`` 时与 Feishu 并行注册。注入其它实现时必须
    用 ``channel_key`` 指定稳定身份，且不自动装配其它 Channel。未注入 Channel 时
    ``channel_key`` 默认取 ``feishu``。

    ``rebooted=True`` 表示本次启动由 CLI 的重启 handoff 触发；
    daemon 就绪时据此发送「已重启完成」回执。

    返回是否收到 ``/reboot``——cli.py 据此 re-exec 重启进程。
    """
    default_assembly = channel is None and channel_key is None
    if channel_key is None:
        if channel is not None:
            raise ValueError("注入 Channel 时必须显式提供 channel_key")
        resolved_channel_key = "feishu"
    else:
        resolved_channel_key = channel_key.strip()
        if not resolved_channel_key:
            raise ValueError("channel_key 不能为空")
    if store_path is None:
        store_path = DEFAULT_CONFIG_PATH.parent / "sessions.json"
    loop = asyncio.get_running_loop()
    if channel is None:
        channel = FeishuBridge(
            app_id=cfg.app_id,
            app_secret=cfg.app_secret,
            main_loop=loop,
            chat_whitelist=cfg.chat_id,
            sender_whitelist=() if discover else cfg.sender_whitelist,
            qps=cfg.feishu_qps,
            stream_mode=cfg.stream_mode,
            throttle_window=cfg.throttle_window,
        )
    channels = {resolved_channel_key: channel}
    control_provider = getattr(channel, "control_conversation", None)
    control_conversation = control_provider() if callable(control_provider) else None
    if control_conversation is not None:
        if not isinstance(control_conversation, ConversationRef):
            raise TypeError("control_conversation() 必须返回 ConversationRef 或 None")
        control_channel_key = control_conversation.channel_key()
        if control_channel_key != resolved_channel_key:
            raise ValueError(
                "控制 Conversation 的 Channel key "
                f"{control_channel_key!r} 与注册 key {resolved_channel_key!r} 不一致"
            )
    daemon = _Daemon(
        cfg,
        discover=discover,
        store=SessionStore(store_path.parent / "tasks.json"),
        trace_store=SessionTraceStore(store_path.parent / "session-trace.sqlite"),
        project_store=ProjectStore(store_path.parent / "projects.json"),
        model_store=ModelStore(store_path.parent / "models.json"),
        job_store=JobStore(store_path.parent / "jobs.json"),
        delegation_store=DelegationStore(store_path.parent / "delegations.json"),
        manager_conversation_store=ManagerConversationStore(
            store_path.parent / "manager-conversations.json"
        ),
        _bg_logs_dir=store_path.parent / "bg-logs",
        _sched_memory=SchedulerMemory(
            store_path.parent / "scheduler_memory.json",
            # [llm].memory_rounds 可配；未配 [llm] 时记忆不参与派发，取默认即可
            max_turns=cfg.llm.memory_rounds if cfg.llm else 12,
        ),
        _project_manager_memory_dir=store_path.parent / "project-manager-memory",
        _channels=channels,
        _primary_channel_key=resolved_channel_key,
        _control_conversation=control_conversation,
    )
    if default_assembly and cfg.http_channel and cfg.http_channel.enabled:
        http_token_path = store_path.parent / "http-channel.token"
        http_channel_config = cfg.http_channel
        scan_executor = ScanExecutor()
        try:
            daemon.configure_http_channel(
                token=ensure_http_channel_token(http_token_path),
                loop=loop,
                host=http_channel_config.bind,
                port=http_channel_config.port,
                throttle_window=cfg.throttle_window,
                scan_executor=scan_executor,
            )
        except BaseException:
            await scan_executor.aclose()
            await daemon.aclose()
            raise
        logger.info("HTTP Channel token 已存: %s", http_token_path)
    try:
        result = await daemon.run(rebooted=rebooted)
    finally:
        await daemon.aclose()
    return result.reboot_requested


#: 唤回 agent 处理后台任务完成的引导语（单条；合并批次时也只出现一次）。
_BG_GUIDANCE = (
    "你之前用 `fdx bg run` 起的后台任务已完成（可能多个，见上）。请逐个根据退出码与"
    "输出继续：成功就推进下一步，失败/超时就用输出诊断并修复。"
)


@dataclass
class _BgBatch:
    """待唤回 agent 的后台任务完成批次（#79）。作为**可变**队列项入队；同一 task 相邻
    完成的多个 job 把各自的 ``<bg_job_done>`` 块 append 进来，只唤回一轮。渲染时把所有块
    拼起来 + 一条引导语——多个 job 合并成一轮 prompt，避免各自冷启动/打断。"""

    blocks: list[str] = field(default_factory=list)

    def add(self, block: str) -> None:
        self.blocks.append(block)

    def render(self) -> str:
        return "\n\n".join(self.blocks) + "\n\n" + _BG_GUIDANCE


class _FanoutStreamingOutput:
    """把一个 Session 回合的输出投影到多个 Conversation。"""

    def __init__(self, outputs: list[tuple[ConversationRef, StreamingOutput]]) -> None:
        self._outputs = outputs

    def feed(self, text: str) -> None:
        for conversation, output in self._outputs:
            try:
                output.feed(text)
            except Exception:
                logger.exception(
                    "Session 流式输出 feed 失败 conversation=%s",
                    conversation.to_log_string(),
                )

    def set_footer(self, footer: str) -> None:
        for conversation, output in self._outputs:
            try:
                output.set_footer(footer)
            except Exception:
                logger.exception(
                    "Session 流式输出 footer 失败 conversation=%s",
                    conversation.to_log_string(),
                )

    async def flush(self) -> None:
        await self._call_all("flush")

    async def set_status(self, status: OutputStatus) -> None:
        await self._call_all("set_status", status)

    async def aclose(self) -> None:
        await self._call_all("aclose")

    async def _call_all(self, method: str, *args) -> None:
        async def call_one(
            conversation: ConversationRef, output: StreamingOutput
        ) -> None:
            try:
                await getattr(output, method)(*args)
            except Exception:
                logger.exception(
                    "Session 流式输出 %s 失败 conversation=%s",
                    method,
                    conversation.to_log_string(),
                )

        await asyncio.gather(
            *(call_one(conversation, output) for conversation, output in self._outputs)
        )


@dataclass
class _AgentSessionRunner:
    """一个活跃 agent 的运行时状态。"""

    project_name: str
    agent_label: str
    #: Task 的主 Thread Conversation；生命周期事件与后台输出据此寻址。
    conversation: ConversationRef
    #: 关联的 Session id（当前值来自持久台账主键 Session.session_id）
    session_id: str = ""
    #: agent 工作目录（= Session.workspace）
    cwd: str = ""
    #: 是否由 load_session 恢复而来（影响启动失败时的提示文案）
    resumed: bool = False
    #: 是否由 /attach 附着外部会话而来（= Session.origin == "attach"）；
    #: 影响启动成功/失败的提示文案（区别于普通恢复的「已恢复」）。
    attached: bool = False
    #: 关联的 forge issue URL（= Session.issue_url，#63）；供 footer/展示标归属，空 = 未绑定
    issue_url: str = ""
    #: agent 实例（先建 session、再建 agent，故允许 None）
    agent: "AcpAgent | None" = None
    #: 当前回合的流式输出呈现；回合间为 None
    current_output: StreamingOutput | None = None
    #: Turn 队列；None 是关闭哨兵（/stop / /done / mark_done），_BgBatch 是后台完成批次
    queue: "asyncio.Queue[TurnRequest | _BgBatch | None]" = field(
        default_factory=asyncio.Queue
    )
    #: 队尾未消费的后台任务批次（#79）；非 None ⟺ 队尾是可继续合并的 _BgBatch。
    #: 入任何非 bg 项（enqueue）或被 worker 消费即清空——据此判「队尾能否再合并」。
    pending_bg: "_BgBatch | None" = None
    #: 收到 None 哨兵时置入的终止态：stopped（/stop，默认）或 done（/done / mark_done）
    terminate_status: str = "stopped"
    #: 本轮是否正在跑（worker 卡在 agent.prompt() 里）；/stop 据此决定要不要发 cancel
    turn_in_flight: bool = False
    #: 当前 Turn 的运行事实聚合，供 SessionEvent sink 生成完整收尾事件。
    current_turn_id: str | None = None
    current_conversations: tuple[ConversationRef, ...] = ()
    current_message_chunks: list[str] = field(default_factory=list)
    current_thought_chunks: list[str] = field(default_factory=list)
    session_event_projection_tail: "asyncio.Task[None] | None" = None
    #: agent 控制面身份 token（本次启动一次性下发，注入 env，映射到 Session id）；#68
    bg_token: str = ""
    #: 单消费者 worker，持有 agent 完整生命周期
    worker: "asyncio.Task[None] | None" = None

    def enqueue(self, request: TurnRequest) -> None:
        """入队一个普通 Turn（话题回复 / 首轮 / 新指令 / send_to_task），**断开** bg
        合并邻接（清 pending_bg）——之后完成的 bg 不会跨这个普通项去合并，保 FIFO。"""
        self.pending_bg = None
        self.queue.put_nowait(request)

    def terminate(self) -> None:
        """入队终止哨兵 None，并**丢弃**队列里所有未处理的后台批次（/stop、/done 立即
        停、不排空后台结果，#79）。单线程、无 await，原子——排空后再放 None。"""
        kept: list = []
        while not self.queue.empty():
            it = self.queue.get_nowait()
            if not isinstance(it, _BgBatch):
                kept.append(it)
        for it in kept:
            self.queue.put_nowait(it)
        self.pending_bg = None
        self.queue.put_nowait(None)


class _CurrentRunnerRegistry:
    """Session 的单活 current-runner 槽位；session_id 只是 lookup key。"""

    def __init__(self) -> None:
        self._by_session: dict[str, _AgentSessionRunner] = {}

    def get_for_session(self, session_id: str) -> _AgentSessionRunner | None:
        return self._by_session.get(session_id)

    def register(self, session_id: str, runner: _AgentSessionRunner) -> None:
        if session_id in self._by_session:
            raise RuntimeError(f"session {session_id} 已有 current runner")
        self._by_session[session_id] = runner

    def is_current(self, session_id: str, runner: _AgentSessionRunner) -> bool:
        return self._by_session.get(session_id) is runner

    def remove_if_current(self, session_id: str, runner: _AgentSessionRunner) -> bool:
        if not self.is_current(session_id, runner):
            return False
        del self._by_session[session_id]
        return True

    def values(self) -> list[_AgentSessionRunner]:
        return list(self._by_session.values())

    def count(self) -> int:
        return len(self._by_session)


@dataclass
class _Daemon:
    cfg: Config
    discover: bool = False
    #: 任务台账（默认纯内存，不写盘）；run() 注入文件版（tasks.json）
    store: SessionStore = field(default_factory=lambda: SessionStore(None))
    #: Host-owned Session Trace（默认不启用）；run() 注入 SQLite 版。
    trace_store: SessionTraceStore | None = None
    #: 运行时注册的项目台账（默认纯内存）；run() 注入文件版（projects.json）。
    #: 有效项目 = config.toml 种子（cfg.projects）+ 这里注册的，见 _all_projects
    project_store: ProjectStore = field(default_factory=lambda: ProjectStore(None))
    #: 按 backend 的 available_models 缓存（默认纯内存）；run() 注入文件版（models.json）
    model_store: ModelStore = field(default_factory=lambda: ModelStore(None))
    #: 后台任务台账（默认纯内存）；run() 注入文件版（jobs.json）。#68
    job_store: JobStore = field(default_factory=lambda: JobStore(None))
    #: Project Manager → Worker 委派台账；默认纯内存，run() 注入文件版。
    delegation_store: DelegationStore = field(
        default_factory=lambda: DelegationStore(None)
    )
    #: Project Manager Conversation 绑定台账；默认纯内存，run() 注入文件版。
    manager_conversation_store: ManagerConversationStore = field(
        default_factory=lambda: ManagerConversationStore(None)
    )
    #: 调度器 LLM（P2）；None = 不启用自然语言派发。run() 按 cfg.llm 构造；测试可注入
    _llm: LLMClient | None = None
    #: 当前激活的 LLM profile 名（/llm 切换时更新，不持久化）；#74
    _llm_active: str = ""
    #: 调度器主线对话记忆（跨重启持久化）；默认纯内存，run() 注入文件版
    _sched_memory: SchedulerMemory = field(
        default_factory=lambda: SchedulerMemory(None)
    )
    #: 当前已登记的 Session Runtime；Runtime 自身负责输入队列与执行生命周期。
    _session_runtimes: SessionRuntimeRegistry = field(
        default_factory=SessionRuntimeRegistry
    )
    #: 每个项目 Manager 的独立对话记忆；首次使用时惰性构造。
    _project_manager_memories: dict[str, SchedulerMemory] = field(default_factory=dict)
    #: Project Manager 记忆目录；测试默认 None，不写盘。
    _project_manager_memory_dir: "Path | None" = None
    #: Tool Loop Runtime 的每 Session 事件消费者尾任务；关闭前等待收尾。
    _runtime_event_tails: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    #: 同一 Session 的 Turn 共用一把锁；跨 Channel / runner 串行，不同 Session 可并行。
    _session_turn_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    #: 已预留、尚未登记到 runner 的 agent 名额；覆盖 create_thread 的 await 窗口。
    _pending_agent_launches: int = 0
    #: 由启动装配层注入；稳定 key → Channel 实例。
    _channels: dict[str, Channel] = field(default_factory=dict)
    #: 兼容消息入口使用的主 Channel key。
    _primary_channel_key: str = "feishu"
    #: Feishu Channel 提供的固定控制 Conversation；没有则为 None。
    _control_conversation: ConversationRef | None = None
    #: 进程内 Conversation → Session 身份绑定；Manager Conversation 另有持久化台账。
    _conversation_session_ids: dict[ConversationRef, str] = field(default_factory=dict)
    #: 每个 Session 的单活 current runner；Thread 只经 Session 路由到这里。
    _runners: _CurrentRunnerRegistry = field(default_factory=_CurrentRunnerRegistry)
    #: 可选的运行时事件消费者；不参与 Channel 投影或持久化。
    _session_event_handler: SessionEventHandler | None = None
    _seen_message_keys: OrderedDict[tuple[ConversationRef, str], None] = field(
        default_factory=OrderedDict
    )
    #: 本地控制面（agent CLI 入口）；run() 里启动，测试构造 _Daemon 时为 None（不起 HTTP）
    _control: "ControlServer | None" = None
    #: workspace API 的有界扫描执行服务；run() 装配、_shutdown 关闭（线程池非 daemon）。
    _scan_executor: ScanExecutor | None = None
    #: agent 控制面身份表：token → Session id（启 agent 时登记，关 Session 时清）。#68
    _bg_tokens: dict[str, str] = field(default_factory=dict)
    #: 后台任务 watcher 的强引用（asyncio 只持弱引用，不存会被 GC）。#68
    _bg_watchers: set = field(default_factory=set)
    #: 在跑的后台进程：job_id → proc（launch 登记、watcher 退出清），供 bg kill。#70
    _bg_procs: dict = field(default_factory=dict)
    #: 后台任务输出日志目录；run() 注入（默认 config 同目录 bg-logs/）
    _bg_logs_dir: "Path | None" = None
    #: /reboot 收到后置位；run() 返回它，cli.py re-exec 重启进程
    _reboot_requested: bool = False
    #: run() 里创建的退出事件；/reboot 或退出信号 set 它跳出主循环
    _stop_event: "asyncio.Event | None" = None

    def __post_init__(self) -> None:
        if self._control_conversation is not None:
            self.bind_conversation(
                _DISPATCHER_SESSION_ID,
                self._control_conversation,
            )

    def _validate_channel_registry(self) -> None:
        if not self._channels:
            raise RuntimeError("Channel registry 不能为空")
        for channel_key in self._channels:
            if not channel_key or channel_key != channel_key.strip():
                raise ValueError("Channel key 必须非空且不能包含首尾空白")
        if self._primary_channel_key not in self._channels:
            raise RuntimeError(f"主 Channel 未注册: {self._primary_channel_key!r}")

    def _channel_for(self, conversation: ConversationRef) -> Channel:
        channel_key = conversation.channel_key().strip()
        if not channel_key:
            raise RuntimeError("Conversation 缺少 channel_key")
        try:
            return self._channels[channel_key]
        except KeyError as exc:
            raise RuntimeError(f"Channel 未注册: {channel_key!r}") from exc

    def _serialize_conversation_ref(
        self,
        conversation: ConversationRef,
    ) -> dict[str, object]:
        return self._channel_for(conversation).serialize_conversation_ref(conversation)

    def _deserialize_conversation_ref(
        self,
        channel_key: str,
        payload: dict[str, object],
    ) -> ConversationRef:
        channel_key = channel_key.strip()
        if not channel_key:
            raise ValueError("Conversation 缺少 channel_key")
        try:
            channel = self._channels[channel_key]
        except KeyError as exc:
            raise ValueError(f"Channel 未注册: {channel_key!r}") from exc
        return channel.deserialize_conversation_ref(payload)

    def _conversation_for_session(self, session: Session) -> ConversationRef:
        return self._deserialize_conversation_ref(
            session.channel_key,
            session.conversation_payload,
        )

    def _stored_session_for_conversation(
        self,
        conversation: ConversationRef,
    ) -> Session | None:
        return self.store.by_conversation(
            conversation.channel_key(),
            self._serialize_conversation_ref(conversation),
        )

    def _reserve_agent_slot(self) -> bool:
        if self._runners.count() + self._pending_agent_launches >= self.cfg.max_agents:
            return False
        self._pending_agent_launches += 1
        return True

    def _release_agent_slot(self) -> None:
        if self._pending_agent_launches <= 0:
            raise RuntimeError("agent 名额预留计数失衡")
        self._pending_agent_launches -= 1

    def bind_conversation(
        self, session_id: str, conversation: ConversationRef
    ) -> str | None:
        """绑定 Conversation；Session 已终止时返回其状态，重复绑定幂等。"""
        session = (
            self.store.get(session_id) if session_id != _DISPATCHER_SESSION_ID else None
        )
        if session_id != _DISPATCHER_SESSION_ID:
            if session is None and not self._session_identity_exists(session_id):
                raise ValueError(f"Session 不存在: {session_id}")
            if session is not None and session.is_terminal:
                return session.status

        bound_session_id = self._conversation_session_ids.get(conversation)
        if bound_session_id is not None and not self._session_identity_exists(
            bound_session_id
        ):
            self._conversation_session_ids.pop(conversation, None)
            bound_session_id = None
        if bound_session_id is not None and bound_session_id != session_id:
            raise RuntimeError(
                f"Conversation {conversation!r} 已绑定 Session {bound_session_id}"
            )
        self._conversation_session_ids[conversation] = session_id
        return None

    def _session_identity_exists(self, session_id: str) -> bool:
        return (
            session_id == _DISPATCHER_SESSION_ID
            or self.store.get(session_id) is not None
            or self._session_runtimes.get_for_session(session_id) is not None
        )

    def session_conversation_header(self, session_id: str) -> str:
        """返回 Session 新 Conversation 的展示标题。"""
        if session_id.startswith(_PROJECT_MANAGER_SESSION_PREFIX):
            project_name = session_id.removeprefix(_PROJECT_MANAGER_SESSION_PREFIX)
            project = self._resolve_project(project_name)
            if project is None:
                raise ValueError(f"项目不存在: {project_name}")
            return f"🧭 Project Manager · {project.name}"
        session = self.store.get(session_id)
        if session is None:
            raise ValueError(f"Session 不存在: {session_id}")
        return f"[{session.session_id}] {session.description}"

    def open_session_conversation(
        self,
        session_id: str,
        conversation: ConversationRef,
    ) -> str | None:
        """按 Session 身份打开并绑定一个 Channel Conversation。"""
        if session_id.startswith(_PROJECT_MANAGER_SESSION_PREFIX):
            project_name = session_id.removeprefix(_PROJECT_MANAGER_SESSION_PREFIX)
            self.open_project_manager(project_name, conversation)
            return None
        return self.bind_conversation(session_id, conversation)

    def _session_turn_lock(self, session_id: str) -> asyncio.Lock:
        """返回 Session 身份对应的进程内 Turn 锁。"""
        lock = self._session_turn_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_turn_locks[session_id] = lock
        return lock

    def _session_id_for_conversation(self, conversation: ConversationRef) -> str | None:
        """返回 Conversation 绑定的 Session 身份；失效绑定会同步清理。"""
        session_id = self._conversation_session_ids.get(conversation)
        if session_id is None:
            return None
        if self._session_identity_exists(session_id):
            return session_id
        self._conversation_session_ids.pop(conversation, None)
        return None

    def _session_for_conversation(
        self, conversation: ConversationRef
    ) -> Session | None:
        """按运行时绑定解析持久化 Session。"""
        session_id = self._session_id_for_conversation(conversation)
        if session_id is None:
            return None
        return self.store.get(session_id)

    def _conversations_for_session(
        self,
        session_id: str,
        *,
        source: ConversationRef | None = None,
    ) -> tuple[ConversationRef, ...]:
        """返回 Session 当前绑定的 Conversation 快照，来源优先且不重复。"""
        if not self._session_identity_exists(session_id):
            stale = [
                conversation
                for conversation, bound_session_id in self._conversation_session_ids.items()
                if bound_session_id == session_id
            ]
            for conversation in stale:
                self._conversation_session_ids.pop(conversation, None)
            return ()

        conversations = [
            conversation
            for conversation, bound_session_id in self._conversation_session_ids.items()
            if bound_session_id == session_id
        ]
        if source is not None:
            conversations = [
                source,
                *(item for item in conversations if item != source),
            ]
        return tuple(conversations)

    async def _send_to_conversations(
        self, conversations: tuple[ConversationRef, ...], text: str
    ) -> None:
        await asyncio.gather(
            *(
                self._safe_send_text(text, conversation=conversation)
                for conversation in conversations
            )
        )

    async def _send_to_session(
        self,
        session_id: str,
        text: str,
        *,
        source: ConversationRef | None = None,
    ) -> None:
        await self._send_to_conversations(
            self._conversations_for_session(session_id, source=source), text
        )

    async def _publish_session_event(
        self,
        event: SessionEvent,
        conversations: tuple[ConversationRef, ...],
        *,
        trace_sequence: int | None = None,
    ) -> None:
        await asyncio.gather(
            *(
                self._safe_handle_session_event(
                    event,
                    conversation=conversation,
                    trace_sequence=trace_sequence,
                )
                for conversation in conversations
            )
        )

    async def _emit_session_event(
        self, event: SessionEvent
    ) -> SessionTraceRecord | None:
        """持久化运行事实并交给其它消费者，分别隔离消费者失败。"""
        record = None
        if self.trace_store is not None:
            try:
                record = await asyncio.to_thread(
                    self.trace_store.append,
                    event,
                    conversation_ref_serializer=self._serialize_conversation_ref,
                    conversation_ref_deserializer=self._deserialize_conversation_ref,
                )
            except Exception:
                logger.exception(
                    "SessionEvent 持久化失败 event=%s",
                    event.event_id,
                )
        if self._session_event_handler is not None:
            try:
                await self._session_event_handler(event)
            except Exception:
                logger.exception(
                    "SessionEvent 运行时消费者失败 event=%s",
                    event.event_id,
                )
        try:
            await self._handle_delegation_event(event)
        except Exception:
            logger.exception(
                "Delegation 事件消费失败 event=%s",
                event.event_id,
            )
        return record

    def _queue_runtime_event(self, event: SessionEvent) -> None:
        """按 Session 串行桥接 Runtime 事件到 Daemon 与 Channel 事件管线。"""
        session_id = event.session_id
        previous = self._runtime_event_tails.get(session_id)
        conversations = self._conversations_for_session(session_id)
        if isinstance(event.body, SessionInputAccepted):
            conversations = tuple(
                conversation
                for conversation in conversations
                if conversation != event.body.source
            )
        task: asyncio.Task[None]

        async def consume() -> None:
            try:
                if previous is not None:
                    await previous
                record = await self._emit_session_event(event)
                if not isinstance(
                    event.body,
                    (
                        SessionInputAccepted,
                        AgentOutputStarted,
                        AgentOutputDelta,
                        AgentPlanUpdated,
                        AgentOutputFinished,
                        ToolCallObserved,
                    ),
                ):
                    return
                await self._publish_session_event(
                    event,
                    conversations,
                    trace_sequence=record.sequence if record is not None else None,
                )
            finally:
                if self._runtime_event_tails.get(session_id) is task:
                    self._runtime_event_tails.pop(session_id, None)

        task = asyncio.create_task(consume())
        self._runtime_event_tails[session_id] = task

    async def _wait_runtime_events(self, session_id: str | None = None) -> None:
        """等待指定 Session 或全部 Runtime 事件完成持久化与消费。"""
        if session_id is not None:
            while (tail := self._runtime_event_tails.get(session_id)) is not None:
                await tail
            return
        while self._runtime_event_tails:
            await asyncio.gather(*tuple(self._runtime_event_tails.values()))

    def _register_session_runtime(self, runtime: SessionRuntime) -> None:
        """登记 Runtime 并接入 daemon 的统一 SessionEvent 管线。"""
        if self._session_runtimes.register(runtime):
            runtime.subscribe(self._queue_runtime_event)

    def _project_manager_session_id(self, project_name: str) -> str:
        return f"{_PROJECT_MANAGER_SESSION_PREFIX}{project_name}"

    def _project_manager_memory(self, project_name: str) -> SchedulerMemory:
        memory = self._project_manager_memories.get(project_name)
        if memory is not None:
            return memory
        path = None
        if self._project_manager_memory_dir is not None:
            digest = hashlib.sha256(project_name.encode("utf-8")).hexdigest()[:16]
            path = self._project_manager_memory_dir / f"{digest}.json"
        memory = SchedulerMemory(
            path,
            max_turns=self.cfg.llm.memory_rounds if self.cfg.llm else 12,
        )
        self._project_manager_memories[project_name] = memory
        return memory

    def _project_manager_list_sessions(self, project_name: str) -> list[dict]:
        return [
            {
                "session_id": session.session_id,
                "project": session.project_name,
                "agent": session.agent_label,
                "description": session.description,
                "status": session.status,
                "turns": session.turns,
                "active": self._runners.get_for_session(session.session_id) is not None,
            }
            for session in self.store.all()
            if session.project_name == project_name
        ]

    def _project_manager_get_session(
        self,
        project_name: str,
        session_id: str,
    ) -> dict | None:
        session = self.store.get(session_id)
        if session is None or session.project_name != project_name:
            return None
        details = self._sched_get_task(session_id)
        if details is None:
            return None
        return {
            "session_id": details["task_id"],
            **{key: value for key, value in details.items() if key != "task_id"},
        }

    @staticmethod
    def _delegation_payload(delegation: Delegation) -> dict[str, object]:
        return {
            "delegation_id": delegation.delegation_id,
            "project": delegation.project_name,
            "manager_session_id": delegation.manager_session_id,
            "worker_session_id": delegation.worker_session_id,
            "worker_turn_id": delegation.worker_turn_id,
            "instruction": delegation.instruction,
            "status": delegation.status,
            "report_status": delegation.report_status,
            "report_message": delegation.report_message,
            "created_at": delegation.created_at,
            "updated_at": delegation.updated_at,
        }

    def _project_manager_list_delegations(
        self,
        project_name: str,
    ) -> list[dict[str, object]]:
        return [
            self._delegation_payload(delegation)
            for delegation in self.delegation_store.by_project(project_name)
        ]

    def _project_manager_get_delegation(
        self,
        project_name: str,
        delegation_id: str,
    ) -> dict[str, object] | None:
        delegation = self.delegation_store.get(delegation_id)
        if delegation is None or delegation.project_name != project_name:
            return None
        return self._delegation_payload(delegation)

    @staticmethod
    def _delegation_prompt(
        delegation_id: str,
        instruction: str,
    ) -> str:
        return (
            f"{instruction}\n\n"
            "---\n"
            f"这是 Project Manager 发起的委派，委派编号为 {delegation_id}。\n"
            "完成本轮工作前，请调用以下命令报告结果：\n\n"
            f"fdx delegation report --id {delegation_id} "
            "--status <completed|input-required|blocked> "
            '--message "<结果摘要、所需信息或阻塞原因>"\n\n'
            "completed 表示你认为目标已经完成；input-required 表示需要 Manager 或用户"
            "提供信息；blocked 表示因为环境、权限或外部条件无法继续。"
            "fdx 报告成功后，再正常结束本轮回复。"
        )

    async def _project_manager_delegate_to_session(
        self,
        project_name: str,
        worker_session_id: str,
        instruction: str,
    ) -> str:
        worker = self.store.get(worker_session_id)
        if worker is None or worker.project_name != project_name:
            return f"未找到项目 {project_name} 下的 Session {worker_session_id}。"
        if worker.is_terminal:
            return (
                f"Session {worker_session_id} 已是终止态（{worker.status}），"
                "不能接受新委派。"
            )
        turn_id = secrets.token_hex(16)
        delegation = self.delegation_store.create(
            project_name=project_name,
            manager_session_id=self._project_manager_session_id(project_name),
            worker_session_id=worker_session_id,
            worker_turn_id=turn_id,
            instruction=instruction,
        )
        ok, message = await self._send_turn_to_session(
            worker_session_id,
            self._delegation_prompt(delegation.delegation_id, instruction),
            turn_id=turn_id,
        )
        if not ok:
            self.delegation_store.update(
                delegation.delegation_id,
                status="failed",
                report_status="unreported",
                report_message=message,
            )
            return message
        await self._safe_send_text(
            self._delegation_request_message(delegation, instruction),
            conversation=self._conversation_for_session(worker),
        )
        return (
            f"已创建委派 {delegation.delegation_id} 并交给 Session "
            f"{worker_session_id}；{message}"
        )

    async def _project_manager_continue_delegation(
        self,
        project_name: str,
        delegation_id: str,
        message: str,
    ) -> str:
        delegation = self.delegation_store.get(delegation_id)
        if delegation is None or delegation.project_name != project_name:
            return f"未找到项目 {project_name} 下的委派 {delegation_id}。"
        if delegation.status == "completed":
            return f"委派 {delegation_id} 已完成，不能继续。"
        if delegation.status not in {"waiting_manager", "failed", "cancelled"}:
            return (
                f"委派 {delegation_id} 当前为 {delegation.status}，"
                "请等待 Worker 本轮结束后再继续。"
            )
        turn_id = secrets.token_hex(16)
        self.delegation_store.update(
            delegation_id,
            worker_turn_id=turn_id,
            status="submitted",
            report_status="",
            report_message="",
        )
        ok, result = await self._send_turn_to_session(
            delegation.worker_session_id,
            self._delegation_prompt(delegation_id, message),
            turn_id=turn_id,
        )
        if not ok:
            self.delegation_store.update(
                delegation_id,
                status="failed",
                report_status="unreported",
                report_message=result,
            )
            return result
        worker = self.store.get(delegation.worker_session_id)
        if worker is not None:
            await self._safe_send_text(
                self._delegation_request_message(
                    delegation,
                    message,
                    continued=True,
                ),
                conversation=self._conversation_for_session(worker),
            )
        return f"已让委派 {delegation_id} 继续执行；{result}"

    async def _project_manager_complete_delegation(
        self,
        project_name: str,
        delegation_id: str,
    ) -> str:
        delegation = self.delegation_store.get(delegation_id)
        if delegation is None or delegation.project_name != project_name:
            return f"未找到项目 {project_name} 下的委派 {delegation_id}。"
        if delegation.status == "completed":
            return f"委派 {delegation_id} 已完成。"
        if delegation.status != "waiting_manager":
            return (
                f"委派 {delegation_id} 当前为 {delegation.status}，"
                "只能在 Worker 本轮结束后确认完成。"
            )
        self.delegation_store.update(delegation_id, status="completed")
        return f"委派 {delegation_id} 已确认完成。"

    async def _project_manager_send_to_session(
        self,
        project_name: str,
        session_id: str,
        message: str,
    ) -> str:
        if self._project_manager_get_session(project_name, session_id) is None:
            return f"未找到项目 {project_name} 下的 Session {session_id}。"
        return await self._sched_send_to_task(session_id, message)

    async def _project_manager_create_session(
        self,
        project_name: str,
        conversation: ConversationRef,
        agent: str,
        description: str,
        initial_task: str,
    ) -> dict:
        agent = agent.strip()
        description = description.strip()
        initial_task = initial_task.strip()
        project = self._resolve_project(project_name)
        if project is None:
            return {
                "status": "rejected",
                "error": f"项目不存在: {project_name}",
            }
        manager_session_id = self._project_manager_session_id(project.name)
        if self._session_id_for_conversation(conversation) != manager_session_id:
            return {
                "status": "rejected",
                "error": (
                    f"Conversation 未绑定项目 {project.name} 的 Project Manager。"
                ),
            }
        if not description or not initial_task:
            return {
                "status": "rejected",
                "error": "description 和 initial_task 都必填。",
            }
        if len(agent) > 100 or len(description) > 200 or len(initial_task) > 4000:
            return {
                "status": "rejected",
                "error": "create_session 参数长度超限。",
            }
        agent_label, agent_argv, error = self._resolve_agent(project, agent)
        if agent_argv is None:
            return {"status": "rejected", "error": error}
        existing = next(
            (
                session
                for session in self.store.all()
                if session.project_name == project.name
                and session.description == description
            ),
            None,
        )
        if existing is not None:
            return {
                "session_id": existing.session_id,
                "agent": existing.agent_label,
                "status": "conflict",
                "description": existing.description,
                "error": "当前项目已存在同名 Session；未重复创建。",
            }
        if not self._reserve_agent_slot():
            return {
                "status": "rejected",
                "error": f"已达并发上限 {self.cfg.max_agents}，请先停止一个 Session。",
            }
        reserved_session_id = ""
        workspace = project.path
        pending_worktree: Path | None = None
        try:
            reserved_session_id = self.store.reserve_session_id()
            try:
                workspace = await _create_session_worktree(project, reserved_session_id)
                pending_worktree = workspace
            except Exception as exc:
                return {
                    "status": "rejected",
                    "error": f"创建 Session worktree 失败：{str(exc)[:300]}",
                }
            channel = self._channel_for(conversation)
            session_conversation = await asyncio.to_thread(
                channel.create_thread,
                f"🚀 {agent_label} · {project.name}\n任务: {description}",
            )
            session = self.store.create(
                project_name=project.name,
                agent_label=agent_label,
                description=description,
                channel_key=session_conversation.channel_key(),
                conversation_payload=self._serialize_conversation_ref(
                    session_conversation
                ),
                workspace=str(workspace),
                workspace_kind="worktree",
                session_id=reserved_session_id,
            )
            pending_worktree = None
            try:
                self._launch(
                    session,
                    agent_argv,
                    first_turn=TurnRequest(initial_task, session_conversation),
                )
            except Exception as exc:
                logger.exception(
                    "Project Manager 创建 Worker Session 启动失败 session=%s",
                    session.session_id,
                )
                self.store.update(
                    session.session_id,
                    status="failed",
                    error_message=_clip(
                        f"{type(exc).__name__}: {exc}",
                        _ERROR_MSG_MAX,
                    ),
                )
                return {
                    "session_id": session.session_id,
                    "agent": session.agent_label,
                    "status": "failed",
                    "description": session.description,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
        except Exception as exc:
            if pending_worktree is not None:
                try:
                    await _remove_session_worktree(
                        project,
                        reserved_session_id,
                        pending_worktree,
                    )
                except Exception as cleanup_exc:
                    exc = RuntimeError(f"{exc}；worktree 失败补偿未完成：{cleanup_exc}")
            logger.exception(
                "Project Manager 创建 Worker Session 失败 project=%s",
                project.name,
            )
            return {
                "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
        finally:
            self._release_agent_slot()
        created = self._project_manager_get_session(project.name, session.session_id)
        assert created is not None
        return created

    def _get_project_manager_runtime(
        self,
        project_name: str,
    ) -> ProjectManagerSessionRuntime:
        """按项目惰性创建并注册 Project Manager Runtime。"""
        project_name = project_name.strip()
        project = self._resolve_project(project_name)
        if project is None:
            raise ValueError(f"项目不存在: {project_name}")

        session_id = self._project_manager_session_id(project.name)
        runtime = self._session_runtimes.get_for_session(session_id)
        if runtime is not None:
            if not isinstance(runtime, ProjectManagerSessionRuntime):
                raise RuntimeError("Project Manager Session Runtime 类型不匹配")
            return runtime

        runtime = ProjectManagerSessionRuntime(
            session_id=session_id,
            project_name=project.name,
            llm_provider=lambda: self._llm,
            memory=self._project_manager_memory(project.name),
            list_sessions=partial(self._project_manager_list_sessions, project.name),
            get_session=partial(self._project_manager_get_session, project.name),
            send_to_session=partial(
                self._project_manager_send_to_session,
                project.name,
            ),
            create_session=partial(
                self._project_manager_create_session,
                project.name,
            ),
            list_delegations=partial(
                self._project_manager_list_delegations,
                project.name,
            ),
            get_delegation=partial(
                self._project_manager_get_delegation,
                project.name,
            ),
            delegate_to_session=partial(
                self._project_manager_delegate_to_session,
                project.name,
            ),
            continue_delegation=partial(
                self._project_manager_continue_delegation,
                project.name,
            ),
            complete_delegation=partial(
                self._project_manager_complete_delegation,
                project.name,
            ),
        )
        self._register_session_runtime(runtime)
        return runtime

    @staticmethod
    def _delegation_request_message(
        delegation: Delegation,
        instruction: str,
        *,
        continued: bool = False,
    ) -> str:
        return (
            f"📨 Project Manager {'继续委派' if continued else '委派'}\n"
            f"委派 ID：{delegation.delegation_id}\n"
            f"Manager Session：{delegation.manager_session_id}\n"
            f"工作要求：\n{instruction}"
        )

    @staticmethod
    def _delegation_result_message(
        delegation: Delegation,
        *,
        outcome: OutputOutcome,
    ) -> str:
        return (
            "📬 Worker 委派结果\n"
            f"委派 ID：{delegation.delegation_id}\n"
            f"Manager Session：{delegation.manager_session_id}\n"
            f"Worker Session：{delegation.worker_session_id}\n"
            f"Worker Turn：{delegation.worker_turn_id}\n"
            f"执行结果：{outcome}\n"
            f"Worker 声明：{delegation.report_status}\n"
            f"报告：{delegation.report_message or '（无）'}"
        )

    async def _handle_delegation_event(self, event: SessionEvent) -> None:
        if event.turn_id is None:
            return
        delegation = self.delegation_store.by_worker_turn(
            event.session_id,
            event.turn_id,
        )
        if delegation is None:
            return
        if isinstance(event.body, AgentOutputStarted):
            if delegation.status == "submitted":
                self.delegation_store.update(
                    delegation.delegation_id,
                    status="running",
                )
            return
        if not isinstance(event.body, AgentOutputFinished):
            return
        await self._finish_delegation_turn(
            delegation,
            outcome=event.body.outcome,
            fallback_message=event.body.message,
        )

    async def _finish_delegation_turn(
        self,
        delegation: Delegation,
        *,
        outcome: OutputOutcome,
        fallback_message: str,
    ) -> None:
        if delegation.status not in {"submitted", "running"}:
            return

        report_status = delegation.report_status or "unreported"
        report_message = delegation.report_message or fallback_message
        status = "waiting_manager" if outcome == "completed" else outcome
        self.delegation_store.update(
            delegation.delegation_id,
            status=status,
            report_status=report_status,
            report_message=_clip(report_message, _DELEGATION_REPORT_MAX),
        )
        await self._notify_project_manager_delegation(
            delegation.delegation_id,
            outcome=outcome,
        )

    async def _notify_project_manager_delegation(
        self,
        delegation_id: str,
        *,
        outcome: OutputOutcome,
    ) -> None:
        delegation = self.delegation_store.get(delegation_id)
        if delegation is None:
            return
        conversations = self._conversations_for_session(delegation.manager_session_id)
        if not conversations:
            logger.warning(
                "委派 %s 已结束，但 Manager Session %s 没有绑定 Conversation",
                delegation_id,
                delegation.manager_session_id,
            )
            return
        runtime = self._get_project_manager_runtime(delegation.project_name)
        result_message = self._delegation_result_message(
            delegation,
            outcome=outcome,
        )
        await self._safe_send_text(result_message, conversation=conversations[0])
        text = (
            f"{result_message}\n\n"
            "请判断下一步：接受结果时调用 complete_delegation；需要补充信息或继续完善时"
            "调用 continue_delegation；需要用户决策时直接向用户提问。"
        )
        runtime.submit(TurnRequest(text, conversations[0]))

    def open_project_manager(
        self,
        project_name: str,
        conversation: ConversationRef,
    ) -> ProjectManagerSessionRuntime:
        """打开项目 Manager Conversation，并返回其 Session Runtime。"""
        project_name = project_name.strip()
        project = self._resolve_project(project_name)
        if project is None:
            raise ValueError(f"项目不存在: {project_name}")
        session_id = self._project_manager_session_id(project.name)
        bound_session_id = self._session_id_for_conversation(conversation)
        if bound_session_id is not None and bound_session_id != session_id:
            raise RuntimeError(
                f"Conversation {conversation!r} 已绑定 Session {bound_session_id}"
            )
        runtime = self._get_project_manager_runtime(project.name)
        self.bind_conversation(runtime.session_id, conversation)
        self.manager_conversation_store.add(
            project_name=project.name,
            channel_key=conversation.channel_key(),
            conversation_payload=self._serialize_conversation_ref(conversation),
        )
        return runtime

    def _restore_project_manager_conversations(self) -> None:
        """从台账恢复 Project Manager Runtime 与 Conversation 绑定。"""
        for stored in self.manager_conversation_store.all():
            try:
                conversation = self._deserialize_conversation_ref(
                    stored.channel_key,
                    stored.conversation_payload,
                )
                self.open_project_manager(stored.project_name, conversation)
            except Exception:
                logger.warning(
                    "恢复 Project Manager Conversation 失败 project=%s channel=%s",
                    stored.project_name,
                    stored.channel_key,
                    exc_info=True,
                )

    async def aclose(self) -> None:
        """幂等关闭由启动 facade 直接装配的 Session Trace Store。"""
        trace_store = self.trace_store
        self.trace_store = None
        if trace_store is None:
            return
        try:
            await asyncio.to_thread(trace_store.close)
        except Exception:
            logger.warning("Session Trace 存储关闭失败，忽略", exc_info=True)

    def configure_http_channel(
        self,
        *,
        token: str,
        loop: asyncio.AbstractEventLoop,
        host: str,
        port: int,
        throttle_window: float,
        scan_executor: ScanExecutor,
    ) -> None:
        """装配 HTTP Channel，并在构造成功后接管其运行时资源。"""
        if "http" in self._channels:
            raise RuntimeError("HTTP Channel 已注册")
        if self._scan_executor is not None:
            raise RuntimeError("扫描执行服务已注册")
        http_channel = HttpChannel(
            token,
            loop,
            host=host,
            port=port,
            routes={
                ("GET", "/api/health"): workspace_health,
                ("GET", "/api/tasks"): self._http_list_tasks,
                ("GET", "/api/tasks/{task_id}/events"): self._http_task_events,
                ("GET", "/api/projects"): workspace_list_projects,
                (
                    "GET",
                    "/api/projects/{name}/tree/children",
                ): workspace_tree_children,
                ("GET", "/api/projects/{name}/file"): workspace_file,
            },
            route_context={
                "all_projects": self._all_projects,
                "scan_executor": scan_executor,
            },
            session_conversation_header=self.session_conversation_header,
            open_session_conversation=self.open_session_conversation,
            conversation_ref_serializer=self._serialize_conversation_ref,
            throttle_window=throttle_window,
        )
        self._scan_executor = scan_executor
        self._channels["http"] = http_channel

    def _queue_session_event_projection(
        self,
        sess: _AgentSessionRunner,
        event: SessionEvent,
        trace_sequence: int | None,
    ) -> None:
        """按 Turn 顺序异步投影事件，避免 Channel 延迟阻塞 ACP 输出。"""
        previous = sess.session_event_projection_tail
        conversations = sess.current_conversations

        async def project() -> None:
            if previous is not None:
                await previous
            await self._publish_session_event(
                event,
                conversations,
                trace_sequence=trace_sequence,
            )

        sess.session_event_projection_tail = asyncio.create_task(project())

    async def _finish_agent_output(
        self,
        sess: _AgentSessionRunner,
        outcome: OutputOutcome,
    ) -> None:
        if sess.current_turn_id is None:
            return
        event = SessionEvent(
            event_id=secrets.token_hex(16),
            session_id=sess.session_id,
            turn_id=sess.current_turn_id,
            occurred_at=datetime.now(timezone.utc),
            body=AgentOutputFinished(
                message="".join(sess.current_message_chunks),
                thought="".join(sess.current_thought_chunks),
                outcome=outcome,
            ),
        )
        record = await self._emit_session_event(event)
        self._queue_session_event_projection(
            sess,
            event,
            record.sequence if record is not None else None,
        )

    def _open_session_output(
        self,
        conversations: tuple[ConversationRef, ...],
        title: str,
        *,
        footer: str,
    ) -> StreamingOutput:
        outputs: list[tuple[ConversationRef, StreamingOutput]] = []
        for conversation in conversations:
            try:
                output = self._channel_for(conversation).open_output(
                    conversation,
                    title,
                    footer=footer,
                )
            except Exception:
                logger.exception(
                    "Session 输出创建失败 conversation=%s",
                    conversation.to_log_string(),
                )
                continue
            outputs.append((conversation, output))
        return _FanoutStreamingOutput(outputs)

    def _start_channels(self) -> None:
        self._validate_channel_registry()
        started: list[tuple[str, Channel]] = []
        try:
            for channel_key, channel in self._channels.items():
                channel.start(self._handle_channel_message)
                started.append((channel_key, channel))
        except Exception:
            for channel_key, channel in reversed(started):
                try:
                    channel.stop()
                except Exception:
                    logger.warning(
                        "Channel %s 启动回滚关闭失败，忽略",
                        channel_key,
                        exc_info=True,
                    )
            raise

    def _restart_dead_channels(self) -> None:
        for channel_key, channel in self._channels.items():
            try:
                alive = channel.is_alive()
            except Exception:
                logger.exception("Channel %s 健康检查失败", channel_key)
                continue
            if alive:
                continue
            logger.error("Channel %s 连接已死亡，尝试重启…", channel_key)
            try:
                channel.restart()
            except Exception:
                logger.exception("Channel %s 重启失败", channel_key)

    def _stop_channels(self) -> None:
        for channel_key, channel in self._channels.items():
            try:
                channel.stop()
            except Exception:
                logger.warning("Channel %s 关闭失败，忽略", channel_key, exc_info=True)

    async def run(self, *, rebooted: bool = False) -> DaemonRunResult:
        loop = asyncio.get_running_loop()
        self._validate_channel_registry()
        if self._llm is None:
            self._llm = build_llm_client(self.cfg.llm)
            self._llm_active = self.cfg.llm_active
        self._stop_event = asyncio.Event()
        # 本地控制面（agent CLI → daemon）：127.0.0.1 + 一次性 token 鉴权（#68）。
        # 路由表可扩展，首个 endpoint 是后台任务 run。
        self._control = ControlServer(
            loop,
            resolve_token=self._bg_tokens.get,
            routes={
                ("POST", "/v1/bg/run"): self._ctl_bg_run,
                ("POST", "/v1/bg/list"): self._ctl_bg_list,
                ("POST", "/v1/bg/logs"): self._ctl_bg_logs,
                ("POST", "/v1/bg/kill"): self._ctl_bg_kill,
                ("POST", "/v1/delegations/report"): self._ctl_delegation_report,
            },
        )
        self._control.start()
        self._start_channels()
        self._restore_project_manager_conversations()
        logger.info(
            "feishu-dispatcher daemon 已启动（Channel: %d；调度器 LLM: %s），等待消息…",
            len(self._channels),
            "on" if self._llm else "off",
        )
        # re-exec 重启起来的进程：给控制台发一条「已重启」回执（HTTP，不依赖 WS）
        if rebooted:
            await self._notify_main("✅ daemon 已重启完成。")
        try:
            # R13：看门狗——最多等 30s 或直到 _stop_event 被 set（/reboot / 退出）；
            # 超时则分别检查 Channel 是否存活，只重启失活实例。
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass  # 正常：每 30s 醒来检查一次
                if self._stop_event.is_set():
                    break
                self._restart_dead_channels()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("收到退出信号，清理 agent…")
        finally:
            await self._shutdown()
        return DaemonRunResult(reboot_requested=self._reboot_requested)

    # ------------------------------------------------------------------ #
    # 消息分发
    # ------------------------------------------------------------------ #

    def _is_duplicate(self, conversation: ConversationRef, message_id: str) -> bool:
        """在 Conversation 作用域内按 message_id 幂等去重。"""
        if not message_id:
            return False
        message_key = (conversation, message_id)
        if message_key in self._seen_message_keys:
            return True
        self._seen_message_keys[message_key] = None
        while len(self._seen_message_keys) > _DEDUP_CAPACITY:
            self._seen_message_keys.popitem(last=False)
        return False

    async def _handle_message(self, msg: ChannelMessage) -> None:
        """当前主 Channel 的直接消息入口（测试与内部兼容路径）。"""
        await self._handle_channel_message(msg)

    async def _handle_channel_message(self, msg: ChannelMessage) -> None:
        """携带稳定 Channel 身份的内部消息入口。"""
        conversation = msg.conversation
        channel_key = conversation.channel_key().strip()
        if not channel_key:
            raise ValueError("channel_key 不能为空")
        # 忽略无发送者的系统消息
        if not msg.sender_id:
            return
        if self._is_duplicate(conversation, msg.message_id):
            logger.info(
                "忽略重复消息 channel=%s message_id=%s",
                channel_key,
                msg.message_id,
            )
            return
        logger.info(
            "收到消息 conversation=%s msg=%s text=%r",
            conversation.to_log_string(),
            msg.message_id,
            msg.text,
        )

        # R10：discover 模式只打印会话标识帮助发现，不执行任何命令
        if self.discover:
            logger.info(
                "[discover] conversation=%r sender_id=%r — 填入 config.toml 的 chat_id 即可",
                conversation.to_log_string(),
                msg.sender_id,
            )
            return

        bound_session_id = self._session_id_for_conversation(conversation)
        persisted_session = (
            self._stored_session_for_conversation(conversation)
            if bound_session_id is None
            else None
        )
        if bound_session_id is not None and bound_session_id != _DISPATCHER_SESSION_ID:
            runtime = self._session_runtimes.get_for_session(bound_session_id)
            if runtime is not None:
                if msg.text.strip():
                    runtime.submit(TurnRequest(msg.text.strip(), conversation))
                return
        if (
            bound_session_id is not None and bound_session_id != _DISPATCHER_SESSION_ID
        ) or persisted_session is not None:
            await self._forward_to_agent(msg, conversation=conversation)
            return

        text = msg.text.strip()
        if text.startswith(_DISPATCH_PREFIX):
            await self._spawn_for_root(
                msg,
                text[len(_DISPATCH_PREFIX) :].strip(),
                conversation=conversation,
            )
        elif text == _ATTACH_CMD or text.startswith(_ATTACH_CMD + " "):
            await self._attach_for_root(
                msg,
                text[len(_ATTACH_CMD) :].strip(),
                conversation=conversation,
            )
        elif conversation.channel_key() == "feishu" and (
            text == _MANAGER_CMD or text.startswith(_MANAGER_CMD + " ")
        ):
            await self._open_project_manager_for_root(
                text[len(_MANAGER_CMD) :].strip(),
                conversation=conversation,
            )
        elif text.startswith(_TASK_PREFIX):
            await self._show_task(
                msg,
                text[len(_TASK_PREFIX) :].strip(),
                conversation=conversation,
            )
        elif text == _LIST_CMD:
            await self._list_agents(msg, conversation=conversation)
        elif text == _CLEAR_CMD:
            n = self.store.clear_terminal()
            await self._send_user(
                f"🧹 已清理 {n} 条已结束任务的历史。",
                conversation=conversation,
            )
        elif text == _PROJECT_CMD or text.startswith(_PROJECT_CMD + " "):
            await self._handle_project_cmd(
                msg,
                text[len(_PROJECT_CMD) :].strip(),
                conversation=conversation,
            )
        elif text == _MODELS_CMD or text.startswith(_MODELS_CMD + " "):
            await self._handle_models_cmd(
                msg,
                text[len(_MODELS_CMD) :].strip(),
                conversation=conversation,
            )
        elif text == _LLM_CMD or text.startswith(_LLM_CMD + " "):
            await self._handle_llm_cmd(
                msg,
                text[len(_LLM_CMD) :].strip(),
                conversation=conversation,
            )
        elif text == _REBOOT_CMD:
            await self._reboot(msg, conversation=conversation)
        elif text in _HELP_CMDS:
            await self._send_user(_USAGE, conversation=conversation)
        elif self._llm is not None and text and not text.startswith("/"):
            # P2：自然语言交给调度器 LLM 理解并派发（未配置 LLM 则回退到用法）
            await self._dispatch_nl(msg, text, conversation=conversation)
        else:
            await self._send_user(_USAGE, conversation=conversation)

    # ------------------------------------------------------------------ #
    # 项目：有效项目表（种子 + 注册）解析 + /project 命令 + register_project 工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _classify_path_error(path: str) -> str | None:
        """按失败原因分类校验注册路径，返回给用户/LLM 的中文报错（合法返回 None）。

        路径合法（已是存在的目录）→ None；否则返回区分原因的消息：
        - 指到了文件上 →「这是文件不是目录」；
        - 某一级拼错/不存在 → 指出从哪一级断开，并列出其父目录下的子目录（截断防刷屏）。
        """
        p = Path(path)
        if p.is_dir():
            return None
        if p.exists():  # 存在但不是目录
            return f"这是文件不是目录：{path}"
        # 逐级向上找最后一个存在的祖先目录，定位断点在哪一级
        missing_segments: list[str] = []
        ancestor = p
        while not ancestor.exists() and ancestor != ancestor.parent:
            missing_segments.append(ancestor.name)
            ancestor = ancestor.parent
        missing_segments.reverse()  # 自顶向下：先断的那一级在最前
        # 列出祖先下的子目录，方便用户对照纠正拼写（截断防刷屏）
        siblings: list[str] = []
        try:
            siblings = sorted(
                child.name for child in ancestor.iterdir() if child.is_dir()
            )
        except OSError:
            pass  # 无权限或祖先本身也不可达（如不存在的盘符）→ 不列子目录
        cap = 10
        if len(siblings) > cap:
            siblings = siblings[:cap] + [f"...（共 {len(siblings)} 个）"]
        hint = ancestor.name or str(ancestor)
        parts = [f"路径不存在：{path}", f"从「{missing_segments[0]}」这一级开始找不到"]
        if siblings:
            parts.append(f"「{hint}」下的子目录：" + "、".join(siblings))
        else:
            parts.append(
                f"「{hint}」下没有可列出的子目录（可能是拼写错误或盘符不存在）。"
            )
        return "\n".join(parts)

    def _all_projects(self) -> dict[str, Project]:
        """有效项目表：config.toml 种子（cfg.projects）+ 运行时注册（projects.json）。

        同名以注册项优先（正常不会撞——注册时禁止占用种子名）。
        """
        merged = dict(self.cfg.projects)
        merged.update(self.project_store.all())
        return merged

    def _resolve_project(self, name: str) -> Project | None:
        return self._all_projects().get(name)

    def _register_project(self, name: str, agent: str, path: str) -> tuple[bool, str]:
        """注册/更新一个项目（``/project add`` 与 ``register_project`` 共用底层）。

        返回 (是否成功, 给用户/LLM 的消息)。校验：三项都必填；项目名非空且不含
        空格（否则 ``/run <项目> <任务>`` 会切错）、不占用 config.toml 种子名；
        agent 必须在 ``[agents]`` 里；path 必须是已存在目录（非 git 仓 warning 放行）。
        """
        name, agent, path = name.strip(), agent.strip(), path.strip()
        if not name or not agent or not path:
            return False, "参数不足：需要 名称、agent、路径 三项。"
        if any(c.isspace() for c in name):
            return False, f"项目名不能含空格：'{name}'。"
        if name in self.cfg.projects:
            return (
                False,
                f"'{name}' 是 config.toml 里的项目，请改配置文件而非在此注册。",
            )
        if agent not in self.cfg.agents:
            known = ", ".join(self.cfg.agents) or "(无)"
            return False, f"未知 agent '{agent}'。已配置 agent: {known}"
        err = self._classify_path_error(path)
        if err is not None:
            return False, err
        p = Path(path)
        warn = ""
        if not (p / ".git").exists():
            warn = "（注意：该目录不是 git 仓库，P1 并发 worktree 隔离将无法启用）"
        verb = "更新" if self.project_store.get(name) else "注册"
        self.project_store.add(Project(name=name, path=p, default_agent=agent))
        logger.info("%s项目 %s（agent=%s, path=%s）", verb, name, agent, p)
        return True, f"✅ 已{verb}项目 {name}（agent={agent}，路径={p}）{warn}"

    def _format_project_list(self) -> str:
        merged = self._all_projects()
        if not merged:
            return "暂无项目。用 `/project add <名称> <agent> <路径>` 注册。"
        registered = self.project_store.all()
        lines = ["项目列表："]
        for name, p in merged.items():
            src = "已注册" if name in registered else "种子"
            lines.append(f"• {name}（{p.default_agent}）— {p.path} [{src}]")
        lines.append(
            "`/project add <名称> <agent> <路径>` 增 · `/project remove <名称>` 删"
        )
        return "\n".join(lines)

    async def _handle_project_cmd(
        self,
        msg: ChannelMessage,
        arg: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """root：``/project`` 列出、``/project add|remove`` 增删（对话/命令层）。"""
        if not arg:
            await self._send_user(
                self._format_project_list(),
                conversation=conversation,
            )
            return
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "add":
            fields = rest.split(maxsplit=2)
            if len(fields) < 3:
                await self._send_user(
                    "格式：`/project add <名称> <agent> <路径>`",
                    conversation=conversation,
                )
                return
            _, out = self._register_project(fields[0], fields[1], fields[2])
            await self._send_user(out, conversation=conversation)
        elif sub == "remove":
            await self._send_user(
                self._remove_project(rest),
                conversation=conversation,
            )
        else:
            await self._send_user(
                "用法：`/project`（列出）/ "
                "`/project add <名称> <agent> <路径>` / "
                "`/project remove <名称>`",
                conversation=conversation,
            )

    def _remove_project(self, name: str) -> str:
        """删除一个已注册项目（种子项目改配置文件；引用它的历史任务不受影响）。"""
        name = name.strip()
        if not name:
            return "格式：`/project remove <名称>`"
        if name in self.cfg.projects:
            return f"'{name}' 是 config.toml 里的项目，删除请改配置文件。"
        if not self.project_store.remove(name):
            return f"未找到已注册项目 '{name}'。"
        refs = sum(1 for t in self.store.all() if t.project_name == name)
        tip = f"（有 {refs} 个历史任务引用它，记录仍保留）" if refs else ""
        logger.info("删除项目 %s（%d 个历史任务引用）", name, refs)
        return f"🗑️ 已删除项目 {name}。{tip}"

    # ------------------------------------------------------------------ #
    # 调度器 LLM profile：/llm 列出 / 切换（#74，运行时换后端，不持久化）
    # ------------------------------------------------------------------ #

    async def _handle_llm_cmd(
        self,
        msg: ChannelMessage,
        arg: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """root：``/llm`` 列出 LLM profile、``/llm <名>`` 切换激活的（重建 client，下轮生效）。"""
        profiles = self.cfg.llm_profiles
        if not profiles:
            await self._send_user(
                "未配置调度器 LLM（`[llm]` 段为空），无可切换的 profile。",
                conversation=conversation,
            )
            return
        arg = arg.strip()
        if not arg:
            lines = [
                "调度器 LLM profile（`/llm <名>` 切换，不持久化、重启回到配置默认）:"
            ]
            for name, s in profiles.items():
                mark = "▶ " if name == self._llm_active else "  "
                lines.append(f"{mark}{name}：{s.model}（{s.api}）")
            await self._send_user("\n".join(lines), conversation=conversation)
            return
        if arg not in profiles:
            await self._send_user(
                f"未知 profile '{arg}'。可选：{', '.join(profiles)}",
                conversation=conversation,
            )
            return
        if arg == self._llm_active:
            await self._send_user(
                f"当前已是 profile「{arg}」。",
                conversation=conversation,
            )
            return
        self._llm = build_llm_client(profiles[arg])
        self._llm_active = arg
        s = profiles[arg]
        logger.info("调度器 LLM 切换 → %s（%s · %s）", arg, s.model, s.api)
        await self._send_user(
            f"✅ 已切换调度器 LLM → 「{arg}」（{s.model} · {s.api}）。下次派发生效。",
            conversation=conversation,
        )

    # ------------------------------------------------------------------ #
    # 模型缓存：/models 列出 / refresh 主动刷新（#65）
    # ------------------------------------------------------------------ #

    async def _handle_models_cmd(
        self,
        msg: ChannelMessage,
        arg: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """root：``/models`` 列缓存、``/models refresh [agent]`` 主动刷新。"""
        arg = arg.strip()
        if arg == "refresh" or arg.startswith("refresh "):
            target = arg[len("refresh") :].strip()
            backends = [target] if target else list(self.cfg.agents.keys())
            if not backends:
                await self._send_user(
                    "没有配置任何 [agents]。",
                    conversation=conversation,
                )
                return
            await self._send_user(
                f"🔄 正在刷新模型缓存（{', '.join(backends)}）…冷启动稍慢，请稍候。",
                conversation=conversation,
            )
            results = await asyncio.gather(*(self._refresh_models(b) for b in backends))
            lines = [("✅ " if ok else "❌ ") + m for ok, m in results]
            await self._send_user(
                "刷新完成：\n" + "\n".join(lines),
                conversation=conversation,
            )
            return
        # 无参 = 列缓存
        cache = self.model_store.all()
        if not cache:
            await self._send_user(
                "模型缓存为空。发 `/models refresh` 采集（会临时起 agent 读取模型清单）。",
                conversation=conversation,
            )
            return
        lines = []
        for backend, d in cache.items():
            models = d.get("models") or []
            when = _fmt_ts(d.get("refreshed_at", 0.0))
            shown = "、".join(models) if models else "（该后端不暴露模型）"
            lines.append(f"• {backend}（更新于 {when}）：{shown}")
        lines.append("`/models refresh [agent]` 主动刷新。")
        await self._send_user(
            "模型缓存：\n" + "\n".join(lines),
            conversation=conversation,
        )

    async def _refresh_models(self, backend: str) -> tuple[bool, str]:
        """临时起一个该 backend 的一次性 agent、读 available_models 后关掉，刷新缓存。

        不进 current-runner registry——不占 max_agents。返回 (是否成功, 人读结果串)。
        """
        argv = self.cfg.agents.get(backend)
        if not argv:
            return False, f"{backend}：不在 [agents] 配置里"
        projs = self._all_projects()
        cwd = next(
            (str(p.path) for p in projs.values() if p.default_agent == backend),
            next((str(p.path) for p in projs.values()), "."),
        )

        async def _noop_out(_output: AgentOutputChunk) -> None:
            pass

        async def _noop_act(_a: dict) -> None:
            pass

        agent = self._make_agent(
            AgentSpawn(
                command=list(argv),
                cwd=cwd,
                env=dict(self.cfg.agent_env.get(backend, {})),
            ),
            _noop_out,
            _noop_act,
        )
        try:
            await asyncio.wait_for(agent.start(), timeout=60)
            models = list(getattr(agent, "available_models", []) or [])
            self.model_store.update(backend, models)
            detail = f" {models}" if models else "（该后端不暴露模型）"
            return True, f"{backend}：{len(models)} 个模型{detail}"
        except Exception as exc:
            logger.exception("刷新模型缓存失败 backend=%s", backend)
            return False, f"{backend}：刷新失败 {type(exc).__name__}: {str(exc)[:100]}"
        finally:
            try:
                await agent.aclose()
            except Exception:
                pass

    def _sched_list_models(self, agent: str = "") -> dict:
        """list_models 工具：读模型缓存（backend -> 模型列表）。agent 空 = 所有后端。"""
        cache = self.model_store.all()
        if agent:
            return {agent: self.model_store.get(agent)}
        return {k: (v.get("models") or []) for k, v in cache.items()}

    def _resolve_agent(
        self, project: Project, override: str
    ) -> tuple[str, list[str] | None, str]:
        """定本次实际用的 agent：``override`` 非空则用它（须在 [agents]），否则用项目
        ``default_agent``。返回 ``(agent_label, argv, 错误串)``；argv=None 表示出错。"""
        label = (override or project.default_agent or "").strip()
        argv = self.cfg.agents.get(label)
        if not argv:
            known = ", ".join(self.cfg.agents) or "(无)"
            if override:
                return label, None, f"未知 agent '{override}'。可选: {known}"
            return (
                label,
                None,
                f"项目 '{project.name}' 的 agent '{label}' 未配置。可选: {known}",
            )
        return label, argv, ""

    async def _spawn_for_root(
        self,
        msg: ChannelMessage,
        body: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """解析 ``/run <project> <task> [--agent <name>]``，建 session 并启动 worker。"""
        usage = "格式：`/run <项目名> <任务描述> [--agent <agent>]`"
        parts = body.split(maxsplit=1)
        if len(parts) < 2:
            await self._send_user(usage, conversation=conversation)
            return
        project_name = parts[0].strip()
        task, agent_override = _parse_agent_flag(parts[1].strip())
        if not task:
            await self._send_user(usage, conversation=conversation)
            return
        project = self._resolve_project(project_name)
        if project is None:
            known = ", ".join(self._all_projects()) or "(无)"
            await self._send_user(
                f"未知项目 '{project_name}'。已知项目: {known}",
                conversation=conversation,
            )
            return
        agent_label, agent_argv, err = self._resolve_agent(project, agent_override)
        if agent_argv is None:
            await self._send_user(err, conversation=conversation)
            return

        if not self._reserve_agent_slot():
            await self._send_user(
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，请先 `/stop` 一个。",
                conversation=conversation,
            )
            return
        try:
            header = f"🚀 {agent_label} · {project_name}\n任务: {task}"
            channel = self._channel_for(conversation)
            session_conversation = await asyncio.to_thread(
                channel.create_thread, header
            )
            new_task = self.store.create(
                project_name=project_name,
                agent_label=agent_label,
                description=task,
                channel_key=session_conversation.channel_key(),
                conversation_payload=self._serialize_conversation_ref(
                    session_conversation
                ),
                workspace=str(project.path),
            )
            self._launch(
                new_task,
                agent_argv,
                first_turn=TurnRequest(
                    task,
                    session_conversation,
                ),
            )
        finally:
            self._release_agent_slot()
        await self._safe_send_text(
            f"🚀 [{new_task.session_id}] 启动 {agent_label} 处理项目 "
            f"{project_name}…\n任务: {task}",
            conversation=session_conversation,
        )

    async def _open_project_manager_for_root(
        self,
        arg: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """解析 ``/manager <项目名>``，创建并绑定项目 Manager 话题。"""
        usage = "格式：`/manager <项目名>`"
        project_name = arg.strip()
        if not project_name:
            await self._send_user(usage, conversation=conversation)
            return
        project = self._resolve_project(project_name)
        if project is None:
            known = ", ".join(self._all_projects()) or "(无)"
            await self._send_user(
                f"未知项目 '{project_name}'。已知项目: {known}",
                conversation=conversation,
            )
            return

        manager_session_id = self._project_manager_session_id(project.name)
        header = self.session_conversation_header(manager_session_id)
        channel = self._channel_for(conversation)
        try:
            manager_conversation = await asyncio.to_thread(
                channel.create_thread,
                header,
            )
            self.open_project_manager(project.name, manager_conversation)
        except Exception as exc:
            logger.exception(
                "创建项目 Manager Conversation 失败 project=%s",
                project.name,
            )
            await self._send_user(
                f"⚠️ 创建项目 {project.name} 的 Manager 话题失败：{str(exc)[:200]}",
                conversation=conversation,
            )
            return
        await self._send_user(
            f"✅ 已创建项目 {project.name} 的 Manager 话题，请在新话题中继续对话。",
            conversation=conversation,
        )

    async def _attach_for_root(
        self,
        msg: ChannelMessage,
        arg: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """解析 ``/attach <项目> <agent> <session_id> [描述...]`` 并附着外部会话。

        参数解析后交给共用底层 :meth:`_attach_task`；成功不额外回复（worker 会发
        附着摘要），失败则回复当前 Conversation。
        """
        usage = "格式：`/attach <项目名> <agent> <session_id> [描述...]`"
        parts = arg.split(maxsplit=3)
        if len(parts) < 3:
            await self._send_user(usage, conversation=conversation)
            return
        project_name = parts[0].strip()
        agent_in = parts[1].strip()
        session_id = parts[2].strip()
        user_desc = parts[3].strip() if len(parts) > 3 else ""
        if not project_name or not agent_in or not session_id:
            await self._send_user(usage, conversation=conversation)
            return
        task, message = await self._attach_task(
            project_name,
            agent_in,
            session_id,
            user_desc,
            conversation=conversation,
        )
        if task is not None:
            return  # 成功：新话题的附着摘要由 worker 发
        await self._send_user(message, conversation=conversation)

    async def _attach_task(
        self,
        project_name: str,
        agent: str,
        session_id: str,
        description: str = "",
        *,
        conversation: ConversationRef,
    ) -> tuple["Session | None", str]:
        """附着外部会话为新 Task 的共用底层（``/attach`` 与 ``attach_session`` 工具都调它）。

        流程：校验→去重→先 load_session 探测→建 Task + 在来源 Channel 新建话题→
        ``_launch(resume)``
        （附着摘要由 worker 就绪后发）。``agent`` 非空则覆盖项目 default_agent（须在
        ``[agents]``），空则用项目默认——``attach_session`` 的 agent 可选正依赖此语义。

        返回 ``(task, message)``：
        - ``task`` 非 None = 成功建 Task 并拉起（message 为成功摘要）；
        - ``task`` 为 None = 未建 Conversation 的失败（校验/去重/探测/准入）。

        无锁 MVP：不探测原 CLI 是否已退出——假定原会话已停止；会话交接锁机制另行立项。
        单次附着约 2× load_session 成本（先探测一次、拉起再恢复一次）——慢后端
        （如 Claude 冷启动 ~15–18s）耗时约为两次冷启动，属预期。
        """
        project = self._resolve_project(project_name)
        if project is None:
            known = ", ".join(self._all_projects()) or "(无)"
            return None, f"未知项目 '{project_name}'。已知项目: {known}"
        agent_label, agent_argv, err = self._resolve_agent(project, agent)
        if agent_argv is None:
            return None, err

        # 重复附着：同 (agent, session_id) 已有 Task → 拒绝并引导到已有 task。
        existing = self.store.by_agent_session(agent_label, session_id)
        if existing is not None:
            return (
                None,
                (
                    f"⚠️ 该会话已由任务 [{existing.session_id}] 附着（agent={agent_label}）。"
                    "请回到其话题继续，勿重复附着。"
                ),
            )

        # 先探测：同步 load_session 确认该 backend + cwd 能恢复此 session；失败不落 Task。
        ok, why = await self._probe_attach(
            agent_label, agent_argv, str(project.path), session_id
        )
        if not ok:
            return None, why

        if not self._reserve_agent_slot():
            return (
                None,
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，请先 `/stop` 一个。",
            )
        try:
            sid = _short_sid(session_id)
            header = f"🔗 {agent_label} · {project_name}\n附着外部会话: {sid}"
            if description:
                header += f"\n说明: {description}"
            channel = self._channel_for(conversation)
            session_conversation = await asyncio.to_thread(
                channel.create_thread, header
            )
            task_desc = f"附着外部会话 {agent_label}/{sid}"
            if description:
                task_desc += f" — {description}"
            new_task = self.store.create(
                project_name=project_name,
                agent_label=agent_label,
                description=task_desc,
                channel_key=session_conversation.channel_key(),
                conversation_payload=self._serialize_conversation_ref(
                    session_conversation
                ),
                workspace=str(project.path),
                agent_session_id=session_id,
                origin="attach",
            )
            self._launch(
                new_task,
                agent_argv,
                first_turn=None,
                resume_session_id=session_id,
                attached=True,
            )
        finally:
            self._release_agent_slot()
        return (
            new_task,
            f"已附着外部会话 {agent_label}/{sid} 为任务 [{new_task.session_id}]。",
        )

    async def _probe_attach(
        self, agent_label: str, agent_argv: list[str], cwd: str, session_id: str
    ) -> tuple[bool, str]:
        """同步 load_session 探测：确认外部 session 可恢复才允许建 Task。

        起一个一次性 ``AcpAgent(resume_session_id=...)`` 走 load_session（成功即证明该
        backend + cwd 能恢复此 session），随后 aclose 探针（含 Windows 进程树清理）。
        探测失败返回 (False, 人读原因)，调用方据此拒绝、不落任何 Task。capability 判定 =
        尝试失败即报错，不做静态标志/动态探测。
        """

        async def _noop_out(_output: AgentOutputChunk) -> None:
            pass

        async def _noop_act(_a: dict) -> None:
            pass

        env = dict(self.cfg.agent_env.get(agent_label, {}))
        agent = self._make_agent(
            AgentSpawn(command=list(agent_argv), cwd=cwd, env=env),
            _noop_out,
            _noop_act,
            resume_session_id=session_id,
        )
        try:
            await agent.start()
        except Exception as exc:  # noqa: BLE001 —— 探测失败归因尽力而为
            # session_id 手滑是正常失败：只记人读摘要，不刷 traceback。
            logger.info(
                "attach 探测失败 agent=%s：%s",
                agent_label,
                _one_line(str(exc), 160),
            )
            return False, _attach_probe_error(exc)
        finally:
            try:
                await agent.aclose()
            except Exception:
                logger.debug("attach 探针 aclose 异常（忽略）", exc_info=True)
        return True, ""

    def _make_agent(
        self,
        spawn: AgentSpawn,
        on_output: OnOutput,
        on_action: "OnAction | None" = None,
        *,
        on_tool_call: "OnToolCall | None" = None,
        resume_session_id: str | None = None,
    ) -> AcpAgent:
        """构造底层 agent（拆出来是测试注入点）。"""
        return AcpAgent(
            spawn,
            on_output,
            on_tool_call=on_tool_call,
            on_action=on_action,
            resume_session_id=resume_session_id,
            start_timeout=self.cfg.agent_start_timeout,
        )

    def _launch(
        self,
        session: Session,
        agent_argv: list[str],
        first_turn: TurnRequest | None,
        *,
        resume_session_id: str | None = None,
        attached: bool = False,
    ) -> _AgentSessionRunner:
        """按 Session 创建 ``_AgentSessionRunner``、接线输出、入队首个 Turn、启动 worker。

        ``resume_session_id`` 非 None 时 agent 用 load_session 恢复（惰性重连）。
        ``first_turn=None`` 时只把 agent 拉起来在线（不跑首轮），用于 resume_task。
        ``attached=True`` 仅由 ``/attach`` 的**首次**拉起置位——附着摘要文案；该 Session
        事后经 ``_try_resume`` 恢复时仍走普通「已恢复」路径（attached 默认 False）。
        """
        session_conversation = self._conversation_for_session(session)
        self.bind_conversation(session.session_id, session_conversation)
        sess = _AgentSessionRunner(
            project_name=session.project_name,
            agent_label=session.agent_label,
            session_id=session.session_id,
            conversation=session_conversation,
            cwd=session.workspace,
            resumed=resume_session_id is not None,
            attached=attached,
            issue_url=session.issue_url,
        )

        async def on_output(output: AgentOutputChunk) -> None:
            if not self._runners.is_current(sess.session_id, sess):
                return
            if sess.current_output is not None:
                sess.current_output.feed(output.display_text)
            if sess.current_turn_id is None:
                return
            if output.raw_text is None:
                if not output.plan_entries:
                    return
                event = SessionEvent(
                    event_id=secrets.token_hex(16),
                    session_id=sess.session_id,
                    turn_id=sess.current_turn_id,
                    occurred_at=datetime.now(timezone.utc),
                    body=AgentPlanUpdated(
                        entries=tuple(
                            AgentPlanEntry(
                                content=entry.content,
                                status=entry.status,
                            )
                            for entry in output.plan_entries
                        )
                    ),
                )
                record = await self._emit_session_event(event)
                self._queue_session_event_projection(
                    sess,
                    event,
                    record.sequence if record is not None else None,
                )
                return
            if output.kind == "message":
                sess.current_message_chunks.append(output.raw_text)
                stream = "message"
            elif output.kind == "thought":
                sess.current_thought_chunks.append(output.raw_text)
                stream = "thought"
            else:
                return
            event = SessionEvent(
                event_id=secrets.token_hex(16),
                session_id=sess.session_id,
                turn_id=sess.current_turn_id,
                occurred_at=datetime.now(timezone.utc),
                body=AgentOutputDelta(stream=stream, text=output.raw_text),
            )
            record = await self._emit_session_event(event)
            self._queue_session_event_projection(
                sess,
                event,
                record.sequence if record is not None else None,
            )

        async def on_action(action: dict) -> None:
            # 审计（A）：只有 current runner 能把 tool_call 记进 Task；旧代 runner
            # 的迟到 callback 仍可收尾自身资源，但不能再代表 Task 写当前运行态。
            if not self._runners.is_current(sess.session_id, sess):
                return
            cur = self.store.get(sess.session_id)
            turn = (cur.turns if cur else 0) + 1
            self.store.add_action(sess.session_id, {"turn": turn, **action})

        async def on_tool_call(update: AgentToolCallUpdate) -> None:
            if (
                not self._runners.is_current(sess.session_id, sess)
                or sess.current_turn_id is None
            ):
                return
            event = SessionEvent(
                event_id=secrets.token_hex(16),
                session_id=sess.session_id,
                turn_id=sess.current_turn_id,
                occurred_at=datetime.now(timezone.utc),
                body=ToolCallObserved(
                    tool_call_id=update.tool_call_id,
                    kind=update.kind,
                    title=update.title,
                    status=update.status,
                    detail=update.detail,
                ),
            )
            record = await self._emit_session_event(event)
            self._queue_session_event_projection(
                sess,
                event,
                record.sequence if record is not None else None,
            )

        # 配置里给该后端声明的追加 env（[agents.<名>].env，如 codex 的 CODEX_PATH）打底。
        env: dict[str, str] = dict(self.cfg.agent_env.get(session.agent_label, {}))
        # 身份注入（#68）：给 agent 子进程一份一次性 token + 控制面 URL（经 env 逐层
        # 透传到 agent 跑的 shell → fdx）。有控制面才注入（测试无控制面时不注入）。
        if self._control is not None:
            token = secrets.token_urlsafe(16)
            self._bg_tokens[token] = session.session_id
            sess.bg_token = token
            env.update(
                {
                    "FEISHU_DISPATCHER_URL": self._control.base_url,
                    "FEISHU_DISPATCHER_TOKEN": token,
                    "FEISHU_DISPATCHER_TASK_ID": session.session_id,
                }
            )
        sess.agent = self._make_agent(
            AgentSpawn(command=list(agent_argv), cwd=session.workspace, env=env),
            on_output,
            on_action,
            on_tool_call=on_tool_call,
            resume_session_id=resume_session_id,
        )
        if first_turn is not None:
            sess.enqueue(first_turn)
        self._runners.register(session.session_id, sess)
        sess.worker = asyncio.create_task(
            self._agent_worker(sess, first_turn), name=f"agent-{session.session_id}"
        )
        return sess

    async def _agent_worker(
        self, sess: _AgentSessionRunner, startup_turn: TurnRequest | None
    ) -> None:
        """一个 agent 的完整生命周期：启动 → 串行消费 Turn 队列 → 关闭。"""
        agent = sess.agent
        assert agent is not None
        startup_conversation = (
            startup_turn.conversation if startup_turn is not None else sess.conversation
        )
        try:
            await agent.start()
        except Exception as exc:
            logger.exception("agent 启动失败")
            if self._runners.is_current(sess.session_id, sess):
                err = _clip(f"{type(exc).__name__}: {exc}", _ERROR_MSG_MAX)
                self.store.update(sess.session_id, status="failed", error_message=err)
                if sess.attached:
                    message = (
                        "❌ 附着失败（session 无法恢复或已过期）。"
                        "请确认后重试，或发送 `/run` 新开。"
                    )
                elif sess.resumed:
                    message = (
                        "❌ 会话恢复失败（可能已在 agent 侧过期）。发送 `/run` 重开。"
                    )
                else:
                    message = f"❌ agent 启动失败: {str(exc)[:200]}"
                await self._send_to_session(
                    sess.session_id,
                    message,
                    source=startup_conversation,
                )
                if startup_turn is not None:
                    delegation = self.delegation_store.by_worker_turn(
                        sess.session_id,
                        startup_turn.turn_id,
                    )
                    if delegation is not None:
                        await self._finish_delegation_turn(
                            delegation,
                            outcome="failed",
                            fallback_message=message,
                        )
            await self._close_session(sess)
            return
        if not self._runners.is_current(sess.session_id, sess):
            await self._close_session(sess)
            return
        # 启动成功：把 agent_session_id + 模型落进 Task 并置 idle（供重启后恢复）
        reported = getattr(agent, "model", "") or ""
        model = reported
        # 模型黏住（恢复后）：agent 后端重载会话（load_session）时可能把模型重置回默认，
        # 报回的 current_value 即是默认——若直接采信就会把用户此前 /model 切过的模型覆盖掉
        # （台账 + 实际都还原）。故：Session 若记着用户切过的模型且后端仍支持，就重新下发一次，
        # 保证「切模型 → 挂起 → 恢复」后仍用用户选的模型。后端已持久化（reported==pinned）时跳过。
        task = self.store.get(sess.session_id)
        pinned = (task.model if task else "") or ""
        available = getattr(agent, "available_models", None) or []
        # 被动刷新模型缓存：真实 agent 一启动就把它报的 available_models 存下来，
        # 供 spawn 前 /models、list_models 列出/校验（copilot 报空也如实存）。
        self.model_store.update(sess.agent_label, list(available))
        if pinned and pinned != reported and pinned in available:
            try:
                await agent.set_model(pinned)
                model = pinned
                logger.info("恢复后重新应用模型 task=%s → %s", sess.session_id, pinned)
            except Exception:
                logger.exception(
                    "恢复后重新应用模型失败 task=%s → %s", sess.session_id, pinned
                )
                model = reported  # 应用失败：如实保留后端报回的模型，不谎报
        elif pinned and pinned != reported and pinned not in available:
            logger.warning(
                "恢复后无法保持模型 task=%s：后端已不提供 %s（回退 %s）",
                sess.session_id,
                pinned,
                reported or "默认",
            )
        if not self._runners.is_current(sess.session_id, sess):
            await self._close_session(sess)
            return
        self.store.update(
            sess.session_id,
            agent_session_id=agent.session_id or "",
            status="idle",
            model=model,
        )
        if sess.attached:
            # 附着摘要（区别于普通「已就绪」/「已恢复」文案）：说明来源 + 后续回复续接上下文
            sid = _short_sid(agent.session_id or "")
            model_tail = f"，模型：{model}" if model else ""
            base = (
                f"🔗 已附着外部会话（agent={sess.agent_label}，session={sid}{model_tail}）。\n"
                "后续回复将继续原上下文；可 `/stop` 结束、`/done` 归档。"
            )
        elif sess.resumed:
            base = "♻️ 已恢复会话，继续执行…"
            if model:
                base += f"（模型：{model}）"
        else:
            base = "▶️ agent 已就绪，开始执行…"
            if model:
                base += f"（模型：{model}）"
        await self._send_to_session(
            sess.session_id,
            base,
            source=startup_conversation,
        )
        try:
            while True:
                # 空闲挂起（坑 1）：超时无新回复就关掉 agent 腾出 max_agents 名额，
                # 但**保留** sessions.json 记录（区别于 /stop 的删除）——之后在本
                # 话题回复即走 load_session 恢复。<=0 表示不自动挂起。
                timeout = self.cfg.idle_timeout if self.cfg.idle_timeout > 0 else None
                try:
                    queued = await asyncio.wait_for(sess.queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if not self._runners.is_current(sess.session_id, sess):
                        break
                    self.store.update(sess.session_id, status="suspended")
                    await self._send_to_session(
                        sess.session_id,
                        "💤 空闲超时，已挂起该 agent（在本话题回复即自动恢复）。",
                        source=sess.conversation,
                    )
                    if self._runners.is_current(sess.session_id, sess):
                        await self._notify_main(
                            f"💤 {sess.project_name} 已空闲挂起（在其话题回复即自动恢复）。"
                        )
                    break
                if not self._runners.is_current(sess.session_id, sess):
                    break
                if queued is None:
                    status = sess.terminate_status  # stopped(/stop) 或 done(/done)
                    self.store.update(sess.session_id, status=status)  # 保留历史
                    await self._send_to_session(
                        sess.session_id,
                        "✅ 任务已完成并归档。"
                        if status == "done"
                        else "🛑 agent 已停止。",
                        source=sess.conversation,
                    )
                    break
                async with self._session_turn_lock(sess.session_id):
                    if not self._runners.is_current(sess.session_id, sess):
                        break
                    if isinstance(queued, _BgBatch):
                        # 后台完成批次（#79）：清 pending_bg（队尾不再有可合并批次），
                        # 渲染成本轮 prompt（可能含多个 job 块）。清空须紧接 get、无 await。
                        sess.pending_bg = None
                        request = TurnRequest(queued.render(), sess.conversation)
                        mirror_input = False
                    else:
                        request = queued
                        mirror_input = True
                    prompt = request.text
                    turn_conversation = request.conversation
                    turn_conversations = self._conversations_for_session(
                        sess.session_id,
                        source=turn_conversation,
                    )
                    if mirror_input:
                        event = SessionEvent(
                            event_id=secrets.token_hex(16),
                            session_id=sess.session_id,
                            turn_id=request.turn_id,
                            occurred_at=datetime.now(timezone.utc),
                            body=SessionInputAccepted(
                                text=request.text,
                                source=request.conversation,
                            ),
                        )
                        record = await self._emit_session_event(event)
                        await self._publish_session_event(
                            event,
                            tuple(
                                conversation
                                for conversation in turn_conversations
                                if conversation != request.conversation
                            ),
                            trace_sequence=(
                                record.sequence if record is not None else None
                            ),
                        )
                    title = f"{sess.project_name} · {sess.agent_label}"
                    model = getattr(agent, "model", "") or ""
                    # footer 与模型同一行显示项目名（#44）：在任意输出单元都可辨归属
                    footer = sess.project_name
                    if model:
                        footer += f" · 模型：{model}"
                    issue_tag = _issue_tag(
                        sess.issue_url
                    )  # 绑定了 issue 则标 · #N（#63）
                    if issue_tag:
                        footer += f" · {issue_tag}"
                    output = self._open_session_output(
                        turn_conversations,
                        title,
                        footer=footer,
                    )
                    sess.current_output = output
                    sess.current_turn_id = request.turn_id
                    sess.current_conversations = turn_conversations
                    sess.current_message_chunks.clear()
                    sess.current_thought_chunks.clear()
                    self.store.update(sess.session_id, status="running")
                    logger.info(
                        "任务 %s 开始一轮（%s）: %.80s",
                        sess.session_id,
                        sess.agent_label,
                        prompt,
                    )
                    sess.turn_in_flight = True
                    event = SessionEvent(
                        event_id=secrets.token_hex(16),
                        session_id=sess.session_id,
                        turn_id=request.turn_id,
                        occurred_at=datetime.now(timezone.utc),
                        body=AgentOutputStarted(),
                    )
                    record = await self._emit_session_event(event)
                    self._queue_session_event_projection(
                        sess,
                        event,
                        record.sequence if record is not None else None,
                    )
                    outcome: OutputOutcome | None = None
                    try:
                        stop_reason = await agent.prompt(prompt)
                        await output.flush()
                        if not self._runners.is_current(sess.session_id, sess):
                            break
                        if stop_reason == "cancelled":
                            # 本轮被 /stop 中途取消：不当作正常完成（不 ✅、不计 turn、
                            # 不发完成通知）。输出置停止态；随后循环取到 None 哨兵即终止。
                            await output.set_status("stopped")
                            if not self._runners.is_current(sess.session_id, sess):
                                break
                            self.store.update(sess.session_id, status="idle")
                            logger.info("任务 %s 本轮被取消", sess.session_id)
                            outcome = "cancelled"
                            continue
                        # footer 追加本轮 token 用量（#53）：取不到就不显示、不报错。
                        # 只标脏，紧随的 set_status("done") 会把新 footer 一起 emit。
                        tokens = getattr(agent, "last_usage_tokens", None)
                        if tokens is not None:
                            output.set_footer(_with_tokens(footer, tokens))
                        await output.set_status("done")
                        if not self._runners.is_current(sess.session_id, sess):
                            break
                        # 落 last_output：本轮 agent 的收尾回复（截断），供 get_task/通知摘要
                        last_output = _clip(agent.last_message, _LAST_OUTPUT_MAX)
                        cur = self.store.get(sess.session_id)
                        turns = (cur.turns if cur else 0) + 1
                        logger.info(
                            "任务 %s 完成第 %d 轮，回复 %d 字",
                            sess.session_id,
                            turns,
                            len(last_output),
                        )
                        self.store.update(
                            sess.session_id,
                            status="idle",
                            turns=turns,
                            last_output=last_output,
                            error_message="",  # 一轮成功即清掉上次异常诊断（恢复成功）
                        )
                        await self._send_to_conversations(
                            turn_conversations,
                            "✅ 本轮结束（可继续回复；发送 `/stop` 结束该 agent）",
                        )
                        # 完成且已闲下来（无排队）→ 推一条主线通知（带收尾摘要），免得挨个点话题
                        if (
                            self._runners.is_current(sess.session_id, sess)
                            and sess.queue.empty()
                        ):
                            note = f"🔔 {sess.project_name} 完成第 {turns} 轮"
                            snippet = _one_line(last_output, 80)
                            if snippet:
                                note += f"：{snippet}"
                            note += "，在其话题里查看/继续。"
                            await self._notify_main(note)
                        outcome = "completed"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("agent 执行异常")
                        err = _clip(f"{type(exc).__name__}: {exc}", _ERROR_MSG_MAX)
                        outcome = "failed"
                        try:
                            await output.set_status("error")
                        except Exception:
                            logger.debug("set_status error 失败（忽略）", exc_info=True)
                        if self._runners.is_current(sess.session_id, sess):
                            # failed 不再是终止态：本轮失败但 session 已建，多半能 load_session
                            # 接回——标 failed（可恢复），话题回复即尝试恢复，而非逼用户重开丢上下文。
                            self.store.update(
                                sess.session_id, status="failed", error_message=err
                            )
                            await self._send_to_conversations(
                                turn_conversations,
                                f"❌ 本轮异常，已暂停：{err}\n"
                                "在话题回复即尝试恢复（load_session 接回上下文），或 `/stop` 结束。",
                            )
                            await self._notify_main(
                                f"❌ {sess.project_name} 本轮异常，已暂停（在其话题回复即尝试恢复）。"
                            )
                        break
                    finally:
                        sess.turn_in_flight = False
                        if outcome is not None:
                            await self._finish_agent_output(sess, outcome)
                        projection_tail = sess.session_event_projection_tail
                        if projection_tail is not None:
                            await projection_tail
                            sess.session_event_projection_tail = None
                        await output.aclose()
                        sess.current_output = None
                        sess.current_turn_id = None
                        sess.current_conversations = ()
                        sess.current_message_chunks.clear()
                        sess.current_thought_chunks.clear()
        except asyncio.CancelledError:
            logger.debug("agent worker 被取消 session=%s", sess.session_id)
        finally:
            await self._close_session(sess)

    async def _close_session(self, sess: _AgentSessionRunner) -> None:
        """收尾 runner：仅按 identity 移除自身槽位，但始终关闭自身资源。"""
        self._runners.remove_if_current(sess.session_id, sess)
        if sess.bg_token:  # 作废该 Session 的 agent 控制面 token（#68）
            self._bg_tokens.pop(sess.bg_token, None)
            sess.bg_token = ""
        output = sess.current_output
        sess.current_output = None
        if output is not None:
            try:
                await output.aclose()
            except Exception:
                logger.debug("output aclose 异常（忽略）", exc_info=True)
        agent = sess.agent
        sess.agent = None
        if agent is not None:
            try:
                await agent.aclose()
            except Exception:
                logger.debug("agent aclose 异常（忽略）", exc_info=True)

    async def _cancel_turn(self, sess: _AgentSessionRunner) -> None:
        """协作式取消 session 当前在途的 turn（ACP session/cancel）。失败不致命。"""
        agent = sess.agent
        if agent is None:
            return
        try:
            await agent.cancel()
            logger.info("已请求取消任务 %s 的当前轮", sess.session_id)
        except Exception:
            logger.exception("取消当前轮失败 task=%s", sess.session_id)

    async def _forward_to_agent(
        self, msg: ChannelMessage, *, conversation: ConversationRef
    ) -> None:
        """话题内回复 → 入队给对应 agent；agent 不在则尝试跨重启恢复。"""
        text = msg.text.strip()
        # /help 先于 session 检查：不依赖 agent 是否在线（挂起的话题里也能查用法），
        # 且绝不入队 / 触发恢复。
        if text in _HELP_CMDS:
            await self._safe_send_text(_THREAD_USAGE, conversation=conversation)
            return
        # /raw <文本>：把 <文本> 逐字转发给 agent，绕过下面所有话题命令（/stop、/model…）
        # 的解释——用来给 coding agent 发它自己的、恰好与保留名撞车的 slash 指令。剥掉
        # 前缀后走与普通消息完全相同的路径（含 session 恢复），只是不再匹配保留命令。
        forward_raw = False
        if text == _RAW_CMD or text.startswith(_RAW_CMD + " "):
            text = text[len(_RAW_CMD) :].strip()
            if not text:
                await self._safe_send_text(
                    "用法：`/raw <指令>` —— 把 <指令> 原样发给 agent（如 `/raw /model`）。",
                    conversation=conversation,
                )
                return
            forward_raw = True
        session = self._session_for_conversation(conversation)
        if session is None:
            session = self._stored_session_for_conversation(conversation)
        if session is not None:
            self.bind_conversation(session.session_id, conversation)
        sess = (
            self._runners.get_for_session(session.session_id)
            if session is not None
            else None
        )
        if sess is None:
            # Conversation 只负责路由到 Session；无 current runner 时再尝试恢复或明确提示。
            await self._recover_or_notify(
                text,
                conversation=conversation,
                task=session,
                forward_raw=forward_raw,
            )
            return
        if not text:
            return
        if sess.worker is None or sess.worker.done():
            await self._send_to_session(
                sess.session_id,
                "⚠️ 该 agent 已结束。发送 `/run ...` 新建任务。",
                source=conversation,
            )
            return
        if forward_raw:
            sess.enqueue(TurnRequest(text, conversation))  # 逐字直传，跳过保留命令解释
            return
        if text == _STOP_CMD:
            # 终止信号：丢弃未处理 bg 批次 + 入队 None（#79 立即停、不排空后台结果）。
            sess.terminate()
            # 有在途 turn 时协作式取消它，否则 None 要等整轮跑完才生效（跑偏时干瞪眼）。
            # terminate() 在 cancel 之前：cancel 让在途 prompt() 返回后，队列里已有 None。
            if sess.turn_in_flight:
                await self._cancel_turn(sess)
            return
        if text == _CANCEL_CMD or text.startswith(_CANCEL_CMD + " "):
            # /cancel = 停当前轮但**保留 agent**（区别于 /stop 的结束）；
            # /cancel <新输入> = 停当前轮 + 把新输入作为下一轮排队（FIFO）。
            new_input = text[len(_CANCEL_CMD) :].strip()
            if sess.turn_in_flight:
                if new_input:
                    # 排在 cancel 之前：取消让在途 prompt() 返回后，队列里已有新输入 →
                    # worker 的 cancelled 分支 continue 后即取到它，作为新一轮跑。
                    sess.enqueue(TurnRequest(new_input, conversation))
                await self._cancel_turn(sess)
                await self._send_to_session(
                    sess.session_id,
                    "🛑 已取消当前轮，改执行新指令…"
                    if new_input
                    else "🛑 已取消当前轮（agent 保留，可继续发指令）。",
                    source=conversation,
                )
            elif new_input:
                # 无在途轮：没什么可取消，新输入当普通消息执行
                sess.enqueue(TurnRequest(new_input, conversation))
            else:
                await self._safe_send_text(
                    "当前没有在跑的轮，无需取消。",
                    conversation=conversation,
                )
            return
        if text == _DONE_CMD:
            self._finish_task(sess.session_id, "done")  # 优雅收尾，worker 发完成消息
            return
        if text == _MODEL_CMD or text.startswith(_MODEL_CMD + " "):
            await self._handle_model_cmd(sess, text, conversation=conversation)
            return
        sess.enqueue(TurnRequest(text, conversation))

    async def _handle_model_cmd(
        self,
        sess: _AgentSessionRunner,
        text: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """`/model` 列出当前+可选模型；`/model <名>` 切换（ACP set_config_option）。

        对下一轮生效。agent 不暴露模型选项（如 copilot）则提示不支持。
        """
        agent = sess.agent
        if agent is None:
            await self._safe_send_text(
                "⚠️ agent 尚未就绪，无法切换模型。",
                conversation=conversation,
            )
            return
        models = list(getattr(agent, "available_models", []) or [])
        current = getattr(agent, "model", "") or ""
        if not models:
            await self._safe_send_text(
                "⚠️ 该 agent 不支持切换模型（未通过 ACP 暴露模型选项）。",
                conversation=conversation,
            )
            return
        arg = text[len(_MODEL_CMD) :].strip()
        if not arg:  # 裸 /model → 列出
            lines = [
                f"当前模型：{current or '未知'}",
                "可切换（发 `/model <完整名>`）：",
            ]
            lines += [f"• {m}" for m in models]
            await self._safe_send_text("\n".join(lines), conversation=conversation)
            return
        if arg not in models:
            await self._safe_send_text(
                f"⚠️ 未知模型 '{arg}'。发 `/model` 查看可选列表。",
                conversation=conversation,
            )
            return
        try:
            await agent.set_model(arg)
        except Exception as exc:
            logger.exception("切换模型失败 task=%s model=%s", sess.session_id, arg)
            if self._runners.is_current(sess.session_id, sess):
                await self._safe_send_text(
                    f"❌ 切换模型失败：{str(exc)[:200]}",
                    conversation=conversation,
                )
            return
        if not self._runners.is_current(sess.session_id, sess):
            return
        self.store.update(sess.session_id, model=arg)
        logger.info("任务 %s 切换模型 → %s", sess.session_id, arg)
        await self._send_to_session(
            sess.session_id,
            f"✅ 已切换模型为 {arg}（下一轮起生效）。",
            source=conversation,
        )

    async def _recover_or_notify(
        self,
        text: str,
        *,
        conversation: ConversationRef,
        task: Session | None,
        forward_raw: bool = False,
    ) -> None:
        """话题无活跃 agent：能恢复的 Task 就 load_session 惰性重连，否则明确提示。

        ``forward_raw``（来自 ``/raw <文本>``）时跳过 ``/stop``/``/done`` 解释——恢复
        agent 后把 <文本> 当普通首轮转发，即使它恰好是 ``/stop`` 也不误当停止命令。
        """
        if task is None:
            await self._safe_send_text(
                "⚠️ 该话题没有对应任务（可能从未启动）。发送 `/run` 新建任务。",
                conversation=conversation,
            )
            return
        if task.is_terminal:
            await self._safe_send_text(
                f"⚠️ 任务 [{task.session_id}] 已结束（{task.status}）。发送 `/run` 新开一个。",
                conversation=conversation,
            )
            return
        if not forward_raw and text == _STOP_CMD:
            self.store.update(task.session_id, status="stopped")
            await self._send_to_session(
                task.session_id,
                f"🛑 任务 [{task.session_id}] 已结束。",
                source=conversation,
            )
            return
        if not forward_raw and text == _DONE_CMD:
            self.store.update(task.session_id, status="done")
            await self._send_to_session(
                task.session_id,
                f"✅ 任务 [{task.session_id}] 已完成并归档。",
                source=conversation,
            )
            return
        if not text:
            return  # 空回复不触发恢复
        ok, why = self._try_resume(
            task,
            first_turn=TurnRequest(text, conversation),
        )
        if not ok:
            await self._safe_send_text(why, conversation=conversation)
            return
        await self._send_to_session(
            task.session_id,
            f"♻️ 正在恢复任务 [{task.session_id}]…",
            source=conversation,
        )

    def _try_resume(
        self, task: Session, *, first_turn: TurnRequest | None
    ) -> tuple[bool, str]:
        """把一个非活跃任务 load_session 惰性重连；返回 (成功, 失败文案)。

        用统一的预留计数覆盖 ``create_thread`` 等其它入口的 await 窗口，保证并发下
        不突破 max_agents。
        """
        if self._runners.get_for_session(task.session_id) is not None:
            return False, f"任务 [{task.session_id}] 已在运行，无需恢复。"
        agent_argv = self.cfg.agents.get(task.agent_label)
        if not agent_argv or not task.agent_session_id:
            self.store.update(task.session_id, status="failed")
            why = "agent 未配置" if not agent_argv else "无可恢复的会话"
            return False, (
                f"⚠️ 无法恢复任务 [{task.session_id}]（{why}）。发送 `/run` 重开。"
            )
        if not self._reserve_agent_slot():
            return False, (
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，无法恢复。"
                "请先 `/stop` 一个再试。"
            )
        try:
            self._launch(
                task,
                agent_argv,
                first_turn=first_turn,
                resume_session_id=task.agent_session_id,
            )
        finally:
            self._release_agent_slot()
        return True, ""

    def _finish_task(self, task_id: str, status: str) -> bool:
        """把任务置为终止态 ``status``；有活跃 worker 则经哨兵优雅收尾，否则直接改台账。

        返回是否找到该任务。活跃时把 ``terminate_status`` 交给 worker、入队 None——
        worker 跑完当前/排队 turn 后落地状态并发完成消息（与 /stop 同机制）。
        """
        task = self.store.get(task_id)
        if task is None:
            return False
        sess = self._runners.get_for_session(task.session_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            sess.terminate_status = status
            sess.terminate()  # 丢弃未处理 bg 批次 + 入队 None（#79，与 /stop 同机制）
        else:
            self.store.update(task_id, status=status)
        return True

    async def _list_agents(
        self, msg: ChannelMessage, *, conversation: ConversationRef
    ) -> None:
        tasks = self.store.all()
        # failed 虽算 is_active（可恢复），但单拉一段标注，别和在跑的混
        paused = [t for t in tasks if t.status == "failed"]
        active = [t for t in tasks if t.is_active and t.status != "failed"]
        terminal = [t for t in tasks if t.is_terminal]
        parts: list[str] = []
        if active:
            parts.append(
                "活跃任务:\n"
                + "\n".join(
                    f"• [{t.session_id}] {t.project_name} · {t.status}"
                    f"（{t.turns} 轮）：{t.description[:24]}"
                    for t in active
                )
            )
        if paused:
            parts.append(
                "⚠️ 异常暂停（在话题回复即尝试恢复，或 `/stop` 结束）:\n"
                + "\n".join(
                    f"• [{t.session_id}] {t.project_name}：{t.error_message or '本轮异常'}"
                    for t in paused
                )
            )
        if terminal:
            parts.append(
                "历史（近 5）:\n"
                + "\n".join(
                    f"• [{t.session_id}] {t.project_name} · {t.status}：{t.description[:24]}"
                    for t in terminal[-5:]
                )
            )
        await self._send_user(
            "\n\n".join(parts) if parts else "当前无任务。",
            conversation=conversation,
        )

    async def _show_task(
        self,
        msg: ChannelMessage,
        task_id: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """`/task <id>`：任务详情 + 最近动作日志（审计 A 的人读入口，无需 LLM）。"""
        t = self.store.get(task_id)
        if t is None:
            await self._send_user(
                f"未找到任务 {task_id}。用 `/agents` 查看有哪些任务。",
                conversation=conversation,
            )
            return
        head = (
            f"[{t.session_id}] {t.project_name} · {t.agent_label} · {t.status}"
            f"（{t.turns} 轮）"
        )
        if t.model:
            head += f"\n模型: {t.model}"
        lines = [head, f"任务: {t.description}"]
        if t.origin == "attach":
            lines.append(
                f"来源: 附着外部会话（session: {_short_sid(t.agent_session_id)}）"
            )
        if t.issue_url:
            lines.append(f"issue: {t.issue_url}")
        if t.status == "failed" and t.error_message:
            lines.append(f"⚠️ 异常暂停：{t.error_message}（话题回复即尝试恢复）")
        if t.last_output:
            lines.append(f"最近回复: {t.last_output}")
        if t.actions:
            recent = t.actions[-15:]
            lines.append(f"最近动作（共 {len(t.actions)} 条，显示末 {len(recent)}）:")
            lines += [
                f"  • 第{a.get('turn', '?')}轮 · {a.get('kind') or '动作'}："
                f"{a.get('title', '')}"
                for a in recent
            ]
        else:
            lines.append("（暂无动作记录）")
        await self._send_user("\n".join(lines), conversation=conversation)

    async def _reboot(
        self, msg: ChannelMessage, *, conversation: ConversationRef
    ) -> None:
        """`/reboot`：优雅关停后由 cli.py re-exec 重启整个 daemon 进程。

        先发回执再置位（之后 WS 会断）；活跃任务由 `_shutdown` 标 suspended、
        重启后可 `load_session` 恢复，不丢上下文。"""
        await self._send_user(
            "🔄 正在重启 daemon…（十几秒后回来，任务会自动恢复）",
            conversation=conversation,
        )
        logger.info("收到 /reboot，准备重启 daemon")
        self._reboot_requested = True
        if self._stop_event is not None:
            self._stop_event.set()

    # ------------------------------------------------------------------ #
    # P2：调度器 LLM（自然语言派发）
    # ------------------------------------------------------------------ #

    async def _dispatch_nl(
        self,
        msg: ChannelMessage,
        text: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """自然语言 → 调度器 LLM 理解并调用工具派发（P2）。"""
        assert self._llm is not None
        self.bind_conversation(_DISPATCHER_SESSION_ID, conversation)

        async with self._session_turn_lock(_DISPATCHER_SESSION_ID):
            runtime = self._get_dispatcher_runtime()
            runtime.submit(TurnRequest(text, conversation))

    def _get_dispatcher_runtime(self) -> DispatcherSessionRuntime:
        runtime = self._session_runtimes.get_for_session(_DISPATCHER_SESSION_ID)
        if runtime is not None:
            if not isinstance(runtime, DispatcherSessionRuntime):
                raise RuntimeError("Dispatcher Session Runtime 类型不匹配")
            return runtime
        runtime = DispatcherSessionRuntime(
            session_id=_DISPATCHER_SESSION_ID,
            llm_provider=lambda: self._llm,
            memory=self._sched_memory,
            tools_provider=self._dispatcher_tools_for,
        )
        self._register_session_runtime(runtime)
        return runtime

    def _dispatcher_tools_for(self, conversation: ConversationRef) -> list:
        """按来源 Conversation 构建 Dispatcher 本轮可用工具。"""
        return build_scheduler_tools(
            list_projects=self._sched_list_projects,
            spawn_agent=partial(self._sched_spawn_agent, conversation=conversation),
            list_tasks=self._sched_list_tasks,
            get_task=self._sched_get_task,
            send_to_task=self._sched_send_to_task,
            resume_task=self._sched_resume_task,
            mark_done=self._sched_mark_done,
            register_project=self._sched_register_project,
            unregister_project=self._sched_unregister_project,
            attach_session=partial(
                self._sched_attach_session, conversation=conversation
            ),
            list_forge=self._sched_list_forge,
            get_forge=self._sched_get_forge,
            list_models=self._sched_list_models,
        )

    def _sched_list_projects(self) -> list[dict]:
        return [
            {"name": p.name, "default_agent": p.default_agent}
            for p in self._all_projects().values()
        ]

    async def _sched_register_project(self, name: str, agent: str, path: str) -> str:
        """register_project 工具：对话式注册项目（与 /project add 共用校验）。"""
        _, msg = self._register_project(name, agent, path)
        return msg

    async def _sched_unregister_project(self, name: str) -> str:
        """unregister_project 工具：删除已注册项目（与 /project remove 共用底层）。"""
        return self._remove_project(name)

    async def _sched_list_forge(self, project: str, state: str, limit: int) -> str:
        """list_forge_items 工具：只读列 issue/PR。project 空 = 扇出所有已注册项目。"""
        projects = self._all_projects()
        if project:
            proj = projects.get(project)
            if proj is None:
                return f"未找到项目 {project}。可用 list_projects 查看。"
            targets = [proj]
        else:
            targets = list(projects.values())
        if not targets:
            return "没有已注册的项目。"
        results: list[dict] = []
        skipped: list[str] = []
        for p in targets:
            ref = await forge.resolve_forge(p)
            if ref is None:
                skipped.append(f"{p.name}（无 forge 绑定）")
                continue
            try:
                data = await forge.list_items(ref, state=state, limit=limit)
                results.append({"project": p.name, **data})
            except forge.ForgeError as exc:
                skipped.append(f"{p.name}（{exc}）")
        payload: dict = {"results": results}
        if skipped:
            payload["skipped"] = skipped
        if not results and skipped:
            # 一个都没查成——把原因直接说清楚，别让 LLM 以为「没有 issue」。
            return f"未能获取任何仓库的 issue/PR。跳过：{'；'.join(skipped)}"
        return json.dumps(payload, ensure_ascii=False)

    async def _sched_get_forge(self, project: str, kind: str, number: int) -> str:
        """get_forge_item 工具：只读取单个 issue/PR 详情。"""
        proj = self._resolve_project(project)
        if proj is None:
            return f"未找到项目 {project}。可用 list_projects 查看。"
        ref = await forge.resolve_forge(proj)
        if ref is None:
            return (
                f"项目 {project} 没有可用的 forge 绑定"
                "（未配置 repo，也没探测到 git origin 远端）。"
            )
        try:
            data = await forge.get_item(ref, kind, number)
        except forge.ForgeError as exc:
            return f"获取 {kind} #{number} 失败：{exc}"
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _task_summary(task: Session) -> dict:
        return {
            "task_id": task.session_id,
            "project": task.project_name,
            "agent": task.agent_label,
            "description": task.description,
            "status": task.status,
            "turns": task.turns,
            "issue_url": task.issue_url,
        }

    async def _http_list_tasks(
        self, _context: dict, _request: dict
    ) -> tuple[int, dict]:
        tasks = [
            {
                "task_id": _DISPATCHER_SESSION_ID,
                "kind": "dispatcher",
                "description": "Dispatcher",
                "status": "active",
                "active": True,
            }
        ]
        for task in self.store.all():
            summary = self._task_summary(task)
            summary.update(
                kind="agent",
                issue_url=task.issue_url or None,
                active=self._runners.get_for_session(task.session_id) is not None,
            )
            tasks.append(summary)
        return 200, {"tasks": tasks}

    async def _http_task_events(
        self, _context: dict, request: dict
    ) -> tuple[int, dict]:
        task_id = request["segments"]["task_id"]
        if self.store.get(task_id) is None:
            return 404, {"error": "task_not_found", "task_id": task_id}
        trace_store = self.trace_store
        if trace_store is None:
            return 503, {"error": "trace_unavailable"}

        query = request.get("query") or {}
        raw_before = query.get("before")
        raw_after = query.get("after")
        if raw_before is not None and raw_after is not None:
            return 400, {
                "error": "invalid_request",
                "message": "before 和 after 不能同时指定",
            }

        before = None
        if raw_before is not None:
            try:
                before = int(raw_before)
            except (TypeError, ValueError):
                return 400, {
                    "error": "invalid_cursor",
                    "message": "before 必须是正整数",
                }
            if before <= 0:
                return 400, {
                    "error": "invalid_cursor",
                    "message": "before 必须是正整数",
                }

        after = None
        if raw_after is not None:
            try:
                after = int(raw_after)
            except (TypeError, ValueError):
                return 400, {
                    "error": "invalid_cursor",
                    "message": "after 必须是非负整数",
                }
            if after < 0:
                return 400, {
                    "error": "invalid_cursor",
                    "message": "after 必须是非负整数",
                }

        raw_limit = query.get("limit", "100")
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return 400, {
                "error": "invalid_limit",
                "message": f"limit 必须是 1-{_TRACE_EVENTS_LIMIT_MAX} 的整数",
            }
        if not 1 <= limit <= _TRACE_EVENTS_LIMIT_MAX:
            return 400, {
                "error": "invalid_limit",
                "message": f"limit 必须是 1-{_TRACE_EVENTS_LIMIT_MAX} 的整数",
            }

        try:
            page = await asyncio.to_thread(
                trace_store.read_page,
                task_id,
                before=before,
                after=after,
                limit=limit,
                conversation_ref_deserializer=self._deserialize_conversation_ref,
            )
        except SessionTraceStoreClosed:
            return 503, {"error": "trace_unavailable"}
        return 200, {
            "task_id": task_id,
            "events": [
                {
                    "sequence": record.sequence,
                    "event": session_event_to_dict(
                        record.event,
                        conversation_ref_serializer=self._serialize_conversation_ref,
                    ),
                }
                for record in page.records
            ],
            "oldest_sequence": page.oldest_sequence,
            "latest_sequence": page.latest_sequence,
        }

    def _sched_list_tasks(self) -> list[dict]:
        # 从任务台账读（含历史），而非只看内存里的活跃 session
        return [self._task_summary(task) for task in self.store.all()]

    def _sched_get_task(self, task_id: str) -> dict | None:
        """get_task 工具：单任务详情 + 动作审计（回答「这个 agent 都干了啥」）。"""
        t = self.store.get(task_id)
        if t is None:
            return None
        return {
            "task_id": t.session_id,
            "project": t.project_name,
            "agent": t.agent_label,
            "description": t.description,
            "status": t.status,
            "turns": t.turns,
            "has_session": bool(t.agent_session_id),
            "origin": t.origin,  # 会话来源 spawn/attach
            "active": self._runners.get_for_session(t.session_id) is not None,
            "model": t.model,  # agent 当前模型（copilot 不暴露则为空）
            "issue_url": t.issue_url,  # 关联的 issue（#63）；空 = 未绑定
            "created_at": t.created_at,
            "updated_at": t.updated_at,
            "last_output": t.last_output,  # 最近一轮 agent 的收尾回复
            "error_message": t.error_message,  # failed 时的诊断（供判断重试/新开）
            "action_count": len(t.actions),
            "recent_actions": t.actions[-30:],  # 审计 A：agent 调过的工具
        }

    async def _send_turn_to_session(
        self,
        task_id: str,
        message: str,
        *,
        turn_id: str | None = None,
    ) -> tuple[bool, str]:
        task = self.store.get(task_id)
        if task is None:
            return False, f"未找到任务 {task_id}（用 list_tasks 查看现有任务）。"
        conversation = self._conversation_for_session(task)
        request = TurnRequest(
            message,
            conversation,
            **({"turn_id": turn_id} if turn_id is not None else {}),
        )
        sess = self._runners.get_for_session(task.session_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            sess.enqueue(request)
            logger.info(
                "send_to_task[%s] 入队（活跃 session，队列深度=%d，task.status=%s）",
                task_id,
                sess.queue.qsize(),
                task.status,
            )
            return (
                True,
                f"已把消息转达给任务 [{task_id}]（{task.project_name}），排队执行。",
            )
        if task.is_terminal:
            logger.info(
                "send_to_task[%s] 拒绝：任务已终止 status=%s", task_id, task.status
            )
            return False, (
                f"任务 [{task_id}] 已是终止态（{task.status}），未自动恢复。"
                f"如需继续，请先 resume_task({task_id})。"
            )
        # 非活跃且可恢复：load_session 惰性重连，把消息作为首轮。check→launch 无 await。
        ok, why = self._try_resume(
            task,
            first_turn=request,
        )
        logger.info(
            "send_to_task[%s] 非活跃 status=%s → 恢复%s",
            task_id,
            task.status,
            "成功" if ok else f"失败（{why}）",
        )
        return (
            (True, f"已恢复任务 [{task_id}] 并转达消息。")
            if ok
            else (
                False,
                why,
            )
        )

    async def _sched_send_to_task(self, task_id: str, message: str) -> str:
        """send_to_task 工具：把消息路由给已有任务的 agent（在跑排队；挂起先恢复）。"""
        _ok, result = await self._send_turn_to_session(task_id, message)
        return result

    async def _sched_resume_task(self, task_id: str) -> str:
        """resume_task 工具：显式恢复挂起/已结束的任务（load_session），仅拉起不跑首轮。"""
        task = self.store.get(task_id)
        if task is None:
            return f"未找到任务 {task_id}（用 list_tasks 查看现有任务）。"
        sess = self._runners.get_for_session(task.session_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            return f"任务 [{task_id}] 已在运行，无需恢复。"
        ok, why = self._try_resume(task, first_turn=None)
        if not ok:
            return why
        return (
            f"已恢复任务 [{task_id}]（{task.project_name}），"
            "可继续 send_to_task 或让用户在其话题回复。"
        )

    async def _sched_mark_done(self, task_id: str) -> str:
        """mark_done 工具：把任务标记完成并归档（有活跃 worker 则优雅收尾）。"""
        if not self._finish_task(task_id, "done"):
            return f"未找到任务 {task_id}（用 list_tasks 查看现有任务）。"
        return f"已把任务 [{task_id}] 标记为完成（done）。"

    async def _sched_attach_session(
        self,
        project_name: str,
        session_id: str,
        agent: str = "",
        description: str = "",
        *,
        conversation: ConversationRef,
    ) -> str:
        """attach_session 工具：附着 daemon 外部的 agent 会话为新 Task（与 /attach 共用底层）。

        ``agent`` 可选：非空则覆盖项目 default_agent（须在 [agents]），空则用默认。
        仅新建；重复 (agent, session_id) 由底层 :meth:`_attach_task` 去重拒绝。
        """
        _task, message = await self._attach_task(
            project_name,
            agent,
            session_id,
            description,
            conversation=conversation,
        )
        return message

    async def _sched_spawn_agent(
        self,
        project_name: str,
        task: str,
        agent: str = "",
        issue: int = 0,
        model: str = "",
        *,
        conversation: ConversationRef,
    ) -> str:
        """spawn_agent 工具实现：建 Task + 新话题 + 启动 agent，返回给 LLM 的状态串。

        ``agent`` 可选：非空则覆盖项目 default_agent（须在 [agents]），否则用默认。
        ``issue`` 可选（>0）：把该 issue 的完整正文当 brief 派给 agent，并把 issue_url
        锚到 Task（#63）；取不到则优雅退化成普通 spawn（见 _compose_issue_brief）。
        ``model`` 可选：指定初始模型；启动后由「模型黏住」逻辑下发（#65）。ACP 只在活
        session 报模型，故 spawn 前无法硬校验——不支持/打错会 warning 回退默认。
        """
        project = self._resolve_project(project_name)
        if project is None:
            known = ", ".join(self._all_projects()) or "(无)"
            return f"未知项目 '{project_name}'。已注册项目: {known}"
        agent_label, agent_argv, err = self._resolve_agent(project, agent)
        if agent_argv is None:
            return err
        # 模型软校验：拿缓存比一比，不在已知列表就提示（仍透传，启动时再硬校验/回退）。
        model = model.strip()
        model_note = ""
        if model:
            cached = self.model_store.get(agent_label)
            if cached and model not in cached:
                model_note = f"（注意：{model} 不在 {agent_label} 已知模型 {cached} 里，将尝试下发）"
        # issue fetch 放在并发上限检查之前：它只读 forge、不碰 current-runner registry，避免加宽
        # 「检查 → _launch 登记」之间的 TOCTOU 窗口。
        brief, issue_url, note = task, "", ""
        if issue and issue > 0:
            brief, issue_url, note = await self._compose_issue_brief(
                project, task, issue
            )
        if not self._reserve_agent_slot():
            return f"已达并发上限 {self.cfg.max_agents}，请先 `/stop` 一个再派发。"
        try:
            header = f"🚀 {agent_label} · {project_name}\n任务: {task}"
            if issue_url:
                header += f"\nissue: {issue_url}"
            channel = self._channel_for(conversation)
            session_conversation = await asyncio.to_thread(
                channel.create_thread, header
            )
            new_task = self.store.create(
                project_name=project_name,
                agent_label=agent_label,
                description=task,
                channel_key=session_conversation.channel_key(),
                conversation_payload=self._serialize_conversation_ref(
                    session_conversation
                ),
                workspace=str(project.path),
                issue_url=issue_url,
                model=model,
            )
            self._launch(
                new_task,
                agent_argv,
                first_turn=TurnRequest(
                    brief,
                    session_conversation,
                ),
            )
        finally:
            self._release_agent_slot()
        bound = f"（brief 来自 issue {issue_url}）" if issue_url else note
        return (
            f"已建任务 [{new_task.session_id}]，在项目 {project_name} 启动 "
            f"{agent_label} 处理：{task}{bound}{model_note}"
        )

    async def _compose_issue_brief(
        self, project: Project, task: str, issue: int
    ) -> tuple[str, str, str]:
        """取 issue 完整正文当 brief，返回 (brief, issue_url, note)。

        取不到（无 forge 绑定 / 编号不存在 / 命令失败）优雅退化：返回 (task, "", 提示)，
        调用方照常派活、只是没绑 issue——不因 forge 出问题挡住派发。
        """
        ref = await forge.resolve_forge(project)
        if ref is None:
            return task, "", f"（项目无 forge 绑定，未关联 issue #{issue}）"
        try:
            item = await forge.get_item(ref, "issue", issue, body_limit=None)
        except forge.ForgeError as exc:
            return task, "", f"（取 issue #{issue} 失败：{exc}，未关联）"
        brief = (
            f"{task}\n\n---\n以下是关联的 issue #{item.get('number', issue)}"
            f"（{ref.slug}）作为本次任务的背景/需求：\n"
            f"标题：{item.get('title', '')}\n\n{item.get('body', '')}"
        )
        return brief, item.get("url", ""), ""

    # ------------------------------------------------------------------ #
    # 后台任务（#68）：agent 经 fdx bg run → 控制面 → daemon 拥有进程 → 完成唤回
    # ------------------------------------------------------------------ #

    async def _ctl_bg_run(self, task_id: str, body: dict) -> tuple[int, dict]:
        """控制面 ``POST /v1/bg/run`` 处理器：起一个 daemon 托管的后台进程。

        在主 loop 上执行（由 ControlServer marshal 而来）。task_id 已由 token 解出。
        """
        command = body.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(c, str) and c for c in command)
        ):
            return 400, {"error": "command 必须是非空字符串数组"}
        task = self.store.get(task_id)
        if task is None:
            return 404, {"error": f"未知任务 {task_id}"}
        cwd = str(body.get("cwd") or task.workspace or ".")
        # 超时：请求显式指定优先（fdx --timeout），否则用 config 默认（<=0 = 不超时）
        try:
            req_timeout = float(body.get("timeout") or 0)
        except (TypeError, ValueError):
            req_timeout = 0.0
        timeout = req_timeout if req_timeout > 0 else self.cfg.bg_job_timeout
        try:
            job = await self._launch_bg_job(
                task_id, list(command), cwd, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("启动后台任务失败 task=%s", task_id)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
        return 200, {"job_id": job.job_id, "status": job.status}

    async def _ctl_delegation_report(
        self,
        task_id: str,
        body: dict,
    ) -> tuple[int, dict]:
        delegation_id = str(body.get("delegation_id", "")).strip()
        report_status = str(body.get("status", "")).strip().replace("-", "_")
        message = str(body.get("message", "")).strip()
        if not delegation_id or not report_status or not message:
            return 400, {
                "error": "delegation_id、status 和 message 都必填",
            }
        if report_status not in DELEGATION_REPORT_STATUSES - {"unreported"}:
            return 400, {
                "error": "status 必须是 completed、input-required 或 blocked",
            }
        if len(message) > _DELEGATION_REPORT_MAX:
            return 400, {
                "error": f"message 最多 {_DELEGATION_REPORT_MAX} 字",
            }
        delegation = self.delegation_store.get(delegation_id)
        if delegation is None:
            return 404, {"error": f"未知委派 {delegation_id}"}
        if delegation.worker_session_id != task_id:
            return 403, {"error": "该委派不属于当前 Worker Session"}
        if delegation.report_status:
            if (
                delegation.report_status == report_status
                and delegation.report_message == message
            ):
                return 200, {
                    "delegation_id": delegation_id,
                    "status": report_status.replace("_", "-"),
                    "reported": True,
                }
            return 409, {"error": f"委派 {delegation_id} 已提交过报告"}
        sess = self._runners.get_for_session(task_id)
        if sess is None or sess.current_turn_id != delegation.worker_turn_id:
            return 409, {"error": "该委派不是当前正在执行的 Worker Turn"}
        if delegation.status not in {"submitted", "running"}:
            return 409, {
                "error": f"委派 {delegation_id} 当前状态为 {delegation.status}",
            }
        self.delegation_store.update(
            delegation_id,
            report_status=report_status,
            report_message=message,
        )
        return 200, {
            "delegation_id": delegation_id,
            "status": report_status.replace("_", "-"),
            "reported": True,
        }

    def _job_summary(self, job: Job) -> dict:
        """给 CLI 的 job 摘要（不含大输出）。"""
        return {
            "job_id": job.job_id,
            "status": job.status,
            "exit_code": job.exit_code,
            "timed_out": job.timed_out,
            "command": " ".join(job.command),
            "created_at": job.created_at,
            "finished_at": job.finished_at,
        }

    def _own_job(self, task_id: str, job_id: str) -> "Job | None":
        """取属于该 task 的 job；不存在或不属于该 task 返回 None（隔离：只能看/管自己的）。"""
        job = self.job_store.get(job_id)
        return job if job is not None and job.task_id == task_id else None

    async def _ctl_bg_list(self, task_id: str, body: dict) -> tuple[int, dict]:
        """``POST /v1/bg/list``：列出本 task 起的后台 job（新→旧）。"""
        jobs = sorted(
            self.job_store.by_task(task_id), key=lambda j: j.created_at, reverse=True
        )
        return 200, {"jobs": [self._job_summary(j) for j in jobs]}

    async def _ctl_bg_logs(self, task_id: str, body: dict) -> tuple[int, dict]:
        """``POST /v1/bg/logs``：读某 job 的输出尾部（末 ``tail`` 行）+ 当前状态。"""
        job_id = str(body.get("id") or "")
        job = self._own_job(task_id, job_id)
        if job is None:
            return 404, {"error": f"未找到属于本任务的后台 job {job_id!r}"}
        try:
            tail = int(body.get("tail") or 50)
        except (TypeError, ValueError):
            tail = 50
        tail = max(1, min(tail, 1000))  # 限幅，防超大响应
        return 200, {
            **self._job_summary(job),
            "output": _read_tail_lines(job.output_file, tail),
        }

    async def _ctl_bg_kill(self, task_id: str, body: dict) -> tuple[int, dict]:
        """``POST /v1/bg/kill``：终止一个在跑的后台 job（watcher 随后照常收尾）。"""
        job_id = str(body.get("id") or "")
        job = self._own_job(task_id, job_id)
        if job is None:
            return 404, {"error": f"未找到属于本任务的后台 job {job_id!r}"}
        proc = self._bg_procs.get(job_id)
        if proc is None:
            return 200, {
                "job_id": job_id,
                "status": job.status,
                "killed": False,
                "note": "该 job 已不在运行",
            }
        try:
            proc.kill()
        except Exception as exc:  # noqa: BLE001
            logger.exception("kill 后台任务失败 %s", job_id)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
        logger.info("后台任务 %s 被 bg kill 终止 task=%s", job_id, task_id)
        return 200, {"job_id": job_id, "killed": True}

    async def _launch_bg_job(
        self, task_id: str, command: list[str], cwd: str, *, timeout: float = 0.0
    ) -> Job:
        """spawn 一个 daemon 拥有的后台进程（argv exec，不经 shell），输出重定向到文件，
        登记 Job 并起 watcher。返回 Job（进程仍在跑）。

        进程是 **daemon 的子进程**（非 agent 的），故 agent 挂起/恢复不影响它。用户自己的
        build/训练命令——继承 daemon(=用户) 的完整环境（PATH/CUDA/conda 等），与用户在终端
        直接跑一致。shell 特性（管道/&&）需 agent 显式 `bash -c "..."`。

        ``stdin=DEVNULL``：给子进程一个立即 EOF 的 stdin——否则它会继承 daemon 的（控制台）
        stdin，交互式 shell profile 里读 stdin 的步骤（实测 `opam env`）会阻塞、卡死整个
        进程（#68 真机踩坑：一条 `Start-Sleep 4` 卡了 26 分钟在 opam env）。
        ``timeout>0`` 时超时杀进程当兜底（长训练默认不超时）。
        """
        logs_dir = self._bg_logs_dir or (DEFAULT_CONFIG_PATH.parent / "bg-logs")
        logs_dir.mkdir(parents=True, exist_ok=True)
        job = self.job_store.create(task_id=task_id, command=command, cwd=cwd)
        out_path = logs_dir / f"{job.job_id}.log"
        self.job_store.update(job.job_id, output_file=str(out_path))
        out_file = open(out_path, "wb")  # noqa: SIM115 —— 交给 watcher 在进程退出后关
        try:
            argv = [resolve_executable(command[0]), *command[1:]]
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_file,
                stderr=asyncio.subprocess.STDOUT,
                env=os.environ.copy(),
            )
        except Exception:
            out_file.close()
            self.job_store.update(job.job_id, status="killed", exit_code=None)
            raise
        logger.info(
            "后台任务 %s 启动: task=%s pid=%s timeout=%s cmd=%.80s",
            job.job_id,
            task_id,
            proc.pid,
            timeout or "无",
            " ".join(command),
        )
        self._bg_procs[job.job_id] = proc  # 供 bg kill（#70）
        # 存强引用：asyncio 只对 task 持弱引用，不存会被 GC 掉、watcher 中途消失（#68）
        watcher = asyncio.create_task(
            self._watch_bg_job(job.job_id, proc, out_file, timeout),
            name=f"bgjob-{job.job_id}",
        )
        self._bg_watchers.add(watcher)
        watcher.add_done_callback(self._bg_watchers.discard)
        return job

    async def _watch_bg_job(self, job_id: str, proc, out_file, timeout: float) -> None:
        """等后台进程退出 → 落 Job 状态 → 把「完成 + 输出尾部」入队回它的 task。

        ``timeout>0`` 时超时就 kill 进程、标 timed_out，但**照样唤回 agent**（带超时说明）——
        兜底防卡死进程无声堆积。
        """
        timed_out = False
        try:
            if timeout and timeout > 0:
                try:
                    rc = await asyncio.wait_for(proc.wait(), timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    logger.warning("后台任务 %s 超时（%.0fs），杀掉", job_id, timeout)
                    try:
                        proc.kill()
                    except Exception:
                        logger.debug("kill 超时进程失败 %s", job_id, exc_info=True)
                    rc = await proc.wait()
            else:
                rc = await proc.wait()
        finally:
            self._bg_procs.pop(job_id, None)  # 退出即出「在跑」表（#70）
            try:
                out_file.close()
            except Exception:
                pass
        self.job_store.update(
            job_id,
            status="killed" if timed_out else "exited",
            exit_code=rc,
            finished_at=time.time(),
            timed_out=timed_out,
        )
        job = self.job_store.get(job_id)
        if job is None:
            return
        logger.info(
            "后台任务 %s %s: exit=%s task=%s",
            job_id,
            "超时被杀" if timed_out else "退出",
            rc,
            job.task_id,
        )
        await self._deliver_bg_result(job, rc)

    def _build_bg_block(self, job: Job, rc: int) -> str:
        """单个后台任务完成的 `<bg_job_done>` 块（id/命令/exit/耗时/超时/输出尾部）。
        多个 job 合并唤回时各出一块（见 _BgBatch），引导语由 render() 单独补一条。"""
        tail = _read_tail(job.output_file)
        dur = (
            _fmt_duration(job.finished_at - job.created_at) if job.finished_at else "?"
        )
        return "\n".join(
            [
                "<bg_job_done>",
                f"Job: {job.job_id}",
                f"Command: {' '.join(job.command)}",
                f"Exit Code: {rc}",
                f"Duration: {dur}",
                f"Timed Out: {'yes（已超时被杀）' if job.timed_out else 'no'}",
                "Output (tail):",
                tail,
                "</bg_job_done>",
            ]
        )

    def _build_bg_prompt(self, job: Job, rc: int) -> str:
        """单个后台任务的完整唤回 prompt（块 + 引导语）——用于挂起恢复的首轮（不合并）。
        与单块 _BgBatch.render() 等价。"""
        return f"{self._build_bg_block(job, rc)}\n\n{_BG_GUIDANCE}"

    def _bg_result_message(self, job: Job, rc: int) -> str:
        """后台任务完成的**可见**话题消息：状态 + exit + 耗时 + 输出尾部（用户直接看结果）。

        与注入给 agent 的 `<bg_job_done>` prompt 分开——prompt 是给 agent 读的、不回显到
        话题；这条是发给用户看的，补上「结果没进对话」的显示缺口（#68）。
        """
        mark = "⏱️" if job.timed_out else ("✅" if rc == 0 else "❌")
        dur = (
            _fmt_duration(job.finished_at - job.created_at) if job.finished_at else "?"
        )
        state = "超时被杀" if job.timed_out else "完成"
        head = f"{mark} 后台任务 {job.job_id} {state}（exit {rc} · 用时 {dur}）"
        tail = _clip(_read_tail(job.output_file), 600)
        return f"{head}\n输出（尾部）:\n{tail}" if tail else head

    async def _deliver_bg_result(self, job: Job, rc: int) -> None:
        """把后台任务完成送回对应 task：活跃则合并入队，挂起则恢复，终止则只通知。

        无论哪种去向，只要 task 还在，都先往它的话题发一条**可见**完成消息（带输出尾部），
        让用户直接看到结果，再驱动 agent 接续（主线 🔔 保留作「快去看」提醒）。

        活跃分支按 #79 合并：队尾已有未消费批次（``pending_bg``）时只把本 job 的块追加进
        去、不再入队，让相邻完成的多个 job 只唤回一轮。挂起 task 不合并（各自恢复）。
        """
        verb = "成功" if rc == 0 else f"失败(exit {rc})"
        tag = f"[{job.task_id}]"
        task = self.store.get(job.task_id)
        if task is None:
            await self._notify_main(
                f"🔔 后台任务 {job.job_id} {verb}，但任务 {tag} 已不存在。"
            )
            return
        # 先把「结果」发到话题（可见），再驱动 agent 接续
        await self._safe_send_text(
            self._bg_result_message(job, rc),
            conversation=self._conversation_for_session(task),
        )
        sess = self._runners.get_for_session(task.session_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            # check-set 之间无 await：单线程原子，并发完成的 job 不会漏合并/重复入队。
            if sess.pending_bg is not None:
                sess.pending_bg.add(self._build_bg_block(job, rc))  # 合并进队尾批次
                await self._notify_main(
                    f"🔔 {tag} 后台任务 {job.job_id} {verb}，已并入待处理批次。"
                )
            else:
                batch = _BgBatch()
                batch.add(self._build_bg_block(job, rc))
                sess.pending_bg = batch
                sess.queue.put_nowait(batch)  # 首个：入队一次
                await self._notify_main(
                    f"🔔 {tag} 后台任务 {job.job_id} {verb}，已让 agent 继续。"
                )
            return
        if task.is_terminal:
            await self._notify_main(
                f"🔔 {tag} 后台任务 {job.job_id} {verb}，但任务已{task.status}，未自动继续。"
            )
            return
        # 挂起/idle 但无活跃 session：load_session 恢复，把完成 prompt 作为首轮（不合并）
        ok, why = self._try_resume(
            task,
            first_turn=TurnRequest(
                self._build_bg_prompt(job, rc),
                self._conversation_for_session(task),
            ),
        )
        if ok:
            await self._notify_main(
                f"🔔 {tag} 后台任务 {job.job_id} {verb}，已恢复 agent 继续。"
            )
        else:
            await self._notify_main(
                f"🔔 {tag} 后台任务 {job.job_id} {verb}，但恢复 agent 失败：{why}"
            )

    # ------------------------------------------------------------------ #
    # 发送辅助
    # ------------------------------------------------------------------ #

    async def _safe_send_text(
        self,
        text: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """向 Conversation 独立发文本并隔离单个 Channel 失败。"""
        try:
            channel = self._channel_for(conversation)
            await asyncio.to_thread(channel.send_text, conversation, text)
        except Exception:
            logger.exception(
                "Channel 独立文本发送失败 conversation=%s",
                conversation.to_log_string(),
            )

    async def _safe_handle_session_event(
        self,
        event: SessionEvent,
        *,
        conversation: ConversationRef,
        trace_sequence: int | None,
    ) -> None:
        """向 Conversation 投影 SessionEvent，并隔离单个 Channel 失败。"""
        try:
            channel = self._channel_for(conversation)
            await asyncio.to_thread(
                channel.handle_session_event,
                conversation,
                event,
                trace_sequence=trace_sequence,
            )
        except Exception:
            logger.exception(
                "Channel SessionEvent 投影失败 event=%s conversation=%s",
                event.event_id,
                conversation.to_log_string(),
            )

    async def _send_user(
        self,
        text: str,
        *,
        conversation: ConversationRef,
    ) -> None:
        """向用户所在的 Conversation 发送普通文本。"""
        await self._safe_send_text(text, conversation=conversation)

    async def _notify_main(self, text: str) -> None:
        """向控制台主线推一条独立通知（不建话题）——agent 完成/出错/挂起时用。"""
        conversation = self._control_conversation
        if conversation is None:
            return
        await self._safe_send_text(text, conversation=conversation)

    async def _shutdown(self) -> None:
        """退出清理：停 WS 线程，取消并等待全部 agent worker 收尾。"""
        for runtime in self._session_runtimes.values():
            try:
                await runtime.close()
            except Exception:
                logger.exception(
                    "Session Runtime 关闭失败 session=%s",
                    runtime.session_id,
                )
        await self._wait_runtime_events()
        if self._control is not None:
            # control.stop() 会阻塞（等 serve_forever 确认），且可能与正 run_
            # coroutine_threadsafe 回等主 loop 的 handler 线程死锁。挪到 worker
            # 线程跑、主 loop 继续转（解死锁）+ 超时兜底，绝不冻住关闭流程（#81）。
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._control.stop),
                    timeout=_CONTROL_STOP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "控制面关闭超时，跳过（serve_forever 是 daemon 线程，随进程退出）"
                )
            except Exception:
                logger.warning("控制面关闭失败，忽略", exc_info=True)
        self._stop_channels()
        # 把仍活跃的任务标记为 suspended，让重启后台账状态准确（且可 load_session 恢复）
        for sess in self._runners.values():
            task = self.store.get(sess.session_id)
            if task is not None and not task.is_terminal:
                self.store.update(sess.session_id, status="suspended")
        workers = [
            s.worker
            for s in self._runners.values()
            if s.worker is not None and not s.worker.done()
        ]
        for w in workers:
            w.cancel()
        for w in workers:
            try:
                await w
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("agent worker 退出异常")
        # 兜底：worker 的 finally(_close_session) 只包住主循环；启动段（agent.start /
        # set_model / 就绪回复）被 cancel 时 CancelledError 直接冒出、不走 finally，
        # registry 槽位悬空。这里把仍残留的 runner 逐个走同一关闭路径清掉——
        # _close_session 幂等（remove_if_current 按 identity、agent 只 aclose 一次），
        # 不会与已正常收尾的 worker 重复关闭。
        for sess in self._runners.values():
            await self._close_session(sess)
        if self._scan_executor is not None:
            try:
                await self._scan_executor.aclose()
            except Exception:
                logger.warning("扫描执行服务关闭失败，忽略", exc_info=True)
            self._scan_executor = None
        await self.aclose()
