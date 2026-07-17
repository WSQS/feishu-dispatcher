"""daemon 主循环：飞书消息 → ACP agent → 飞书话题 完整闭环。

P0 原型范围（设计文档）：
- 硬编码项目匹配（不做 LLM 规划）
- 单 agent（不做并发/worktree）
- 根消息触发 spawn，话题回复转发给 agent

验证目标（P0）：
1. ACP 流式输出 → 飞书实时转发链路
2. 飞书话题双向通信
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from .acp_client import AcpAgent, AgentSpawn
from .config import Config
from .feishu import FeishuBridge, IncomingMessage
from .throttler import StreamThrottler

logger = logging.getLogger(__name__)

#: 触发任务的命令前缀（P0：简单匹配，不做 LLM 规划）
_DISPATCH_PREFIX = "/run "
_REPLY_PREFIX = "/say "
_LIST_PREFIX = "/agents"


@dataclass
class _AgentTask:
    """一个活跃 agent 的运行时状态。"""

    thread_root_id: str
    project_name: str
    agent: AcpAgent
    throttler: StreamThrottler
    #: 后台 prompt 任务，用于 cancel
    task: asyncio.Task[None] | None = None


async def run(cfg: Config) -> None:
    """启动 daemon：飞书 WS 长连接 + agent 调度。

    阻塞调用，直到收到 Ctrl-C。
    """
    daemon = _Daemon(cfg)
    await daemon.run()


@dataclass
class _Daemon:
    cfg: Config
    _bridge: FeishuBridge | None = None
    _agents_by_thread: dict[str, _AgentTask] = field(default_factory=dict)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        self._bridge = FeishuBridge(
            app_id=self.cfg.app_id,
            app_secret=self.cfg.app_secret,
            main_loop=loop,
            on_event=self._handle_message,
            chat_whitelist=self.cfg.chat_id,
        )
        self._bridge.start_background()
        logger.info("feishu-dispatcher daemon 已启动，等待飞书消息…")
        # 主 loop 只需保活；WS 在后台线程，agent task 按需创建
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("收到退出信号，清理 agent…")
            await self._shutdown()

    # ------------------------------------------------------------------ #
    # 消息分发
    # ------------------------------------------------------------------ #

    async def _handle_message(self, msg: IncomingMessage) -> None:
        """所有飞书消息的入口（在主 event loop 上）。"""
        if self._bridge and self.cfg.chat_id and msg.chat_id != self.cfg.chat_id:
            logger.debug("忽略非目标群消息 chat_id=%s", msg.chat_id)
            return
        # 忽略 bot 自己发的消息（sender_id 为空通常是系统消息）
        if not msg.sender_id:
            return
        logger.info(
            "收到消息 chat=%s msg=%s thread_root=%s text=%r",
            msg.chat_id, msg.message_id, msg.thread_root_id, msg.text,
        )

        if msg.thread_root_id:
            # 话题内回复 → 转发给对应 agent
            await self._forward_to_agent(msg)
            return

        # 根消息 → 命令解析
        text = msg.text.strip()
        if text.startswith(_DISPATCH_PREFIX):
            await self._spawn_for_root(msg, text[len(_DISPATCH_PREFIX):].strip())
        elif text == _LIST_PREFIX:
            await self._list_agents(msg)
        else:
            await self._safe_reply(
                msg.message_id,
                "用法：\n"
                "• `/run <项目名> <任务描述>`  派发任务给 agent\n"
                "• `/agents`  列出活跃 agent\n"
                "• 在 agent 话题内直接回复即可与 agent 对话",
            )

    async def _spawn_for_root(self, msg: IncomingMessage, body: str) -> None:
        """解析 ``/run <project> <task>``，spawn agent 并创建话题。"""
        assert self._bridge is not None
        parts = body.split(maxsplit=1)
        if len(parts) < 2:
            await self._safe_reply(
                msg.message_id, "格式：`/run <项目名> <任务描述>`"
            )
            return
        project_name, task = parts[0].strip(), parts[1].strip()
        project = self.cfg.projects.get(project_name)
        if project is None:
            known = ", ".join(self.cfg.projects) or "(无)"
            await self._safe_reply(
                msg.message_id, f"未知项目 '{project_name}'。已知项目: {known}"
            )
            return
        agent_argv = self.cfg.agents.get(project.default_agent)
        if not agent_argv:
            await self._safe_reply(
                msg.message_id,
                f"项目 '{project_name}' 的 agent '{project.default_agent}' 未配置",
            )
            return

        # 创建话题：用根消息 message_id 作为 thread root + 路由 key
        thread_root = msg.message_id
        await self._safe_reply(
            thread_root,
            f"🚀 启动 {project.default_agent} 处理项目 {project_name}…\n任务: {task}",
        )

        async def on_output(text: str) -> None:
            await throttler.feed(text)

        throttler = StreamThrottler(
            sink=lambda piece: self._send_piece(thread_root, piece),
            window=self.cfg.throttle_window,
        )
        spawn = AgentSpawn(
            command=list(agent_argv),
            cwd=str(project.path),
        )
        agent = AcpAgent(spawn, on_output)

        task_obj = _AgentTask(
            thread_root_id=thread_root,
            project_name=project_name,
            agent=agent,
            throttler=throttler,
        )
        self._agents_by_thread[thread_root] = task_obj

        bg = asyncio.create_task(
            self._run_agent_turn(task_obj, task), name=f"agent-{thread_root}"
        )
        task_obj.task = bg

    async def _run_agent_turn(self, task_obj: _AgentTask, prompt: str) -> None:
        """启动 agent 进程并发送首条 prompt；结束后收尾。"""
        thread_root = task_obj.thread_root_id
        try:
            await task_obj.agent.start()
        except Exception as exc:
            logger.exception("agent 启动失败")
            await self._safe_reply(thread_root, f"❌ agent 启动失败: {exc}")
            await task_obj.throttler.aclose()
            self._agents_by_thread.pop(thread_root, None)
            return
        try:
            await self._safe_reply(thread_root, "▶️ agent 已就绪，开始执行…")
            await task_obj.agent.prompt(prompt)
            await task_obj.throttler.flush()
            await self._safe_reply(thread_root, "✅ 本轮结束（可在话题内继续回复 agent）")
        except Exception as exc:
            logger.exception("agent 执行异常")
            await self._safe_reply(thread_root, f"❌ agent 异常: {exc}")
        finally:
            await task_obj.throttler.aclose()
            await task_obj.agent.aclose()

    async def _forward_to_agent(self, msg: IncomingMessage) -> None:
        """话题内回复 → 作为新 prompt 转发给 agent。"""
        task_obj = self._agents_by_thread.get(msg.thread_root_id or "")
        if task_obj is None:
            logger.debug("话题 %s 无对应 agent，忽略", msg.thread_root_id)
            return
        text = msg.text.strip()
        if not text:
            return
        if task_obj.task is None or task_obj.task.done():
            await self._safe_reply(
                msg.thread_root_id or msg.message_id,
                "⚠️ 该 agent 已结束本轮。发送 `/run ...` 新建任务。",
            )
            return
        # 转发：开新 turn，复用同一 session（保留上下文）
        asyncio.create_task(
            self._run_agent_turn(task_obj, text),
            name=f"agent-followup-{msg.message_id}",
        )

    async def _list_agents(self, msg: IncomingMessage) -> None:
        if not self._agents_by_thread:
            await self._safe_reply(msg.message_id, "当前无活跃 agent。")
            return
        lines = [f"• {t.project_name} (thread {t.thread_root_id})" for t in self._agents_by_thread.values()]
        await self._safe_reply(msg.message_id, "活跃 agent:\n" + "\n".join(lines))

    # ------------------------------------------------------------------ #
    # 发送辅助
    # ------------------------------------------------------------------ #

    async def _send_piece(self, thread_root: str, piece: str) -> None:
        """节流器 sink：把一段文本发到话题。HTTP 是阻塞调用，放线程池。"""
        if not piece:
            return
        await asyncio.to_thread(self._bridge.reply_in_thread, thread_root, piece)

    async def _safe_reply(self, root_message_id: str, text: str) -> None:
        """发消息但吞掉异常（只记录日志），避免一条失败拖垮 daemon。"""
        assert self._bridge is not None
        try:
            await asyncio.to_thread(self._bridge.reply_in_thread, root_message_id, text)
        except Exception:
            logger.exception("飞书发送失败 root=%s", root_message_id)

    async def _shutdown(self) -> None:
        for task_obj in list(self._agents_by_thread.values()):
            if task_obj.task and not task_obj.task.done():
                task_obj.task.cancel()
            try:
                await task_obj.agent.aclose()
            except Exception:
                pass
        self._agents_by_thread.clear()
