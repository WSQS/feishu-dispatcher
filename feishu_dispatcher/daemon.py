"""daemon 主循环：飞书消息 → ACP agent → 飞书话题 完整闭环。

P0 原型范围（设计文档）：
- 硬编码项目匹配（不做 LLM 规划）
- 根消息 `/run` 触发 spawn，话题回复排队追加给同一 agent

生命周期模型（review R2/R3 修复后的设计）：
- 一个 `/run` = 一个 `_AgentSession`：agent 进程与 ACP session **跨 turn 存活**，
  上下文保留在 session 里
- 每个 session 一个 prompt 队列 + 单消费者 worker task，turn 串行执行
- 话题回复只入队；`/stop`（入队 None 哨兵）、执行出错或 daemon 退出才关闭 agent
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from pathlib import Path

from . import forge
from .acp_client import AcpAgent, AgentSpawn, OnAction, OnOutput, _resolve_executable
from .channel import Channel, ChannelMessage, ConversationRef, StreamingOutput
from .config import DEFAULT_CONFIG_PATH, Config, Project
from .control import ControlServer
from .feishu import FeishuBridge
from .llm import build_llm_client
from .scheduler import (
    LLMClient,
    SchedulerMemory,
    build_scheduler_tools,
    run_tool_loop,
)
from .store import Job, JobStore, ModelStore, ProjectStore, Task, TaskStore
from ._scan_executor import ScanExecutor
from ._viewer_token import ensure_token
from .viewer import (
    ViewerServer,
    health as viewer_health,
    list_projects as viewer_list_projects,
)
from .viewer import (
    file as viewer_file,
    tree as viewer_tree,
    tree_children as viewer_tree_children,
)

logger = logging.getLogger(__name__)

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
_HELP_CMDS = ("/help", "/?", "/usage")  # root 与话题内通用

#: 环境变量：re-exec 重启时置位，新进程据此发「已重启」回执
_REBOOTED_ENV = "FEISHU_DISPATCHER_REBOOTED"

#: message_id 去重窗口大小（飞书 ACK 异常时服务端会重推事件）
_DEDUP_CAPACITY = 512

#: 关闭时等控制面停下的上限（秒）；超时即放弃继续关（serve_forever 是 daemon 线程）。#81
_CONTROL_STOP_TIMEOUT = 5.0

_USAGE = (
    "用法：\n"
    "• `/run <项目名> <任务描述> [--agent <名>]`  派发任务给 agent（可选覆盖默认 agent）\n"
    "• `/attach <项目名> <agent> <session_id> [描述]`  附着外部 agent 会话为新任务"
    "（假定原会话已停止）\n"
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

#: Task.last_output 截断上限（收尾回复只留精华，防 tasks.json 涨）
_LAST_OUTPUT_MAX = 800

#: Task.error_message 截断上限（turn 异常诊断，异常类型 + 片段）
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


async def run(
    cfg: Config,
    *,
    discover: bool = False,
    store_path: Path | None = None,
    channel: Channel | None = None,
    channel_key: str | None = None,
) -> bool:
    """启动 daemon：飞书 WS 长连接 + agent 调度。阻塞直到收到退出信号。

    ``discover=True`` 时只打印收到消息的 chat_id，不执行任何命令
    （帮助用户发现群 id 后填进配置）。``store_path`` 是会话持久化文件
    （默认 config 同目录的 sessions.json）。``channel`` 未传时装配现有 Feishu
    Channel；注入其它实现时必须用 ``channel_key`` 指定稳定身份。未注入 Channel
    时 ``channel_key`` 默认取 ``feishu``。

    返回是否收到 ``/reboot``——cli.py 据此 re-exec 重启进程。
    """
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
    if channel is None:
        channel = FeishuBridge(
            app_id=cfg.app_id,
            app_secret=cfg.app_secret,
            main_loop=asyncio.get_running_loop(),
            chat_whitelist=cfg.chat_id,
            qps=cfg.feishu_qps,
            stream_mode=cfg.stream_mode,
            throttle_window=cfg.throttle_window,
        )
    daemon = _Daemon(
        cfg,
        discover=discover,
        store=TaskStore(store_path.parent / "tasks.json"),
        project_store=ProjectStore(store_path.parent / "projects.json"),
        model_store=ModelStore(store_path.parent / "models.json"),
        job_store=JobStore(store_path.parent / "jobs.json"),
        _bg_logs_dir=store_path.parent / "bg-logs",
        _viewer_token_path=store_path.parent / "viewer.token",
        _sched_memory=SchedulerMemory(
            store_path.parent / "scheduler_memory.json",
            # [llm].memory_rounds 可配；未配 [llm] 时记忆不参与派发，取默认即可
            max_turns=cfg.llm.memory_rounds if cfg.llm else 12,
        ),
        _channel=channel,
        _channel_key=resolved_channel_key,
    )
    await daemon.run()
    return daemon._reboot_requested


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


@dataclass
class _AgentSession:
    """一个活跃 agent 的运行时状态。"""

    thread_root_id: str
    project_name: str
    agent_label: str
    #: 关联的 Task id（持久台账的主键）
    task_id: str = ""
    #: agent 工作目录（= Task.workspace）
    cwd: str = ""
    #: 是否由 load_session 恢复而来（影响启动失败时的提示文案）
    resumed: bool = False
    #: 是否由 /attach 附着外部会话而来（= Task.origin == "attach"）；
    #: 影响启动成功/失败的提示文案（区别于普通恢复的「已恢复」）。
    attached: bool = False
    #: 关联的 forge issue URL（= Task.issue_url，#63）；供 footer/展示标归属，空 = 未绑定
    issue_url: str = ""
    #: agent 实例（先建 session、再建 agent，故允许 None）
    agent: "AcpAgent | None" = None
    #: 当前回合的流式输出呈现；回合间为 None
    current_output: StreamingOutput | None = None
    #: prompt 队列；None 是关闭哨兵（/stop / /done / mark_done），_BgBatch 是后台完成批次
    queue: "asyncio.Queue[str | _BgBatch | None]" = field(default_factory=asyncio.Queue)
    #: 队尾未消费的后台任务批次（#79）；非 None ⟺ 队尾是可继续合并的 _BgBatch。
    #: 入任何非 bg 项（enqueue）或被 worker 消费即清空——据此判「队尾能否再合并」。
    pending_bg: "_BgBatch | None" = None
    #: 收到 None 哨兵时置入的终止态：stopped（/stop，默认）或 done（/done / mark_done）
    terminate_status: str = "stopped"
    #: 本轮是否正在跑（worker 卡在 agent.prompt() 里）；/stop 据此决定要不要发 cancel
    turn_in_flight: bool = False
    #: 后台任务身份 token（本次启动一次性下发，注入 agent env，映射到 task_id）；#68
    bg_token: str = ""
    #: 单消费者 worker，持有 agent 完整生命周期
    worker: "asyncio.Task[None] | None" = None

    def enqueue(self, item: str) -> None:
        """入队一个普通 prompt（话题回复 / 首轮 / 新指令 / send_to_task），**断开** bg
        合并邻接（清 pending_bg）——之后完成的 bg 不会跨这个普通项去合并，保 FIFO。"""
        self.pending_bg = None
        self.queue.put_nowait(item)

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
    """Task 的单活 current-runner 槽位；task_id 只是 lookup key。"""

    def __init__(self) -> None:
        self._by_task: dict[str, _AgentSession] = {}

    def get_for_task(self, task_id: str) -> _AgentSession | None:
        return self._by_task.get(task_id)

    def register(self, task_id: str, runner: _AgentSession) -> None:
        if task_id in self._by_task:
            raise RuntimeError(f"task {task_id} 已有 current runner")
        self._by_task[task_id] = runner

    def is_current(self, task_id: str, runner: _AgentSession) -> bool:
        return self._by_task.get(task_id) is runner

    def remove_if_current(self, task_id: str, runner: _AgentSession) -> bool:
        if not self.is_current(task_id, runner):
            return False
        del self._by_task[task_id]
        return True

    def values(self) -> list[_AgentSession]:
        return list(self._by_task.values())

    def count(self) -> int:
        return len(self._by_task)


@dataclass
class _Daemon:
    cfg: Config
    discover: bool = False
    #: 任务台账（默认纯内存，不写盘）；run() 注入文件版（tasks.json）
    store: TaskStore = field(default_factory=lambda: TaskStore(None))
    #: 运行时注册的项目台账（默认纯内存）；run() 注入文件版（projects.json）。
    #: 有效项目 = config.toml 种子（cfg.projects）+ 这里注册的，见 _all_projects
    project_store: ProjectStore = field(default_factory=lambda: ProjectStore(None))
    #: 按 backend 的 available_models 缓存（默认纯内存）；run() 注入文件版（models.json）
    model_store: ModelStore = field(default_factory=lambda: ModelStore(None))
    #: 后台任务台账（默认纯内存）；run() 注入文件版（jobs.json）。#68
    job_store: JobStore = field(default_factory=lambda: JobStore(None))
    #: 调度器 LLM（P2）；None = 不启用自然语言派发。run() 按 cfg.llm 构造；测试可注入
    _llm: LLMClient | None = None
    #: 当前激活的 LLM profile 名（/llm 切换时更新，不持久化）；#74
    _llm_active: str = ""
    #: 调度器主线对话记忆（跨重启持久化）；默认纯内存，run() 注入文件版
    _sched_memory: SchedulerMemory = field(
        default_factory=lambda: SchedulerMemory(None)
    )
    #: 由启动装配层注入；_Daemon 不负责选择或构造具体通道实现。
    _channel: Channel | None = None
    #: 当前单 Channel 的稳定身份；后续多 Channel registry 以此作为持久化 lookup key。
    _channel_key: str = "feishu"
    #: 每个 Task 的单活 current runner；Thread 只经 Task 路由到这里。
    _runners: _CurrentRunnerRegistry = field(default_factory=_CurrentRunnerRegistry)
    _seen_message_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)
    #: 本地控制面（agent CLI 入口）；run() 里启动，测试构造 _Daemon 时为 None（不起 HTTP）
    _control: "ControlServer | None" = None
    #: 移动端查看器（只读 HTTP，给手机）；run() 里按 cfg.viewer.enabled 启动，否则 None。
    _viewer: "ViewerServer | None" = None
    #: 查看器的有界扫描执行服务；_start_viewer 里创建、_shutdown 里关闭（线程池非 daemon）。
    _scan_executor: ScanExecutor | None = None
    #: 后台任务身份表：token → task_id（启 agent 时登记，关 session 时清）。#68
    _bg_tokens: dict[str, str] = field(default_factory=dict)
    #: 后台任务 watcher 的强引用（asyncio 只持弱引用，不存会被 GC）。#68
    _bg_watchers: set = field(default_factory=set)
    #: 在跑的后台进程：job_id → proc（launch 登记、watcher 退出清），供 bg kill。#70
    _bg_procs: dict = field(default_factory=dict)
    #: 后台任务输出日志目录；run() 注入（默认 config 同目录 bg-logs/）
    _bg_logs_dir: "Path | None" = None
    #: viewer token 文件路径；run() 注入（默认 config 同目录 viewer.token），_start_viewer 用
    _viewer_token_path: "Path | None" = None
    #: /reboot 收到后置位；run() 返回它，cli.py re-exec 重启进程
    _reboot_requested: bool = False
    #: run() 里创建的退出事件；/reboot 或退出信号 set 它跳出主循环
    _stop_event: "asyncio.Event | None" = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        if self._channel is None:
            raise RuntimeError("Channel 未注入")
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
            },
        )
        self._control.start()
        # 移动端查看器（#104/#107/#111）：只读 HTTP，给手机经私有网络连。默认不起
        # （cfg.viewer 为 None 或 enabled=false）。失败不拖累 daemon —— 记 ERROR 日志、
        # 飞书功能照常（决策 Q3=β）。token 未填则自动生成 + 持久化（决策 Q3/Q8）。
        if self.cfg.viewer and self.cfg.viewer.enabled:
            self._viewer = self._start_viewer(loop)
        self._channel.start(self._handle_message)
        logger.info(
            "feishu-dispatcher daemon 已启动（调度器 LLM: %s），等待飞书消息…",
            "on" if self._llm else "off",
        )
        # re-exec 重启起来的进程：给控制台发一条「已重启」回执（HTTP，不依赖 WS）
        if os.environ.pop(_REBOOTED_ENV, None):
            await self._notify_main("✅ daemon 已重启完成。")
        try:
            # R13：看门狗——最多等 30s 或直到 _stop_event 被 set（/reboot / 退出）；
            # 超时则检查 WS 线程是否存活，死了 channel.restart()。
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass  # 正常：每 30s 醒来检查一次
                if self._stop_event.is_set():
                    break
                if not self._channel.is_alive():
                    logger.error("飞书 WS 线程已死亡，尝试重启…")
                    self._channel.restart()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("收到退出信号，清理 agent…")
        finally:
            await self._shutdown()

    def _start_viewer(self, loop: asyncio.AbstractEventLoop) -> ViewerServer | None:
        """拉起移动端查看器（只读 HTTP）。token 永远自动生成 + 持久化 + 日志打印
        （决策 Q3/Q8，config 里不配 token）；端口/绑定失败记 ERROR、返回 None，
        不拖累 daemon（Q3=β）。

        token 落 ``_viewer_token_path``（run() 注入 = config 同目录 viewer.token）；
        None 时（测试构造 _Daemon 不经 run()）兜底 DEFAULT_CONFIG_PATH 同目录。
        """
        assert self.cfg.viewer is not None
        v = self.cfg.viewer
        token_path = (
            self._viewer_token_path or DEFAULT_CONFIG_PATH.parent / "viewer.token"
        )
        token = ensure_token(token_path)
        logger.info(
            "移动端查看器 token（已存 viewer.token，重启不变；填进手机 App）: %s", token
        )
        try:
            self._scan_executor = ScanExecutor()
            vs = ViewerServer(
                token,
                routes={
                    ("GET", "/api/health"): viewer_health,
                    ("GET", "/api/projects"): viewer_list_projects,
                    ("GET", "/api/projects/{name}/tree"): viewer_tree,
                    ("GET", "/api/projects/{name}/tree/children"): viewer_tree_children,
                    ("GET", "/api/projects/{name}/file"): viewer_file,
                },
                host=v.bind,
                port=v.port,
                main_loop=loop,
                ctx={
                    "all_projects": self._all_projects,
                    "scan_executor": self._scan_executor,
                },
            )
            vs.start()
            return vs
        except OSError:
            logger.error(
                "移动端查看器启动失败（bind %s:%s 可能被占用）；飞书功能不受影响",
                v.bind,
                v.port,
                exc_info=True,
            )
            return None

    # ------------------------------------------------------------------ #
    # 消息分发
    # ------------------------------------------------------------------ #

    def _is_duplicate(self, message_id: str) -> bool:
        """按 message_id 幂等去重（R5：ACK 异常时飞书会重推同一事件）。"""
        if not message_id:
            return False
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids[message_id] = None
        while len(self._seen_message_ids) > _DEDUP_CAPACITY:
            self._seen_message_ids.popitem(last=False)
        return False

    async def _handle_message(self, msg: ChannelMessage) -> None:
        """所有飞书消息的入口（在主 event loop 上）。"""
        if self.cfg.chat_id and msg.conversation_id != self.cfg.chat_id:
            logger.debug("忽略非目标群消息 chat_id=%s", msg.conversation_id)
            return
        # 忽略无发送者的系统消息
        if not msg.sender_id:
            return
        if self._is_duplicate(msg.message_id):
            logger.info("忽略重复消息 message_id=%s", msg.message_id)
            return
        logger.info(
            "收到消息 chat=%s msg=%s thread_root=%s text=%r",
            msg.conversation_id,
            msg.message_id,
            msg.thread_id,
            msg.text,
        )

        # R10：discover 模式只打印 chat_id 帮助发现，不执行任何命令
        if self.discover:
            logger.info(
                "[discover] chat_id=%r sender_id=%r — 填入 config.toml 的 chat_id 即可",
                msg.conversation_id,
                msg.sender_id,
            )
            return

        # R10：发送者白名单（非空时校验）
        if self.cfg.sender_whitelist and msg.sender_id not in self.cfg.sender_whitelist:
            logger.debug(
                "忽略非白名单发送者 sender_id=%s (msg=%s)",
                msg.sender_id,
                msg.message_id,
            )
            return

        if msg.thread_id:
            await self._forward_to_agent(msg)
            return

        text = msg.text.strip()
        if text.startswith(_DISPATCH_PREFIX):
            await self._spawn_for_root(msg, text[len(_DISPATCH_PREFIX) :].strip())
        elif text == _ATTACH_CMD or text.startswith(_ATTACH_CMD + " "):
            await self._attach_for_root(msg, text[len(_ATTACH_CMD) :].strip())
        elif text.startswith(_TASK_PREFIX):
            await self._show_task(msg, text[len(_TASK_PREFIX) :].strip())
        elif text == _LIST_CMD:
            await self._list_agents(msg)
        elif text == _CLEAR_CMD:
            n = self.store.clear_terminal()
            await self._reply_user(
                msg.message_id, f"🧹 已清理 {n} 条已结束任务的历史。"
            )
        elif text == _PROJECT_CMD or text.startswith(_PROJECT_CMD + " "):
            await self._handle_project_cmd(msg, text[len(_PROJECT_CMD) :].strip())
        elif text == _MODELS_CMD or text.startswith(_MODELS_CMD + " "):
            await self._handle_models_cmd(msg, text[len(_MODELS_CMD) :].strip())
        elif text == _LLM_CMD or text.startswith(_LLM_CMD + " "):
            await self._handle_llm_cmd(msg, text[len(_LLM_CMD) :].strip())
        elif text == _REBOOT_CMD:
            await self._reboot(msg)
        elif text in _HELP_CMDS:
            await self._reply_user(msg.message_id, _USAGE)
        elif self._llm is not None and text and not text.startswith("/"):
            # P2：自然语言交给调度器 LLM 理解并派发（未配置 LLM 则回退到用法）
            await self._dispatch_nl(msg, text)
        else:
            await self._reply_user(msg.message_id, _USAGE)

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

    async def _handle_project_cmd(self, msg: ChannelMessage, arg: str) -> None:
        """root：``/project`` 列出、``/project add|remove`` 增删（对话/命令层）。"""
        if not arg:
            await self._reply_user(msg.message_id, self._format_project_list())
            return
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "add":
            fields = rest.split(maxsplit=2)
            if len(fields) < 3:
                await self._reply_user(
                    msg.message_id, "格式：`/project add <名称> <agent> <路径>`"
                )
                return
            _, out = self._register_project(fields[0], fields[1], fields[2])
            await self._reply_user(msg.message_id, out)
        elif sub == "remove":
            await self._reply_user(msg.message_id, self._remove_project(rest))
        else:
            await self._reply_user(
                msg.message_id,
                "用法：`/project`（列出）/ "
                "`/project add <名称> <agent> <路径>` / "
                "`/project remove <名称>`",
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

    async def _handle_llm_cmd(self, msg: ChannelMessage, arg: str) -> None:
        """root：``/llm`` 列出 LLM profile、``/llm <名>`` 切换激活的（重建 client，下轮生效）。"""
        profiles = self.cfg.llm_profiles
        if not profiles:
            await self._reply_user(
                msg.message_id,
                "未配置调度器 LLM（`[llm]` 段为空），无可切换的 profile。",
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
            await self._reply_user(msg.message_id, "\n".join(lines))
            return
        if arg not in profiles:
            await self._reply_user(
                msg.message_id,
                f"未知 profile '{arg}'。可选：{', '.join(profiles)}",
            )
            return
        if arg == self._llm_active:
            await self._reply_user(msg.message_id, f"当前已是 profile「{arg}」。")
            return
        self._llm = build_llm_client(profiles[arg])
        self._llm_active = arg
        s = profiles[arg]
        logger.info("调度器 LLM 切换 → %s（%s · %s）", arg, s.model, s.api)
        await self._reply_user(
            msg.message_id,
            f"✅ 已切换调度器 LLM → 「{arg}」（{s.model} · {s.api}）。下次派发生效。",
        )

    # ------------------------------------------------------------------ #
    # 模型缓存：/models 列出 / refresh 主动刷新（#65）
    # ------------------------------------------------------------------ #

    async def _handle_models_cmd(self, msg: ChannelMessage, arg: str) -> None:
        """root：``/models`` 列缓存、``/models refresh [agent]`` 主动刷新。"""
        arg = arg.strip()
        if arg == "refresh" or arg.startswith("refresh "):
            target = arg[len("refresh") :].strip()
            backends = [target] if target else list(self.cfg.agents.keys())
            if not backends:
                await self._reply_user(msg.message_id, "没有配置任何 [agents]。")
                return
            await self._reply_user(
                msg.message_id,
                f"🔄 正在刷新模型缓存（{', '.join(backends)}）…冷启动稍慢，请稍候。",
            )
            results = await asyncio.gather(*(self._refresh_models(b) for b in backends))
            lines = [("✅ " if ok else "❌ ") + m for ok, m in results]
            await self._reply_user(msg.message_id, "刷新完成：\n" + "\n".join(lines))
            return
        # 无参 = 列缓存
        cache = self.model_store.all()
        if not cache:
            await self._reply_user(
                msg.message_id,
                "模型缓存为空。发 `/models refresh` 采集（会临时起 agent 读取模型清单）。",
            )
            return
        lines = []
        for backend, d in cache.items():
            models = d.get("models") or []
            when = _fmt_ts(d.get("refreshed_at", 0.0))
            shown = "、".join(models) if models else "（该后端不暴露模型）"
            lines.append(f"• {backend}（更新于 {when}）：{shown}")
        lines.append("`/models refresh [agent]` 主动刷新。")
        await self._reply_user(msg.message_id, "模型缓存：\n" + "\n".join(lines))

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

        async def _noop_out(_t: str) -> None:
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

    async def _spawn_for_root(self, msg: ChannelMessage, body: str) -> None:
        """解析 ``/run <project> <task> [--agent <name>]``，建 session 并启动 worker。"""
        usage = "格式：`/run <项目名> <任务描述> [--agent <agent>]`"
        parts = body.split(maxsplit=1)
        if len(parts) < 2:
            await self._reply_user(msg.message_id, usage)
            return
        project_name = parts[0].strip()
        task, agent_override = _parse_agent_flag(parts[1].strip())
        if not task:
            await self._reply_user(msg.message_id, usage)
            return
        project = self._resolve_project(project_name)
        if project is None:
            known = ", ".join(self._all_projects()) or "(无)"
            await self._reply_user(
                msg.message_id, f"未知项目 '{project_name}'。已知项目: {known}"
            )
            return
        agent_label, agent_argv, err = self._resolve_agent(project, agent_override)
        if agent_argv is None:
            await self._reply_user(msg.message_id, err)
            return

        thread_root = msg.message_id
        existing = self.store.by_thread(thread_root)
        if (
            existing is not None
            and self._runners.get_for_task(existing.task_id) is not None
        ):
            logger.info("根消息 %s 已有 agent session，忽略重复 spawn", thread_root)
            return

        # R11：并发上限检查。check 与 _launch 的登记之间不能有 await，否则两条
        # 并发 /run 会都通过检查再各自登记，突破上限（TOCTOU）。故先原子地
        # 检查+登记，再发「🚀」提示。
        if self._runners.count() >= self.cfg.max_agents:
            await self._reply_user(
                msg.message_id,
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，请先 `/stop` 一个。",
            )
            return

        new_task = self.store.create(
            project_name=project_name,
            agent_label=agent_label,
            description=task,
            conversation=ConversationRef(self._channel_key, msg.conversation_id),
            thread_root_id=thread_root,
            workspace=str(project.path),
        )
        self._launch(new_task, agent_argv, first_prompt=task)
        await self._safe_reply(
            thread_root,
            f"🚀 [{new_task.task_id}] 启动 {agent_label} 处理项目 "
            f"{project_name}…\n任务: {task}",
        )

    async def _attach_for_root(self, msg: ChannelMessage, arg: str) -> None:
        """解析 ``/attach <项目> <agent> <session_id> [描述...]`` 并附着外部会话。

        参数解析后交给共用底层 :meth:`_attach_task`；按返回结果决定回复目标：
        成功不额外回（worker 会发附着摘要）；未建话题的失败回原消息；已建话题的
        罕见竞态失败回新话题。
        """
        usage = "格式：`/attach <项目名> <agent> <session_id> [描述...]`"
        parts = arg.split(maxsplit=3)
        if len(parts) < 3:
            await self._reply_user(msg.message_id, usage)
            return
        project_name = parts[0].strip()
        agent_in = parts[1].strip()
        session_id = parts[2].strip()
        user_desc = parts[3].strip() if len(parts) > 3 else ""
        if not project_name or not agent_in or not session_id:
            await self._reply_user(msg.message_id, usage)
            return
        task, root, message = await self._attach_task(
            project_name,
            agent_in,
            session_id,
            user_desc,
            conversation=ConversationRef(self._channel_key, msg.conversation_id),
        )
        if task is not None:
            return  # 成功：新话题的附着摘要由 worker 发
        if root:
            await self._safe_reply(root, message)
        else:
            await self._reply_user(msg.message_id, message)

    async def _attach_task(
        self,
        project_name: str,
        agent: str,
        session_id: str,
        description: str = "",
        *,
        conversation: ConversationRef,
    ) -> tuple["Task | None", str, str]:
        """附着外部会话为新 Task 的共用底层（``/attach`` 与 ``attach_session`` 工具都调它）。

        流程：校验→去重→先 load_session 探测→建 Task + 新飞书话题→``_launch(resume)``
        （附着摘要由 worker 就绪后发）。``agent`` 非空则覆盖项目 default_agent（须在
        ``[agents]``），空则用项目默认——``attach_session`` 的 agent 可选正依赖此语义。

        返回 ``(task, thread_root_id, message)``：
        - ``task`` 非 None = 成功建 Task 并拉起（message 为成功摘要）；
        - ``task`` 为 None 且 ``thread_root_id`` 非空 = 已建话题但终查超限的罕见竞态失败；
        - ``task`` 为 None 且 ``thread_root_id`` 为空 = 未建话题的失败（校验/去重/探测/预查）。

        无锁 MVP：不探测原 CLI 是否已退出——假定原会话已停止；会话交接锁机制另行立项。
        单次附着约 2× load_session 成本（先探测一次、拉起再恢复一次）——慢后端
        （如 Claude 冷启动 ~15–18s）耗时约为两次冷启动，属预期。
        """
        project = self._resolve_project(project_name)
        if project is None:
            known = ", ".join(self._all_projects()) or "(无)"
            return None, "", f"未知项目 '{project_name}'。已知项目: {known}"
        agent_label, agent_argv, err = self._resolve_agent(project, agent)
        if agent_argv is None:
            return None, "", err

        # 重复附着：同 (agent, session_id) 已有 Task → 拒绝并引导到已有 task。
        existing = self.store.by_agent_session(agent_label, session_id)
        if existing is not None:
            return (
                None,
                "",
                (
                    f"⚠️ 该会话已由任务 [{existing.task_id}] 附着（agent={agent_label}）。"
                    "请回到其话题继续，勿重复附着。"
                ),
            )

        # 先探测：同步 load_session 确认该 backend + cwd 能恢复此 session；失败不落 Task。
        ok, why = await self._probe_attach(
            agent_label, agent_argv, str(project.path), session_id
        )
        if not ok:
            return None, "", why

        # 并发上限双保险（同 /run 的 TOCTOU 防护）：
        #  ① 先查——超限直接回原消息、**不建新话题**（避免孤儿话题）。
        if self._runners.count() >= self.cfg.max_agents:
            return (
                None,
                "",
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，请先 `/stop` 一个。",
            )
        # 新话题（Channel.create_thread 开新话题拿 thread_root_id），header = 固定摘要 + 截断
        # session_id + 可选描述。
        assert self._channel is not None
        sid = _short_sid(session_id)
        header = f"🔗 {agent_label} · {project_name}\n附着外部会话: {sid}"
        if description:
            header += f"\n说明: {description}"
        root = await asyncio.to_thread(
            self._channel.create_thread, conversation.conversation_id, header
        )
        #  ② 终查——create_thread 是 await，其间别的并发附着/派发可能占走名额；
        # 终查与 create+_launch 之间**无 await**，守住「check→launch 无 await」不变量。
        # 终查超限属罕见竞态：话题已建，就地提示并放弃，不落 Task。
        if self._runners.count() >= self.cfg.max_agents:
            return (
                None,
                root,
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，附着未完成。"
                "请先 `/stop` 一个再试。",
            )
        task_desc = f"附着外部会话 {agent_label}/{sid}"
        if description:
            task_desc += f" — {description}"
        new_task = self.store.create(
            project_name=project_name,
            agent_label=agent_label,
            description=task_desc,
            conversation=conversation,
            thread_root_id=root,
            workspace=str(project.path),
            session_id=session_id,
            origin="attach",
        )
        self._launch(
            new_task,
            agent_argv,
            first_prompt=None,
            resume_session_id=session_id,
            attached=True,
        )
        return (
            new_task,
            root,
            f"已附着外部会话 {agent_label}/{sid} 为任务 [{new_task.task_id}]。",
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

        async def _noop_out(_t: str) -> None:
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
        resume_session_id: str | None = None,
    ) -> AcpAgent:
        """构造底层 agent（拆出来是测试注入点）。"""
        return AcpAgent(
            spawn,
            on_output,
            on_action=on_action,
            resume_session_id=resume_session_id,
            start_timeout=self.cfg.agent_start_timeout,
        )

    def _launch(
        self,
        task: Task,
        agent_argv: list[str],
        first_prompt: str | None,
        *,
        resume_session_id: str | None = None,
        attached: bool = False,
    ) -> _AgentSession:
        """按 Task 建 session、接线 on_output、入队首条 prompt、启动 worker。

        ``resume_session_id`` 非 None 时 agent 用 load_session 恢复（惰性重连）。
        ``first_prompt=None`` 时只把 agent 拉起来在线（不跑首轮），用于 resume_task。
        ``attached=True`` 仅由 ``/attach`` 的**首次**拉起置位——附着摘要文案；附着任务
        事后经 ``_try_resume`` 恢复时仍走普通「已恢复」路径（attached 默认 False）。
        """
        sess = _AgentSession(
            thread_root_id=task.thread_root_id,
            project_name=task.project_name,
            agent_label=task.agent_label,
            task_id=task.task_id,
            cwd=task.workspace,
            resumed=resume_session_id is not None,
            attached=attached,
            issue_url=task.issue_url,
        )

        async def on_output(text: str) -> None:
            if (
                self._runners.is_current(sess.task_id, sess)
                and sess.current_output is not None
            ):
                sess.current_output.feed(text)

        async def on_action(action: dict) -> None:
            # 审计（A）：只有 current runner 能把 tool_call 记进 Task；旧代 runner
            # 的迟到 callback 仍可收尾自身资源，但不能再代表 Task 写当前运行态。
            if not self._runners.is_current(sess.task_id, sess):
                return
            cur = self.store.get(sess.task_id)
            turn = (cur.turns if cur else 0) + 1
            self.store.add_action(sess.task_id, {"turn": turn, **action})

        # 配置里给该后端声明的追加 env（[agents.<名>].env，如 codex 的 CODEX_PATH）打底。
        env: dict[str, str] = dict(self.cfg.agent_env.get(task.agent_label, {}))
        # 身份注入（#68）：给 agent 子进程一份一次性 token + 控制面 URL（经 env 逐层
        # 透传到 agent 跑的 shell → fdx）。有控制面才注入（测试无控制面时不注入）。
        if self._control is not None:
            token = secrets.token_urlsafe(16)
            self._bg_tokens[token] = task.task_id
            sess.bg_token = token
            env.update(
                {
                    "FEISHU_DISPATCHER_URL": self._control.base_url,
                    "FEISHU_DISPATCHER_TOKEN": token,
                    "FEISHU_DISPATCHER_TASK_ID": task.task_id,
                }
            )
        sess.agent = self._make_agent(
            AgentSpawn(command=list(agent_argv), cwd=task.workspace, env=env),
            on_output,
            on_action,
            resume_session_id=resume_session_id,
        )
        if first_prompt is not None:
            sess.enqueue(first_prompt)
        self._runners.register(task.task_id, sess)
        sess.worker = asyncio.create_task(
            self._agent_worker(sess), name=f"agent-{task.task_id}"
        )
        return sess

    async def _agent_worker(self, sess: _AgentSession) -> None:
        """一个 agent 的完整生命周期：启动 → 串行消费 prompt 队列 → 关闭。"""
        root = sess.thread_root_id
        try:
            await sess.agent.start()
        except Exception as exc:
            logger.exception("agent 启动失败")
            if self._runners.is_current(sess.task_id, sess):
                err = _clip(f"{type(exc).__name__}: {exc}", _ERROR_MSG_MAX)
                self.store.update(sess.task_id, status="failed", error_message=err)
                if sess.attached:
                    await self._safe_reply(
                        root,
                        "❌ 附着失败（session 无法恢复或已过期）。"
                        "请确认后重试，或发送 `/run` 新开。",
                    )
                elif sess.resumed:
                    await self._safe_reply(
                        root,
                        "❌ 会话恢复失败（可能已在 agent 侧过期）。发送 `/run` 重开。",
                    )
                else:
                    await self._safe_reply(root, f"❌ agent 启动失败: {str(exc)[:200]}")
            await self._close_session(sess)
            return
        if not self._runners.is_current(sess.task_id, sess):
            await self._close_session(sess)
            return
        # 启动成功：把 session_id + 模型落进 Task 并置 idle（供重启后 load_session 恢复）
        reported = getattr(sess.agent, "model", "") or ""
        model = reported
        # 模型黏住（恢复后）：agent 后端重载会话（load_session）时可能把模型重置回默认，
        # 报回的 current_value 即是默认——若直接采信就会把用户此前 /model 切过的模型覆盖掉
        # （台账 + 实际都还原）。故：Task 若记着用户切过的模型且后端仍支持，就重新下发一次，
        # 保证「切模型 → 挂起 → 恢复」后仍用用户选的模型。后端已持久化（reported==pinned）时跳过。
        task = self.store.get(sess.task_id)
        pinned = (task.model if task else "") or ""
        available = getattr(sess.agent, "available_models", None) or []
        # 被动刷新模型缓存：真实 agent 一启动就把它报的 available_models 存下来，
        # 供 spawn 前 /models、list_models 列出/校验（copilot 报空也如实存）。
        self.model_store.update(sess.agent_label, list(available))
        if pinned and pinned != reported and pinned in available:
            try:
                await sess.agent.set_model(pinned)
                model = pinned
                logger.info("恢复后重新应用模型 task=%s → %s", sess.task_id, pinned)
            except Exception:
                logger.exception(
                    "恢复后重新应用模型失败 task=%s → %s", sess.task_id, pinned
                )
                model = reported  # 应用失败：如实保留后端报回的模型，不谎报
        elif pinned and pinned != reported and pinned not in available:
            logger.warning(
                "恢复后无法保持模型 task=%s：后端已不提供 %s（回退 %s）",
                sess.task_id,
                pinned,
                reported or "默认",
            )
        if not self._runners.is_current(sess.task_id, sess):
            await self._close_session(sess)
            return
        self.store.update(
            sess.task_id,
            session_id=sess.agent.session_id or "",
            status="idle",
            model=model,
        )
        if sess.attached:
            # 附着摘要（区别于普通「已就绪」/「已恢复」文案）：说明来源 + 后续回复续接上下文
            sid = _short_sid(sess.agent.session_id or "")
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
        await self._safe_reply(root, base)
        try:
            while True:
                # 空闲挂起（坑 1）：超时无新回复就关掉 agent 腾出 max_agents 名额，
                # 但**保留** sessions.json 记录（区别于 /stop 的删除）——之后在本
                # 话题回复即走 load_session 恢复。<=0 表示不自动挂起。
                timeout = self.cfg.idle_timeout if self.cfg.idle_timeout > 0 else None
                try:
                    prompt = await asyncio.wait_for(sess.queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if not self._runners.is_current(sess.task_id, sess):
                        break
                    self.store.update(sess.task_id, status="suspended")
                    await self._safe_reply(
                        root,
                        "💤 空闲超时，已挂起该 agent（在本话题回复即自动恢复）。",
                    )
                    if self._runners.is_current(sess.task_id, sess):
                        await self._notify_main(
                            f"💤 {sess.project_name} 已空闲挂起（在其话题回复即自动恢复）。"
                        )
                    break
                if not self._runners.is_current(sess.task_id, sess):
                    break
                if prompt is None:
                    status = sess.terminate_status  # stopped(/stop) 或 done(/done)
                    self.store.update(sess.task_id, status=status)  # 保留历史
                    await self._safe_reply(
                        root,
                        "✅ 任务已完成并归档。"
                        if status == "done"
                        else "🛑 agent 已停止。",
                    )
                    break
                if isinstance(prompt, _BgBatch):
                    # 后台完成批次（#79）：清 pending_bg（队尾不再有可合并批次），
                    # 渲染成本轮 prompt（可能含多个 job 块）。清空须紧接 get、无 await。
                    sess.pending_bg = None
                    prompt = prompt.render()
                title = f"{sess.project_name} · {sess.agent_label}"
                model = getattr(sess.agent, "model", "") or ""
                # footer 与模型同一行显示项目名（#44）：在任意输出单元都可辨归属
                footer = sess.project_name
                if model:
                    footer += f" · 模型：{model}"
                issue_tag = _issue_tag(sess.issue_url)  # 绑定了 issue 则标 · #N（#63）
                if issue_tag:
                    footer += f" · {issue_tag}"
                assert self._channel is not None
                output = self._channel.open_output(root, title, footer=footer)
                sess.current_output = output
                self.store.update(sess.task_id, status="running")
                logger.info(
                    "任务 %s 开始一轮（%s）: %.80s",
                    sess.task_id,
                    sess.agent_label,
                    prompt,
                )
                sess.turn_in_flight = True
                try:
                    stop_reason = await sess.agent.prompt(prompt)
                    await output.flush()
                    if not self._runners.is_current(sess.task_id, sess):
                        break
                    if stop_reason == "cancelled":
                        # 本轮被 /stop 中途取消：不当作正常完成（不 ✅、不计 turn、
                        # 不发完成通知）。输出置停止态；随后循环取到 None 哨兵即终止。
                        await output.set_status("stopped")
                        if not self._runners.is_current(sess.task_id, sess):
                            break
                        self.store.update(sess.task_id, status="idle")
                        logger.info("任务 %s 本轮被取消", sess.task_id)
                        continue
                    # footer 追加本轮 token 用量（#53）：取不到就不显示、不报错。
                    # 只标脏，紧随的 set_status("done") 会把新 footer 一起 emit。
                    tokens = getattr(sess.agent, "last_usage_tokens", None)
                    if tokens is not None:
                        output.set_footer(_with_tokens(footer, tokens))
                    await output.set_status("done")
                    if not self._runners.is_current(sess.task_id, sess):
                        break
                    # 落 last_output：本轮 agent 的收尾回复（截断），供 get_task/通知摘要
                    last_output = _clip(sess.agent.last_message, _LAST_OUTPUT_MAX)
                    cur = self.store.get(sess.task_id)
                    turns = (cur.turns if cur else 0) + 1
                    logger.info(
                        "任务 %s 完成第 %d 轮，回复 %d 字",
                        sess.task_id,
                        turns,
                        len(last_output),
                    )
                    self.store.update(
                        sess.task_id,
                        status="idle",
                        turns=turns,
                        last_output=last_output,
                        error_message="",  # 一轮成功即清掉上次异常诊断（恢复成功）
                    )
                    await self._safe_reply(
                        root, "✅ 本轮结束（可继续回复；发送 `/stop` 结束该 agent）"
                    )
                    # 完成且已闲下来（无排队）→ 推一条主线通知（带收尾摘要），免得挨个点话题
                    if (
                        self._runners.is_current(sess.task_id, sess)
                        and sess.queue.empty()
                    ):
                        note = f"🔔 {sess.project_name} 完成第 {turns} 轮"
                        snippet = _one_line(last_output, 80)
                        if snippet:
                            note += f"：{snippet}"
                        note += "，在其话题里查看/继续。"
                        await self._notify_main(note)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("agent 执行异常")
                    err = _clip(f"{type(exc).__name__}: {exc}", _ERROR_MSG_MAX)
                    try:
                        await output.set_status("error")
                    except Exception:
                        logger.debug("set_status error 失败（忽略）", exc_info=True)
                    if self._runners.is_current(sess.task_id, sess):
                        # failed 不再是终止态：本轮失败但 session 已建，多半能 load_session
                        # 接回——标 failed（可恢复），话题回复即尝试恢复，而非逼用户重开丢上下文。
                        self.store.update(
                            sess.task_id, status="failed", error_message=err
                        )
                        await self._safe_reply(
                            root,
                            f"❌ 本轮异常，已暂停：{err}\n"
                            "在话题回复即尝试恢复（load_session 接回上下文），或 `/stop` 结束。",
                        )
                        await self._notify_main(
                            f"❌ {sess.project_name} 本轮异常，已暂停（在其话题回复即尝试恢复）。"
                        )
                    break
                finally:
                    sess.turn_in_flight = False
                    await output.aclose()
                    sess.current_output = None
        except asyncio.CancelledError:
            logger.debug("agent worker 被取消 root=%s", root)
        finally:
            await self._close_session(sess)

    async def _close_session(self, sess: _AgentSession) -> None:
        """收尾 runner：仅按 identity 移除自身槽位，但始终关闭自身资源。"""
        self._runners.remove_if_current(sess.task_id, sess)
        if sess.bg_token:  # 作废该 session 的后台任务 token（#68）
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

    async def _cancel_turn(self, sess: _AgentSession) -> None:
        """协作式取消 session 当前在途的 turn（ACP session/cancel）。失败不致命。"""
        agent = sess.agent
        if agent is None:
            return
        try:
            await agent.cancel()
            logger.info("已请求取消任务 %s 的当前轮", sess.task_id)
        except Exception:
            logger.exception("取消当前轮失败 task=%s", sess.task_id)

    async def _forward_to_agent(self, msg: ChannelMessage) -> None:
        """话题内回复 → 入队给对应 agent；agent 不在则尝试跨重启恢复。"""
        thread_root = msg.thread_id or ""
        text = msg.text.strip()
        # /help 先于 session 检查：不依赖 agent 是否在线（挂起的话题里也能查用法），
        # 且绝不入队 / 触发恢复。
        if text in _HELP_CMDS:
            await self._safe_reply(thread_root or msg.message_id, _THREAD_USAGE)
            return
        # /raw <文本>：把 <文本> 逐字转发给 agent，绕过下面所有话题命令（/stop、/model…）
        # 的解释——用来给 coding agent 发它自己的、恰好与保留名撞车的 slash 指令。剥掉
        # 前缀后走与普通消息完全相同的路径（含 session 恢复），只是不再匹配保留命令。
        forward_raw = False
        if text == _RAW_CMD or text.startswith(_RAW_CMD + " "):
            text = text[len(_RAW_CMD) :].strip()
            if not text:
                await self._safe_reply(
                    thread_root or msg.message_id,
                    "用法：`/raw <指令>` —— 把 <指令> 原样发给 agent（如 `/raw /model`）。",
                )
                return
            forward_raw = True
        task = self.store.by_thread(thread_root)
        sess = self._runners.get_for_task(task.task_id) if task is not None else None
        if sess is None:
            # Thread 只负责路由到 Task；无 current runner 时再按 Task 恢复或明确提示。
            await self._recover_or_notify(
                thread_root or msg.message_id,
                thread_root,
                text,
                forward_raw=forward_raw,
                task=task,
            )
            return
        if not text:
            return
        if sess.worker is None or sess.worker.done():
            await self._safe_reply(
                thread_root or msg.message_id,
                "⚠️ 该 agent 已结束。发送 `/run ...` 新建任务。",
            )
            return
        if forward_raw:
            sess.enqueue(text)  # 逐字直传，跳过保留命令解释
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
                    sess.enqueue(new_input)
                await self._cancel_turn(sess)
                await self._safe_reply(
                    thread_root or msg.message_id,
                    "🛑 已取消当前轮，改执行新指令…"
                    if new_input
                    else "🛑 已取消当前轮（agent 保留，可继续发指令）。",
                )
            elif new_input:
                # 无在途轮：没什么可取消，新输入当普通消息执行
                sess.enqueue(new_input)
            else:
                await self._safe_reply(
                    thread_root or msg.message_id, "当前没有在跑的轮，无需取消。"
                )
            return
        if text == _DONE_CMD:
            self._finish_task(sess.task_id, "done")  # 优雅收尾，worker 发完成消息
            return
        if text == _MODEL_CMD or text.startswith(_MODEL_CMD + " "):
            await self._handle_model_cmd(sess, thread_root, text)
            return
        sess.enqueue(text)

    async def _handle_model_cmd(
        self, sess: _AgentSession, reply_target: str, text: str
    ) -> None:
        """`/model` 列出当前+可选模型；`/model <名>` 切换（ACP set_config_option）。

        对下一轮生效。agent 不暴露模型选项（如 copilot）则提示不支持。
        """
        agent = sess.agent
        models = list(getattr(agent, "available_models", []) or [])
        current = getattr(agent, "model", "") or ""
        if not models:
            await self._safe_reply(
                reply_target, "⚠️ 该 agent 不支持切换模型（未通过 ACP 暴露模型选项）。"
            )
            return
        arg = text[len(_MODEL_CMD) :].strip()
        if not arg:  # 裸 /model → 列出
            lines = [
                f"当前模型：{current or '未知'}",
                "可切换（发 `/model <完整名>`）：",
            ]
            lines += [f"• {m}" for m in models]
            await self._safe_reply(reply_target, "\n".join(lines))
            return
        if arg not in models:
            await self._safe_reply(
                reply_target, f"⚠️ 未知模型 '{arg}'。发 `/model` 查看可选列表。"
            )
            return
        try:
            await agent.set_model(arg)
        except Exception as exc:
            logger.exception("切换模型失败 task=%s model=%s", sess.task_id, arg)
            if self._runners.is_current(sess.task_id, sess):
                await self._safe_reply(
                    reply_target, f"❌ 切换模型失败：{str(exc)[:200]}"
                )
            return
        if not self._runners.is_current(sess.task_id, sess):
            return
        self.store.update(sess.task_id, model=arg)
        logger.info("任务 %s 切换模型 → %s", sess.task_id, arg)
        await self._safe_reply(reply_target, f"✅ 已切换模型为 {arg}（下一轮起生效）。")

    async def _recover_or_notify(
        self,
        reply_target: str,
        thread_root: str,
        text: str,
        *,
        forward_raw: bool = False,
        task: Task | None = None,
    ) -> None:
        """话题无活跃 agent：能恢复的 Task 就 load_session 惰性重连，否则明确提示。

        ``forward_raw``（来自 ``/raw <文本>``）时跳过 ``/stop``/``/done`` 解释——恢复
        agent 后把 <文本> 当普通首轮转发，即使它恰好是 ``/stop`` 也不误当停止命令。
        """
        task = task or self.store.by_thread(thread_root)
        if task is None:
            await self._safe_reply(
                reply_target,
                "⚠️ 该话题没有对应任务（可能从未启动）。发送 `/run` 新建任务。",
            )
            return
        if task.is_terminal:
            await self._safe_reply(
                reply_target,
                f"⚠️ 任务 [{task.task_id}] 已结束（{task.status}）。发送 `/run` 新开一个。",
            )
            return
        if not forward_raw and text == _STOP_CMD:
            self.store.update(task.task_id, status="stopped")
            await self._safe_reply(reply_target, f"🛑 任务 [{task.task_id}] 已结束。")
            return
        if not forward_raw and text == _DONE_CMD:
            self.store.update(task.task_id, status="done")
            await self._safe_reply(
                reply_target, f"✅ 任务 [{task.task_id}] 已完成并归档。"
            )
            return
        if not text:
            return  # 空回复不触发恢复
        ok, why = self._try_resume(task, first_prompt=text)
        if not ok:
            await self._safe_reply(reply_target, why)
            return
        await self._safe_reply(reply_target, f"♻️ 正在恢复任务 [{task.task_id}]…")

    def _try_resume(self, task: Task, *, first_prompt: str | None) -> tuple[bool, str]:
        """把一个非活跃任务 load_session 惰性重连；返回 (成功, 失败文案)。

        check（agent 配置 / 会话 / max_agents）与 ``_launch`` 登记之间**无 await**，
        保证并发下不突破 max_agents（TOCTOU，同 _spawn_for_root）。调用点务必也别
        在 check 与本调用之间插入 await。
        """
        if self._runners.get_for_task(task.task_id) is not None:
            return False, f"任务 [{task.task_id}] 已在运行，无需恢复。"
        agent_argv = self.cfg.agents.get(task.agent_label)
        if not agent_argv or not task.session_id:
            self.store.update(task.task_id, status="failed")
            why = "agent 未配置" if not agent_argv else "无可恢复的会话"
            return False, (
                f"⚠️ 无法恢复任务 [{task.task_id}]（{why}）。发送 `/run` 重开。"
            )
        if self._runners.count() >= self.cfg.max_agents:
            return False, (
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，无法恢复。"
                "请先 `/stop` 一个再试。"
            )
        self._launch(
            task,
            agent_argv,
            first_prompt=first_prompt,
            resume_session_id=task.session_id,
        )
        return True, ""

    def _finish_task(self, task_id: str, status: str) -> bool:
        """把任务置为终止态 ``status``；有活跃 worker 则经哨兵优雅收尾，否则直接改台账。

        返回是否找到该任务。活跃时把 ``terminate_status`` 交给 worker、入队 None——
        worker 跑完当前/排队 turn 后落地状态并发完成消息（与 /stop 同机制）。
        """
        task = self.store.get(task_id)
        if task is None:
            return False
        sess = self._runners.get_for_task(task.task_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            sess.terminate_status = status
            sess.terminate()  # 丢弃未处理 bg 批次 + 入队 None（#79，与 /stop 同机制）
        else:
            self.store.update(task_id, status=status)
        return True

    async def _list_agents(self, msg: ChannelMessage) -> None:
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
                    f"• [{t.task_id}] {t.project_name} · {t.status}"
                    f"（{t.turns} 轮）：{t.description[:24]}"
                    for t in active
                )
            )
        if paused:
            parts.append(
                "⚠️ 异常暂停（在话题回复即尝试恢复，或 `/stop` 结束）:\n"
                + "\n".join(
                    f"• [{t.task_id}] {t.project_name}：{t.error_message or '本轮异常'}"
                    for t in paused
                )
            )
        if terminal:
            parts.append(
                "历史（近 5）:\n"
                + "\n".join(
                    f"• [{t.task_id}] {t.project_name} · {t.status}：{t.description[:24]}"
                    for t in terminal[-5:]
                )
            )
        await self._reply_user(
            msg.message_id, "\n\n".join(parts) if parts else "当前无任务。"
        )

    async def _show_task(self, msg: ChannelMessage, task_id: str) -> None:
        """`/task <id>`：任务详情 + 最近动作日志（审计 A 的人读入口，无需 LLM）。"""
        t = self.store.get(task_id)
        if t is None:
            await self._reply_user(
                msg.message_id, f"未找到任务 {task_id}。用 `/agents` 查看有哪些任务。"
            )
            return
        head = (
            f"[{t.task_id}] {t.project_name} · {t.agent_label} · {t.status}"
            f"（{t.turns} 轮）"
        )
        if t.model:
            head += f"\n模型: {t.model}"
        lines = [head, f"任务: {t.description}"]
        if t.origin == "attach":
            lines.append(f"来源: 附着外部会话（session: {_short_sid(t.session_id)}）")
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
        await self._reply_user(msg.message_id, "\n".join(lines))

    async def _reboot(self, msg: ChannelMessage) -> None:
        """`/reboot`：优雅关停后由 cli.py re-exec 重启整个 daemon 进程。

        先发回执再置位（之后 WS 会断）；活跃任务由 `_shutdown` 标 suspended、
        重启后可 `load_session` 恢复，不丢上下文。"""
        await self._reply_user(
            msg.message_id, "🔄 正在重启 daemon…（十几秒后回来，任务会自动恢复）"
        )
        logger.info("收到 /reboot，准备重启 daemon")
        self._reboot_requested = True
        if self._stop_event is not None:
            self._stop_event.set()

    # ------------------------------------------------------------------ #
    # P2：调度器 LLM（自然语言派发）
    # ------------------------------------------------------------------ #

    async def _dispatch_nl(self, msg: ChannelMessage, text: str) -> None:
        """自然语言 → 调度器 LLM 理解并调用工具派发（P2）。"""
        assert self._llm is not None
        tools = build_scheduler_tools(
            list_projects=self._sched_list_projects,
            spawn_agent=self._sched_spawn_agent,
            list_tasks=self._sched_list_tasks,
            get_task=self._sched_get_task,
            send_to_task=self._sched_send_to_task,
            resume_task=self._sched_resume_task,
            mark_done=self._sched_mark_done,
            register_project=self._sched_register_project,
            unregister_project=self._sched_unregister_project,
            attach_session=self._sched_attach_session,
            list_forge=self._sched_list_forge,
            get_forge=self._sched_get_forge,
            list_models=self._sched_list_models,
        )
        turn: list[dict] | None = None
        try:
            reply, turn = await run_tool_loop(
                self._llm, text, tools, history=self._sched_memory.history()
            )
        except Exception as exc:
            logger.exception("调度器 LLM 失败")
            reply = (
                f"调度器出错：{str(exc)[:200]}。可用 `/run <项目> <任务>` 直接派发。"
            )
        reply = reply or "（调度器无输出）"
        # 无损记忆：存整轮（含真实 tool_calls/结果），避免只存文本训练出「说了不做」的幻觉
        if turn:
            self._sched_memory.add_turn(turn)
        else:
            self._sched_memory.add_exchange(text, reply)  # 出错兜底：至少存问答对
        await self._reply_user(msg.message_id, reply)

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

    def _sched_list_tasks(self) -> list[dict]:
        # 从任务台账读（含历史），而非只看内存里的活跃 session
        return [
            {
                "task_id": t.task_id,
                "project": t.project_name,
                "agent": t.agent_label,
                "description": t.description,
                "status": t.status,
                "turns": t.turns,
                "issue_url": t.issue_url,  # 关联的 issue（#63）；空 = 未绑定
            }
            for t in self.store.all()
        ]

    def _sched_get_task(self, task_id: str) -> dict | None:
        """get_task 工具：单任务详情 + 动作审计（回答「这个 agent 都干了啥」）。"""
        t = self.store.get(task_id)
        if t is None:
            return None
        return {
            "task_id": t.task_id,
            "project": t.project_name,
            "agent": t.agent_label,
            "description": t.description,
            "status": t.status,
            "turns": t.turns,
            "has_session": bool(t.session_id),
            "origin": t.origin,  # 会话来源 spawn/attach
            "active": self._runners.get_for_task(t.task_id) is not None,
            "model": t.model,  # agent 当前模型（copilot 不暴露则为空）
            "issue_url": t.issue_url,  # 关联的 issue（#63）；空 = 未绑定
            "created_at": t.created_at,
            "updated_at": t.updated_at,
            "last_output": t.last_output,  # 最近一轮 agent 的收尾回复
            "error_message": t.error_message,  # failed 时的诊断（供判断重试/新开）
            "action_count": len(t.actions),
            "recent_actions": t.actions[-30:],  # 审计 A：agent 调过的工具
        }

    async def _sched_send_to_task(self, task_id: str, message: str) -> str:
        """send_to_task 工具：把消息路由给已有任务的 agent（在跑排队；挂起先恢复）。"""
        task = self.store.get(task_id)
        if task is None:
            return f"未找到任务 {task_id}（用 list_tasks 查看现有任务）。"
        sess = self._runners.get_for_task(task.task_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            sess.enqueue(message)
            logger.info(
                "send_to_task[%s] 入队（活跃 session，队列深度=%d，task.status=%s）",
                task_id,
                sess.queue.qsize(),
                task.status,
            )
            return f"已把消息转达给任务 [{task_id}]（{task.project_name}），排队执行。"
        if task.is_terminal:
            logger.info(
                "send_to_task[%s] 拒绝：任务已终止 status=%s", task_id, task.status
            )
            return (
                f"任务 [{task_id}] 已是终止态（{task.status}），未自动恢复。"
                f"如需继续，请先 resume_task({task_id})。"
            )
        # 非活跃且可恢复：load_session 惰性重连，把消息作为首轮。check→launch 无 await。
        ok, why = self._try_resume(task, first_prompt=message)
        logger.info(
            "send_to_task[%s] 非活跃 status=%s → 恢复%s",
            task_id,
            task.status,
            "成功" if ok else f"失败（{why}）",
        )
        return f"已恢复任务 [{task_id}] 并转达消息。" if ok else why

    async def _sched_resume_task(self, task_id: str) -> str:
        """resume_task 工具：显式恢复挂起/已结束的任务（load_session），仅拉起不跑首轮。"""
        task = self.store.get(task_id)
        if task is None:
            return f"未找到任务 {task_id}（用 list_tasks 查看现有任务）。"
        sess = self._runners.get_for_task(task.task_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            return f"任务 [{task_id}] 已在运行，无需恢复。"
        ok, why = self._try_resume(task, first_prompt=None)
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
    ) -> str:
        """attach_session 工具：附着 daemon 外部的 agent 会话为新 Task（与 /attach 共用底层）。

        ``agent`` 可选：非空则覆盖项目 default_agent（须在 [agents]），空则用默认。
        仅新建；重复 (agent, session_id) 由底层 :meth:`_attach_task` 去重拒绝。
        """
        _task, _root, message = await self._attach_task(
            project_name,
            agent,
            session_id,
            description,
            conversation=ConversationRef(self._channel_key, self.cfg.chat_id),
        )
        return message

    async def _sched_spawn_agent(
        self,
        project_name: str,
        task: str,
        agent: str = "",
        issue: int = 0,
        model: str = "",
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
        if self._runners.count() >= self.cfg.max_agents:
            return f"已达并发上限 {self.cfg.max_agents}，请先 `/stop` 一个再派发。"
        assert self._channel is not None
        # 每个派发新建一个话题根消息，agent 输出流进该话题
        header = f"🚀 {agent_label} · {project_name}\n任务: {task}"
        if issue_url:
            header += f"\nissue: {issue_url}"
        root = await asyncio.to_thread(
            self._channel.create_thread, self.cfg.chat_id, header
        )
        new_task = self.store.create(
            project_name=project_name,
            agent_label=agent_label,
            description=task,
            conversation=ConversationRef(self._channel_key, self.cfg.chat_id),
            thread_root_id=root,
            workspace=str(project.path),
            issue_url=issue_url,
            model=model,
        )
        self._launch(new_task, agent_argv, first_prompt=brief)
        bound = f"（brief 来自 issue {issue_url}）" if issue_url else note
        return (
            f"已建任务 [{new_task.task_id}]，在项目 {project_name} 启动 "
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
            argv = [_resolve_executable(command[0]), *command[1:]]
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
        await self._safe_reply(task.thread_root_id, self._bg_result_message(job, rc))
        sess = self._runners.get_for_task(task.task_id)
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
        ok, why = self._try_resume(task, first_prompt=self._build_bg_prompt(job, rc))
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

    async def _safe_reply(
        self, message_id: str, text: str, *, in_thread: bool = True
    ) -> None:
        """发消息但吞掉异常（只记录日志），避免一条失败拖垮 daemon。

        ``in_thread=True``（默认）用于 agent 话题内的输出/状态；``in_thread=False``
        用于对用户对话/命令的普通回复——**不创建话题**（只有派发 agent 才建话题）。
        """
        assert self._channel is not None
        try:
            await asyncio.to_thread(
                self._channel.reply_text,
                message_id,
                text,
                threaded=in_thread,
            )
        except Exception:
            logger.exception("飞书发送失败 msg=%s", message_id)

    async def _reply_user(self, message_id: str, text: str) -> None:
        """对用户对话/命令消息的普通回复（不建话题）。"""
        await self._safe_reply(message_id, text, in_thread=False)

    async def _notify_main(self, text: str) -> None:
        """向控制台主线推一条独立通知（不建话题）——agent 完成/出错/挂起时用。"""
        if not self.cfg.chat_id or self._channel is None:
            return
        try:
            await asyncio.to_thread(self._channel.send_text, self.cfg.chat_id, text)
        except Exception:
            logger.exception("主线通知发送失败")

    async def _shutdown(self) -> None:
        """退出清理：停 WS 线程，取消并等待全部 agent worker 收尾。"""
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
        if self._channel is not None:
            self._channel.stop()
        # 把仍活跃的任务标记为 suspended，让重启后台账状态准确（且可 load_session 恢复）
        for sess in self._runners.values():
            task = self.store.get(sess.task_id)
            if task is not None and not task.is_terminal:
                self.store.update(sess.task_id, status="suspended")
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
