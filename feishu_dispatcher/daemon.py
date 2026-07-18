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
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

from pathlib import Path

from .acp_client import AcpAgent, AgentSpawn, OnOutput
from .config import DEFAULT_CONFIG_PATH, Config
from .feishu import FeishuBridge, IncomingMessage
from .llm import build_llm_client
from .scheduler import (
    LLMClient,
    SchedulerMemory,
    build_scheduler_tools,
    run_tool_loop,
)
from .store import SessionRecord, SessionStore

logger = logging.getLogger(__name__)

_DISPATCH_PREFIX = "/run "
_LIST_CMD = "/agents"
_STOP_CMD = "/stop"

#: message_id 去重窗口大小（飞书 ACK 异常时服务端会重推事件）
_DEDUP_CAPACITY = 512

_USAGE = (
    "用法：\n"
    "• `/run <项目名> <任务描述>`  派发任务给 agent\n"
    "• `/agents`  列出活跃 agent\n"
    "• 在 agent 话题内直接回复 = 追加指令（排队串行执行）\n"
    "• 在 agent 话题内发 `/stop` = 结束该 agent"
)


async def run(
    cfg: Config, *, discover: bool = False, store_path: Path | None = None
) -> None:
    """启动 daemon：飞书 WS 长连接 + agent 调度。阻塞直到收到退出信号。

    ``discover=True`` 时只打印收到消息的 chat_id，不执行任何命令
    （帮助用户发现群 id 后填进配置）。``store_path`` 是会话持久化文件
    （默认 config 同目录的 sessions.json）。
    """
    if store_path is None:
        store_path = DEFAULT_CONFIG_PATH.parent / "sessions.json"
    daemon = _Daemon(
        cfg,
        discover=discover,
        store=SessionStore(store_path),
        _sched_memory=SchedulerMemory(store_path.parent / "scheduler_memory.json"),
    )
    await daemon.run()


@dataclass
class _AgentSession:
    """一个活跃 agent 的运行时状态。"""

    thread_root_id: str
    project_name: str
    agent_label: str
    #: agent 工作目录（持久化 + resume 用）
    cwd: str = ""
    #: 是否由 load_session 恢复而来（影响启动失败时的提示文案）
    resumed: bool = False
    #: 运行态：starting / running（跑一轮中）/ idle（等回复）/ error
    state: str = "starting"
    #: 已完成的回合数
    turns: int = 0
    #: agent 实例（先建 session、再建 agent，故允许 None）
    agent: "AcpAgent | None" = None
    #: 当前回合的输出通道（card 或 text 模式）；回合间为 None
    current_channel: "object | None" = None
    #: prompt 队列；None 是关闭哨兵（/stop）
    queue: "asyncio.Queue[str | None]" = field(default_factory=asyncio.Queue)
    #: 单消费者 worker，持有 agent 完整生命周期
    worker: "asyncio.Task[None] | None" = None


@dataclass
class _Daemon:
    cfg: Config
    discover: bool = False
    #: 会话持久化（默认纯内存，不写盘）；run() 会注入文件版
    store: SessionStore = field(default_factory=lambda: SessionStore(None))
    #: 调度器 LLM（P2）；None = 不启用自然语言派发。run() 按 cfg.llm 构造；测试可注入
    _llm: LLMClient | None = None
    #: 调度器主线对话记忆（跨重启持久化）；默认纯内存，run() 注入文件版
    _sched_memory: SchedulerMemory = field(
        default_factory=lambda: SchedulerMemory(None)
    )
    _bridge: FeishuBridge | None = None
    _sessions: dict[str, _AgentSession] = field(default_factory=dict)
    _seen_message_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)

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
        )
        self._bridge.start_background()
        logger.info(
            "feishu-dispatcher daemon 已启动（调度器 LLM: %s），等待飞书消息…",
            "on" if self._llm else "off",
        )
        try:
            # R13：看门狗——每 30s 醒一次检查 WS 线程是否存活；
            # 死了则 error 日志 + bridge.restart() 重启（重启前确认未在退出）
            while True:
                try:
                    await asyncio.wait_for(asyncio.Event().wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass  # 正常：每 30s 醒来检查一次
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
        elif text == _LIST_CMD:
            await self._list_agents(msg)
        elif text in ("/help", "/?", "/usage"):
            await self._reply_user(msg.message_id, _USAGE)
        elif self._llm is not None and text and not text.startswith("/"):
            # P2：自然语言交给调度器 LLM 理解并派发（未配置 LLM 则回退到用法）
            await self._dispatch_nl(msg, text)
        else:
            await self._reply_user(msg.message_id, _USAGE)

    async def _spawn_for_root(self, msg: IncomingMessage, body: str) -> None:
        """解析 ``/run <project> <task>``，创建 agent session 并启动 worker。"""
        parts = body.split(maxsplit=1)
        if len(parts) < 2:
            await self._reply_user(msg.message_id, "格式：`/run <项目名> <任务描述>`")
            return
        project_name, task = parts[0].strip(), parts[1].strip()
        project = self.cfg.projects.get(project_name)
        if project is None:
            known = ", ".join(self.cfg.projects) or "(无)"
            await self._reply_user(
                msg.message_id, f"未知项目 '{project_name}'。已知项目: {known}"
            )
            return
        agent_argv = self.cfg.agents.get(project.default_agent)
        if not agent_argv:
            await self._reply_user(
                msg.message_id,
                f"项目 '{project_name}' 的 agent '{project.default_agent}' 未配置",
            )
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

        self._launch(
            thread_root=thread_root,
            project_name=project_name,
            agent_label=project.default_agent,
            cwd=str(project.path),
            agent_argv=agent_argv,
            first_prompt=task,
        )
        await self._safe_reply(
            thread_root,
            f"🚀 启动 {project.default_agent} 处理项目 {project_name}…\n任务: {task}",
        )

    def _make_agent(
        self,
        spawn: AgentSpawn,
        on_output: OnOutput,
        *,
        resume_session_id: str | None = None,
    ) -> AcpAgent:
        """构造底层 agent（拆出来是测试注入点）。"""
        return AcpAgent(spawn, on_output, resume_session_id=resume_session_id)

    def _launch(
        self,
        *,
        thread_root: str,
        project_name: str,
        agent_label: str,
        cwd: str,
        agent_argv: list[str],
        first_prompt: str,
        resume_session_id: str | None = None,
    ) -> _AgentSession:
        """建 session、接线 on_output、入队首条 prompt、启动 worker。

        ``resume_session_id`` 非 None 时 agent 用 load_session 恢复（惰性重连）。
        """
        sess = _AgentSession(
            thread_root_id=thread_root,
            project_name=project_name,
            agent_label=agent_label,
            cwd=cwd,
            resumed=resume_session_id is not None,
        )

        async def on_output(text: str) -> None:
            if sess.current_channel is not None:
                sess.current_channel.feed(text)

        sess.agent = self._make_agent(
            AgentSpawn(command=list(agent_argv), cwd=cwd),
            on_output,
            resume_session_id=resume_session_id,
        )
        sess.queue.put_nowait(first_prompt)
        self._sessions[thread_root] = sess
        sess.worker = asyncio.create_task(
            self._agent_worker(sess), name=f"agent-{thread_root}"
        )
        return sess

    def _make_channel(self, root: str, title: str):
        """按 cfg.stream_mode 创建输出通道。

        card 模式返回 LiveCard（原地更新卡片），text 模式返回 StreamThrottler
        （每批发新消息，兜底）。
        """
        if self.cfg.stream_mode == "card":
            from .livecard import LiveCard

            return LiveCard(self._bridge, root, title)
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
            if sess.resumed:
                # 恢复失败：agent 侧会话多半已过期，丢弃记录并提示重开
                self.store.remove(root)
                await self._safe_reply(
                    root, "❌ 会话恢复失败（可能已在 agent 侧过期）。发送 `/run` 重开。"
                )
            else:
                await self._safe_reply(root, f"❌ agent 启动失败: {str(exc)[:200]}")
            await self._close_session(sess)
            return
        # 启动成功：落盘会话映射，供 daemon 重启后 load_session 恢复（幂等）
        self.store.put(
            SessionRecord(
                thread_root_id=root,
                project_name=sess.project_name,
                agent_label=sess.agent_label,
                session_id=sess.agent.session_id or "",
                cwd=sess.cwd,
            )
        )
        await self._safe_reply(
            root,
            "♻️ 已恢复会话，继续执行…" if sess.resumed else "▶️ agent 已就绪，开始执行…",
        )
        sess.state = "idle"
        try:
            while True:
                # 空闲挂起（坑 1）：超时无新回复就关掉 agent 腾出 max_agents 名额，
                # 但**保留** sessions.json 记录（区别于 /stop 的删除）——之后在本
                # 话题回复即走 load_session 恢复。<=0 表示不自动挂起。
                timeout = self.cfg.idle_timeout if self.cfg.idle_timeout > 0 else None
                try:
                    prompt = await asyncio.wait_for(sess.queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    sess.state = "suspended"
                    await self._safe_reply(
                        root,
                        "💤 空闲超时，已挂起该 agent（在本话题回复即自动恢复）。",
                    )
                    await self._notify_main(
                        f"💤 {sess.project_name} 已空闲挂起（在其话题回复即自动恢复）。"
                    )
                    break
                if prompt is None:
                    self.store.remove(root)  # 用户显式结束，不再恢复
                    await self._safe_reply(root, "🛑 agent 已停止。")
                    break
                title = f"{sess.project_name} · {sess.agent_label}"
                channel = self._make_channel(root, title)
                sess.current_channel = channel
                sess.state = "running"
                try:
                    await sess.agent.prompt(prompt)
                    await channel.flush()
                    await channel.set_status("done")
                    sess.turns += 1
                    sess.state = "idle"
                    await self._safe_reply(
                        root, "✅ 本轮结束（可继续回复；发送 `/stop` 结束该 agent）"
                    )
                    # 完成且已闲下来（无排队）→ 推一条主线通知，免得你挨个点话题
                    if sess.queue.empty():
                        await self._notify_main(
                            f"🔔 {sess.project_name} 完成第 {sess.turns} 轮，在其话题里查看/继续。"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("agent 执行异常")
                    sess.state = "error"
                    try:
                        await channel.set_status("error")
                    except Exception:
                        logger.debug("set_status error 失败（忽略）", exc_info=True)
                    await self._safe_reply(
                        root, f"❌ agent 异常，已结束该 agent: {str(exc)[:200]}"
                    )
                    await self._notify_main(
                        f"❌ {sess.project_name} 出错，已结束该 agent。"
                    )
                    break
                finally:
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

    async def _forward_to_agent(self, msg: IncomingMessage) -> None:
        """话题内回复 → 入队给对应 agent；agent 不在则尝试跨重启恢复。"""
        thread_root = msg.thread_root_id or ""
        text = msg.text.strip()
        sess = self._sessions.get(thread_root)
        if sess is None:
            # 无活跃 agent：尝试从持久化记录恢复（惰性重连），或明确提示——
            # 不再静默忽略（那是重启后老话题回复石沉大海的根源）。
            await self._recover_or_notify(
                thread_root or msg.message_id, thread_root, text
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
        if text == _STOP_CMD:
            self.store.remove(thread_root)
            sess.queue.put_nowait(None)
            return
        sess.queue.put_nowait(text)

    async def _recover_or_notify(
        self, reply_target: str, thread_root: str, text: str
    ) -> None:
        """话题无活跃 agent：能恢复就用 load_session 惰性重连，否则明确提示用户。"""
        rec = self.store.get(thread_root)
        if rec is None:
            await self._safe_reply(
                reply_target,
                "⚠️ 该话题没有活跃 agent（可能已 `/stop` 或从未启动）。发送 `/run` 新建任务。",
            )
            return
        if text == _STOP_CMD:
            # 对孤儿话题发 /stop：直接忘记记录，不必为了停而先恢复
            self.store.remove(thread_root)
            await self._safe_reply(reply_target, "🛑 会话已结束。")
            return
        agent_argv = self.cfg.agents.get(rec.agent_label)
        if not agent_argv:
            self.store.remove(thread_root)  # agent 已不在配置，无法恢复
            await self._safe_reply(
                reply_target,
                f"⚠️ 无法恢复会话：agent '{rec.agent_label}' 未配置。发送 `/run` 重开。",
            )
            return
        if not text:
            return  # 空回复不触发恢复
        # 与 _spawn_for_root 同理：check 与 _launch 登记之间不能 await（TOCTOU）。
        if len(self._sessions) >= self.cfg.max_agents:
            await self._safe_reply(
                reply_target,
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，无法恢复会话。"
                "请先 `/stop` 一个再回复。",
            )
            return
        self._launch(
            thread_root=thread_root,
            project_name=rec.project_name,
            agent_label=rec.agent_label,
            cwd=rec.cwd,
            agent_argv=agent_argv,
            first_prompt=text,
            resume_session_id=rec.session_id,
        )
        await self._safe_reply(reply_target, "♻️ 正在恢复会话…")

    async def _list_agents(self, msg: IncomingMessage) -> None:
        lines = [
            f"• {s.project_name} (thread {s.thread_root_id}, 待执行 {s.queue.qsize()})"
            for s in self._sessions.values()
        ]
        # 已持久化但当前未激活的会话（重启后回复对应话题即自动恢复）
        dormant = [
            r for tid, r in self.store.all().items() if tid not in self._sessions
        ]
        parts: list[str] = []
        if lines:
            parts.append("活跃 agent:\n" + "\n".join(lines))
        if dormant:
            parts.append(
                f"可恢复会话 {len(dormant)} 个（回复对应话题即自动恢复）："
                + "、".join(f"{r.project_name}" for r in dormant)
            )
        await self._reply_user(
            msg.message_id, "\n\n".join(parts) if parts else "当前无活跃 agent。"
        )

    # ------------------------------------------------------------------ #
    # P2：调度器 LLM（自然语言派发）
    # ------------------------------------------------------------------ #

    async def _dispatch_nl(self, msg: IncomingMessage, text: str) -> None:
        """自然语言 → 调度器 LLM 理解并调用工具派发（P2）。"""
        assert self._llm is not None
        tools = build_scheduler_tools(
            list_projects=self._sched_list_projects,
            spawn_agent=self._sched_spawn_agent,
            list_agents=self._sched_list_agents,
        )
        try:
            reply = await run_tool_loop(
                self._llm, text, tools, history=self._sched_memory.history()
            )
        except Exception as exc:
            logger.exception("调度器 LLM 失败")
            reply = (
                f"调度器出错：{str(exc)[:200]}。可用 `/run <项目> <任务>` 直接派发。"
            )
        reply = reply or "（调度器无输出）"
        self._sched_memory.add_exchange(text, reply)  # 跨重启持久化的主线记忆
        await self._reply_user(msg.message_id, reply)

    def _sched_list_projects(self) -> list[dict]:
        return [
            {"name": p.name, "default_agent": p.default_agent}
            for p in self.cfg.projects.values()
        ]

    def _sched_list_agents(self) -> list[dict]:
        active = [
            {
                "project": s.project_name,
                "agent": s.agent_label,
                "state": s.state,  # starting/running/idle/error
                "turns": s.turns,
                "queued": s.queue.qsize(),
            }
            for s in self._sessions.values()
        ]
        dormant = [
            {"project": r.project_name, "agent": r.agent_label, "state": "dormant"}
            for tid, r in self.store.all().items()
            if tid not in self._sessions
        ]
        return active + dormant

    async def _sched_spawn_agent(self, project_name: str, task: str) -> str:
        """spawn_agent 工具实现：新建话题 + 启动 agent，返回给 LLM 的状态串。"""
        project = self.cfg.projects.get(project_name)
        if project is None:
            known = ", ".join(self.cfg.projects) or "(无)"
            return f"未知项目 '{project_name}'。已注册项目: {known}"
        agent_argv = self.cfg.agents.get(project.default_agent)
        if not agent_argv:
            return f"项目 '{project_name}' 的 agent '{project.default_agent}' 未配置。"
        if len(self._sessions) >= self.cfg.max_agents:
            return f"已达并发上限 {self.cfg.max_agents}，请先 `/stop` 一个再派发。"
        assert self._bridge is not None
        # 每个派发新建一个话题根消息，agent 输出流进该话题
        root = await asyncio.to_thread(
            self._bridge.send_root_message,
            self.cfg.chat_id,
            f"🚀 {project.default_agent} · {project_name}\n任务: {task}",
        )
        self._launch(
            thread_root=root,
            project_name=project_name,
            agent_label=project.default_agent,
            cwd=str(project.path),
            agent_argv=agent_argv,
            first_prompt=task,
        )
        return f"已在项目 {project_name} 启动 {project.default_agent} 处理：{task}"

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
