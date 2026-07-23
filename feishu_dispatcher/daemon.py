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
from collections import OrderedDict
from dataclasses import dataclass, field

from pathlib import Path

from . import forge
from .acp_client import AcpAgent, AgentSpawn, OnAction, OnOutput
from .config import DEFAULT_CONFIG_PATH, Config, Project
from .feishu import FeishuBridge, IncomingMessage
from .llm import build_llm_client
from .scheduler import (
    LLMClient,
    SchedulerMemory,
    build_scheduler_tools,
    run_tool_loop,
)
from .store import ProjectStore, Task, TaskStore

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
_REBOOT_CMD = "/reboot"  # root：重启整个 daemon 进程（cli.py re-exec）
_HELP_CMDS = ("/help", "/?", "/usage")  # root 与话题内通用

#: 环境变量：re-exec 重启时置位，新进程据此发「已重启」回执
_REBOOTED_ENV = "FEISHU_DISPATCHER_REBOOTED"

#: message_id 去重窗口大小（飞书 ACK 异常时服务端会重推事件）
_DEDUP_CAPACITY = 512

_USAGE = (
    "用法：\n"
    "• `/run <项目名> <任务描述> [--agent <名>]`  派发任务给 agent（可选覆盖默认 agent）\n"
    "• `/agents`  列出活跃 + 历史任务\n"
    "• `/task <任务id>`  查看某任务详情与动作日志\n"
    "• `/project`  列出项目；`/project add <名> <agent> <路径>` 注册，`/project remove <名>` 删除\n"
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
    cfg: Config, *, discover: bool = False, store_path: Path | None = None
) -> bool:
    """启动 daemon：飞书 WS 长连接 + agent 调度。阻塞直到收到退出信号。

    ``discover=True`` 时只打印收到消息的 chat_id，不执行任何命令
    （帮助用户发现群 id 后填进配置）。``store_path`` 是会话持久化文件
    （默认 config 同目录的 sessions.json）。

    返回是否收到 ``/reboot``——cli.py 据此 re-exec 重启进程。
    """
    if store_path is None:
        store_path = DEFAULT_CONFIG_PATH.parent / "sessions.json"
    daemon = _Daemon(
        cfg,
        discover=discover,
        store=TaskStore(store_path.parent / "tasks.json"),
        project_store=ProjectStore(store_path.parent / "projects.json"),
        _sched_memory=SchedulerMemory(
            store_path.parent / "scheduler_memory.json",
            # [llm].memory_rounds 可配；未配 [llm] 时记忆不参与派发，取默认即可
            max_turns=cfg.llm.memory_rounds if cfg.llm else 12,
        ),
    )
    await daemon.run()
    return daemon._reboot_requested


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
    #: agent 实例（先建 session、再建 agent，故允许 None）
    agent: "AcpAgent | None" = None
    #: 当前回合的输出通道（card 或 text 模式）；回合间为 None
    current_channel: "object | None" = None
    #: prompt 队列；None 是关闭哨兵（/stop / /done / mark_done）
    queue: "asyncio.Queue[str | None]" = field(default_factory=asyncio.Queue)
    #: 收到 None 哨兵时置入的终止态：stopped（/stop，默认）或 done（/done / mark_done）
    terminate_status: str = "stopped"
    #: 本轮是否正在跑（worker 卡在 agent.prompt() 里）；/stop 据此决定要不要发 cancel
    turn_in_flight: bool = False
    #: 单消费者 worker，持有 agent 完整生命周期
    worker: "asyncio.Task[None] | None" = None


@dataclass
class _Daemon:
    cfg: Config
    discover: bool = False
    #: 任务台账（默认纯内存，不写盘）；run() 注入文件版（tasks.json）
    store: TaskStore = field(default_factory=lambda: TaskStore(None))
    #: 运行时注册的项目台账（默认纯内存）；run() 注入文件版（projects.json）。
    #: 有效项目 = config.toml 种子（cfg.projects）+ 这里注册的，见 _all_projects
    project_store: ProjectStore = field(default_factory=lambda: ProjectStore(None))
    #: 调度器 LLM（P2）；None = 不启用自然语言派发。run() 按 cfg.llm 构造；测试可注入
    _llm: LLMClient | None = None
    #: 调度器主线对话记忆（跨重启持久化）；默认纯内存，run() 注入文件版
    _sched_memory: SchedulerMemory = field(
        default_factory=lambda: SchedulerMemory(None)
    )
    _bridge: FeishuBridge | None = None
    _sessions: dict[str, _AgentSession] = field(default_factory=dict)
    _seen_message_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)
    #: /reboot 收到后置位；run() 返回它，cli.py re-exec 重启进程
    _reboot_requested: bool = False
    #: run() 里创建的退出事件；/reboot 或退出信号 set 它跳出主循环
    _stop_event: "asyncio.Event | None" = None

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        if self._llm is None:
            self._llm = build_llm_client(self.cfg.llm)
        self._bridge = FeishuBridge(
            app_id=self.cfg.app_id,
            app_secret=self.cfg.app_secret,
            main_loop=loop,
            on_event=self._handle_message,
            chat_whitelist=self.cfg.chat_id,
            qps=self.cfg.feishu_qps,
        )
        self._stop_event = asyncio.Event()
        self._bridge.start_background()
        logger.info(
            "feishu-dispatcher daemon 已启动（调度器 LLM: %s），等待飞书消息…",
            "on" if self._llm else "off",
        )
        # re-exec 重启起来的进程：给控制台发一条「已重启」回执（HTTP，不依赖 WS）
        if os.environ.pop(_REBOOTED_ENV, None):
            await self._notify_main("✅ daemon 已重启完成。")
        try:
            # R13：看门狗——最多等 30s 或直到 _stop_event 被 set（/reboot / 退出）；
            # 超时则检查 WS 线程是否存活，死了 bridge.restart()。
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass  # 正常：每 30s 醒来检查一次
                if self._stop_event.is_set():
                    break
                if not self._bridge.is_alive():
                    logger.error("飞书 WS 线程已死亡，尝试重启…")
                    self._bridge.restart()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("收到退出信号，清理 agent…")
        finally:
            await self._shutdown()

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

    async def _handle_message(self, msg: IncomingMessage) -> None:
        """所有飞书消息的入口（在主 event loop 上）。"""
        if self.cfg.chat_id and msg.chat_id != self.cfg.chat_id:
            logger.debug("忽略非目标群消息 chat_id=%s", msg.chat_id)
            return
        # 忽略无发送者的系统消息
        if not msg.sender_id:
            return
        if self._is_duplicate(msg.message_id):
            logger.info("忽略重复消息 message_id=%s", msg.message_id)
            return
        logger.info(
            "收到消息 chat=%s msg=%s thread_root=%s text=%r",
            msg.chat_id,
            msg.message_id,
            msg.thread_root_id,
            msg.text,
        )

        # R10：discover 模式只打印 chat_id 帮助发现，不执行任何命令
        if self.discover:
            logger.info(
                "[discover] chat_id=%r sender_id=%r — 填入 config.toml 的 chat_id 即可",
                msg.chat_id,
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

        if msg.thread_root_id:
            await self._forward_to_agent(msg)
            return

        text = msg.text.strip()
        if text.startswith(_DISPATCH_PREFIX):
            await self._spawn_for_root(msg, text[len(_DISPATCH_PREFIX) :].strip())
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
        p = Path(path)
        if not p.is_dir():
            return False, f"路径不存在或不是目录：{path}"
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

    async def _handle_project_cmd(self, msg: IncomingMessage, arg: str) -> None:
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

    async def _spawn_for_root(self, msg: IncomingMessage, body: str) -> None:
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
        if thread_root in self._sessions:
            logger.info("根消息 %s 已有 agent session，忽略重复 spawn", thread_root)
            return

        # R11：并发上限检查。check 与 _launch 的登记之间不能有 await，否则两条
        # 并发 /run 会都通过检查再各自登记，突破上限（TOCTOU）。故先原子地
        # 检查+登记，再发「🚀」提示。
        if len(self._sessions) >= self.cfg.max_agents:
            await self._reply_user(
                msg.message_id,
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，请先 `/stop` 一个。",
            )
            return

        new_task = self.store.create(
            project_name=project_name,
            agent_label=agent_label,
            description=task,
            thread_root_id=thread_root,
            workspace=str(project.path),
        )
        self._launch(new_task, agent_argv, first_prompt=task)
        await self._safe_reply(
            thread_root,
            f"🚀 [{new_task.task_id}] 启动 {agent_label} 处理项目 "
            f"{project_name}…\n任务: {task}",
        )

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
        )

    def _launch(
        self,
        task: Task,
        agent_argv: list[str],
        first_prompt: str | None,
        *,
        resume_session_id: str | None = None,
    ) -> _AgentSession:
        """按 Task 建 session、接线 on_output、入队首条 prompt、启动 worker。

        ``resume_session_id`` 非 None 时 agent 用 load_session 恢复（惰性重连）。
        ``first_prompt=None`` 时只把 agent 拉起来在线（不跑首轮），用于 resume_task。
        """
        sess = _AgentSession(
            thread_root_id=task.thread_root_id,
            project_name=task.project_name,
            agent_label=task.agent_label,
            task_id=task.task_id,
            cwd=task.workspace,
            resumed=resume_session_id is not None,
        )

        async def on_output(text: str) -> None:
            if sess.current_channel is not None:
                sess.current_channel.feed(text)

        async def on_action(action: dict) -> None:
            # 审计（A）：把 agent 的 tool_call 记进 Task，标上「进行中的回合号」
            # （= 已完成回合数 + 1，回合结束时 worker 才递增 turns）。
            cur = self.store.get(sess.task_id)
            turn = (cur.turns if cur else 0) + 1
            self.store.add_action(sess.task_id, {"turn": turn, **action})

        sess.agent = self._make_agent(
            AgentSpawn(command=list(agent_argv), cwd=task.workspace),
            on_output,
            on_action,
            resume_session_id=resume_session_id,
        )
        if first_prompt is not None:
            sess.queue.put_nowait(first_prompt)
        self._sessions[task.thread_root_id] = sess
        sess.worker = asyncio.create_task(
            self._agent_worker(sess), name=f"agent-{task.task_id}"
        )
        return sess

    def _make_channel(self, root: str, title: str, footer: str = ""):
        """按 cfg.stream_mode 创建输出通道。

        card 模式返回 LiveCard（原地更新卡片，``footer`` 固定显示在卡片最下方，
        如「模型：X」），text 模式返回 StreamThrottler（每批发新消息，兜底）。
        """
        if self.cfg.stream_mode == "card":
            from .livecard import LiveCard

            return LiveCard(self._bridge, root, title, footer=footer)
        else:
            from .throttler import StreamThrottler

            return StreamThrottler(
                sink=lambda piece: self._send_piece(root, piece),
                window=self.cfg.throttle_window,
            )

    async def _agent_worker(self, sess: _AgentSession) -> None:
        """一个 agent 的完整生命周期：启动 → 串行消费 prompt 队列 → 关闭。"""
        root = sess.thread_root_id
        try:
            await sess.agent.start()
        except Exception as exc:
            logger.exception("agent 启动失败")
            err = _clip(f"{type(exc).__name__}: {exc}", _ERROR_MSG_MAX)
            self.store.update(sess.task_id, status="failed", error_message=err)
            if sess.resumed:
                await self._safe_reply(
                    root, "❌ 会话恢复失败（可能已在 agent 侧过期）。发送 `/run` 重开。"
                )
            else:
                await self._safe_reply(root, f"❌ agent 启动失败: {str(exc)[:200]}")
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
        self.store.update(
            sess.task_id,
            session_id=sess.agent.session_id or "",
            status="idle",
            model=model,
        )
        base = (
            "♻️ 已恢复会话，继续执行…" if sess.resumed else "▶️ agent 已就绪，开始执行…"
        )
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
                    self.store.update(sess.task_id, status="suspended")
                    await self._safe_reply(
                        root,
                        "💤 空闲超时，已挂起该 agent（在本话题回复即自动恢复）。",
                    )
                    await self._notify_main(
                        f"💤 {sess.project_name} 已空闲挂起（在其话题回复即自动恢复）。"
                    )
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
                title = f"{sess.project_name} · {sess.agent_label}"
                model = getattr(sess.agent, "model", "") or ""
                # footer 与模型同一行显示项目名（#44）：滚到任意卡片都可辨归属
                footer = sess.project_name
                if model:
                    footer += f" · 模型：{model}"
                channel = self._make_channel(root, title, footer=footer)
                sess.current_channel = channel
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
                    await channel.flush()
                    if stop_reason == "cancelled":
                        # 本轮被 /stop 中途取消：不当作正常完成（不 ✅、不计 turn、
                        # 不发完成通知）。卡片置停止态；随后循环取到 None 哨兵即终止。
                        await channel.set_status("stopped")
                        self.store.update(sess.task_id, status="idle")
                        logger.info("任务 %s 本轮被取消", sess.task_id)
                        continue
                    # footer 追加本轮 token 用量（#53）：取不到就不显示、不报错。
                    # 只标脏，紧随的 set_status("done") 会把新 footer 一起 emit。
                    tokens = getattr(sess.agent, "last_usage_tokens", None)
                    if tokens is not None and hasattr(channel, "set_footer"):
                        channel.set_footer(_with_tokens(footer, tokens))
                    await channel.set_status("done")
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
                    if sess.queue.empty():
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
                    # failed 不再是终止态：本轮失败但 session 已建，多半能 load_session
                    # 接回——标 failed（可恢复），话题回复即尝试恢复，而非逼用户重开丢上下文。
                    self.store.update(sess.task_id, status="failed", error_message=err)
                    try:
                        await channel.set_status("error")
                    except Exception:
                        logger.debug("set_status error 失败（忽略）", exc_info=True)
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
                    await channel.aclose()
                    sess.current_channel = None
        except asyncio.CancelledError:
            logger.debug("agent worker 被取消 root=%s", root)
        finally:
            await self._close_session(sess)

    async def _close_session(self, sess: _AgentSession) -> None:
        """收尾一个 session：出注册表、清空输出通道、关 agent 进程。"""
        self._sessions.pop(sess.thread_root_id, None)
        if sess.current_channel is not None:
            try:
                await sess.current_channel.aclose()
            except Exception:
                logger.debug("channel aclose 异常（忽略）", exc_info=True)
        if sess.agent is not None:
            try:
                await sess.agent.aclose()
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

    async def _forward_to_agent(self, msg: IncomingMessage) -> None:
        """话题内回复 → 入队给对应 agent；agent 不在则尝试跨重启恢复。"""
        thread_root = msg.thread_root_id or ""
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
        sess = self._sessions.get(thread_root)
        if sess is None:
            # 无活跃 agent：尝试从持久化记录恢复（惰性重连），或明确提示——
            # 不再静默忽略（那是重启后老话题回复石沉大海的根源）。
            await self._recover_or_notify(
                thread_root or msg.message_id,
                thread_root,
                text,
                forward_raw=forward_raw,
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
            sess.queue.put_nowait(text)  # 逐字直传，跳过保留命令解释
            return
        if text == _STOP_CMD:
            sess.queue.put_nowait(None)  # 终止信号：worker 收到即标 stopped 收尾
            # 有在途 turn 时协作式取消它，否则 None 要等整轮跑完才生效（跑偏时干瞪眼）。
            # put_nowait 在 cancel 之前：cancel 让在途 prompt() 返回后，队列里已有 None。
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
                    sess.queue.put_nowait(new_input)
                await self._cancel_turn(sess)
                await self._safe_reply(
                    thread_root or msg.message_id,
                    "🛑 已取消当前轮，改执行新指令…"
                    if new_input
                    else "🛑 已取消当前轮（agent 保留，可继续发指令）。",
                )
            elif new_input:
                # 无在途轮：没什么可取消，新输入当普通消息执行
                sess.queue.put_nowait(new_input)
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
        sess.queue.put_nowait(text)

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
            await self._safe_reply(reply_target, f"❌ 切换模型失败：{str(exc)[:200]}")
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
    ) -> None:
        """话题无活跃 agent：能恢复的 Task 就 load_session 惰性重连，否则明确提示。

        ``forward_raw``（来自 ``/raw <文本>``）时跳过 ``/stop``/``/done`` 解释——恢复
        agent 后把 <文本> 当普通首轮转发，即使它恰好是 ``/stop`` 也不误当停止命令。
        """
        task = self.store.by_thread(thread_root)
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
        agent_argv = self.cfg.agents.get(task.agent_label)
        if not agent_argv or not task.session_id:
            self.store.update(task.task_id, status="failed")
            why = "agent 未配置" if not agent_argv else "无可恢复的会话"
            return False, (
                f"⚠️ 无法恢复任务 [{task.task_id}]（{why}）。发送 `/run` 重开。"
            )
        if len(self._sessions) >= self.cfg.max_agents:
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
        sess = self._sessions.get(task.thread_root_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            sess.terminate_status = status
            sess.queue.put_nowait(None)
        else:
            self.store.update(task_id, status=status)
        return True

    async def _list_agents(self, msg: IncomingMessage) -> None:
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

    async def _show_task(self, msg: IncomingMessage, task_id: str) -> None:
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

    async def _reboot(self, msg: IncomingMessage) -> None:
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

    async def _dispatch_nl(self, msg: IncomingMessage, text: str) -> None:
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
            list_forge=self._sched_list_forge,
            get_forge=self._sched_get_forge,
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
            "active": t.thread_root_id in self._sessions,
            "model": t.model,  # agent 当前模型（copilot 不暴露则为空）
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
        sess = self._sessions.get(task.thread_root_id)
        if sess is not None and sess.worker is not None and not sess.worker.done():
            sess.queue.put_nowait(message)
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
        sess = self._sessions.get(task.thread_root_id)
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

    async def _sched_spawn_agent(
        self, project_name: str, task: str, agent: str = ""
    ) -> str:
        """spawn_agent 工具实现：建 Task + 新话题 + 启动 agent，返回给 LLM 的状态串。

        ``agent`` 可选：非空则覆盖项目 default_agent（须在 [agents]），否则用默认。
        """
        project = self._resolve_project(project_name)
        if project is None:
            known = ", ".join(self._all_projects()) or "(无)"
            return f"未知项目 '{project_name}'。已注册项目: {known}"
        agent_label, agent_argv, err = self._resolve_agent(project, agent)
        if agent_argv is None:
            return err
        if len(self._sessions) >= self.cfg.max_agents:
            return f"已达并发上限 {self.cfg.max_agents}，请先 `/stop` 一个再派发。"
        assert self._bridge is not None
        # 每个派发新建一个话题根消息，agent 输出流进该话题
        root = await asyncio.to_thread(
            self._bridge.send_root_message,
            self.cfg.chat_id,
            f"🚀 {agent_label} · {project_name}\n任务: {task}",
        )
        new_task = self.store.create(
            project_name=project_name,
            agent_label=agent_label,
            description=task,
            thread_root_id=root,
            workspace=str(project.path),
        )
        self._launch(new_task, agent_argv, first_prompt=task)
        return (
            f"已建任务 [{new_task.task_id}]，在项目 {project_name} 启动 "
            f"{agent_label} 处理：{task}"
        )

    # ------------------------------------------------------------------ #
    # 发送辅助
    # ------------------------------------------------------------------ #

    async def _send_piece(self, thread_root: str, piece: str) -> None:
        """节流器 sink：把一段文本发到话题。HTTP 是阻塞调用，放线程池。"""
        if not piece:
            return
        assert self._bridge is not None
        await asyncio.to_thread(self._bridge.reply_in_thread, thread_root, piece)

    async def _safe_reply(
        self, message_id: str, text: str, *, in_thread: bool = True
    ) -> None:
        """发消息但吞掉异常（只记录日志），避免一条失败拖垮 daemon。

        ``in_thread=True``（默认）用于 agent 话题内的输出/状态；``in_thread=False``
        用于对用户对话/命令的普通回复——**不创建话题**（只有派发 agent 才建话题）。
        """
        assert self._bridge is not None
        fn = self._bridge.reply_in_thread if in_thread else self._bridge.reply
        try:
            await asyncio.to_thread(fn, message_id, text)
        except Exception:
            logger.exception("飞书发送失败 msg=%s", message_id)

    async def _reply_user(self, message_id: str, text: str) -> None:
        """对用户对话/命令消息的普通回复（不建话题）。"""
        await self._safe_reply(message_id, text, in_thread=False)

    async def _notify_main(self, text: str) -> None:
        """向控制台主线推一条独立通知（不建话题）——agent 完成/出错/挂起时用。"""
        if not self.cfg.chat_id or self._bridge is None:
            return
        try:
            await asyncio.to_thread(
                self._bridge.send_root_message, self.cfg.chat_id, text
            )
        except Exception:
            logger.exception("主线通知发送失败")

    async def _shutdown(self) -> None:
        """退出清理：停 WS 线程，取消并等待全部 agent worker 收尾。"""
        if self._bridge is not None:
            self._bridge.stop()
        # 把仍活跃的任务标记为 suspended，让重启后台账状态准确（且可 load_session 恢复）
        for sess in list(self._sessions.values()):
            task = self.store.get(sess.task_id)
            if task is not None and not task.is_terminal:
                self.store.update(sess.task_id, status="suspended")
        workers = [
            s.worker
            for s in list(self._sessions.values())
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
        self._sessions.clear()
