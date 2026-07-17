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

from .acp_client import AcpAgent, AgentSpawn, OnOutput
from .config import Config
from .feishu import FeishuBridge, IncomingMessage

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


async def run(cfg: Config, *, discover: bool = False) -> None:
    """启动 daemon：飞书 WS 长连接 + agent 调度。阻塞直到收到退出信号。

    ``discover=True`` 时只打印收到消息的 chat_id，不执行任何命令
    （帮助用户发现群 id 后填进配置）。
    """
    daemon = _Daemon(cfg, discover=discover)
    await daemon.run()


@dataclass
class _AgentSession:
    """一个活跃 agent 的运行时状态。"""

    thread_root_id: str
    project_name: str
    agent_label: str
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
    _bridge: FeishuBridge | None = None
    _sessions: dict[str, _AgentSession] = field(default_factory=dict)
    _seen_message_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)

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
        else:
            await self._safe_reply(msg.message_id, _USAGE)

    async def _spawn_for_root(self, msg: IncomingMessage, body: str) -> None:
        """解析 ``/run <project> <task>``，创建 agent session 并启动 worker。"""
        parts = body.split(maxsplit=1)
        if len(parts) < 2:
            await self._safe_reply(msg.message_id, "格式：`/run <项目名> <任务描述>`")
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

        thread_root = msg.message_id
        if thread_root in self._sessions:
            logger.info("根消息 %s 已有 agent session，忽略重复 spawn", thread_root)
            return

        # R11：并��上限检查
        if len(self._sessions) >= self.cfg.max_agents:
            await self._safe_reply(
                msg.message_id,
                f"⚠️ 活跃 agent 已达上限 {self.cfg.max_agents}，"
                "请先 `/stop` 或等待完成。",
            )
            return

        sess = _AgentSession(
            thread_root_id=thread_root,
            project_name=project_name,
            agent_label=project.default_agent,
        )

        async def on_output(text: str) -> None:
            if sess.current_channel is not None:
                sess.current_channel.feed(text)

        agent = self._make_agent(
            AgentSpawn(command=list(agent_argv), cwd=str(project.path)), on_output
        )
        sess.agent = agent
        sess.queue.put_nowait(task)
        self._sessions[thread_root] = sess
        await self._safe_reply(
            thread_root,
            f"🚀 启动 {project.default_agent} 处理项目 {project_name}…\n任务: {task}",
        )
        sess.worker = asyncio.create_task(
            self._agent_worker(sess), name=f"agent-{thread_root}"
        )

    def _make_agent(self, spawn: AgentSpawn, on_output: OnOutput) -> AcpAgent:
        """构造底层 agent（拆出来是测试注入点）。"""
        return AcpAgent(spawn, on_output)

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
            await self._safe_reply(root, f"❌ agent 启动失败: {str(exc)[:200]}")
            await self._close_session(sess)
            return
        await self._safe_reply(root, "▶️ agent 已就绪，开始执行…")
        try:
            while True:
                prompt = await sess.queue.get()
                if prompt is None:
                    await self._safe_reply(root, "🛑 agent 已停止。")
                    break
                title = f"{sess.project_name} · {sess.agent_label}"
                channel = self._make_channel(root, title)
                sess.current_channel = channel
                try:
                    await sess.agent.prompt(prompt)
                    await channel.flush()
                    await channel.set_status("done")
                    await self._safe_reply(
                        root, "✅ 本轮结束（可继续回复；发送 `/stop` 结束该 agent）"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("agent 执行异常")
                    try:
                        await channel.set_status("error")
                    except Exception:
                        logger.debug("set_status error 失败（忽略）", exc_info=True)
                    await self._safe_reply(
                        root, f"❌ agent 异常，已结束该 agent: {str(exc)[:200]}"
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
        """话题内回复 → 入队给对应 agent（worker 串行消费）。"""
        sess = self._sessions.get(msg.thread_root_id or "")
        if sess is None:
            logger.debug("话题 %s 无对应 agent，忽略", msg.thread_root_id)
            return
        text = msg.text.strip()
        if not text:
            return
        if sess.worker is None or sess.worker.done():
            await self._safe_reply(
                msg.thread_root_id or msg.message_id,
                "⚠️ 该 agent 已结束。发送 `/run ...` 新建任务。",
            )
            return
        if text == _STOP_CMD:
            sess.queue.put_nowait(None)
            return
        sess.queue.put_nowait(text)

    async def _list_agents(self, msg: IncomingMessage) -> None:
        if not self._sessions:
            await self._safe_reply(msg.message_id, "当前无活跃 agent。")
            return
        lines = [
            f"• {s.project_name} (thread {s.thread_root_id}, 待执行 {s.queue.qsize()})"
            for s in self._sessions.values()
        ]
        await self._safe_reply(msg.message_id, "活跃 agent:\n" + "\n".join(lines))

    # ------------------------------------------------------------------ #
    # 发送辅助
    # ------------------------------------------------------------------ #

    async def _send_piece(self, thread_root: str, piece: str) -> None:
        """节流器 sink：把一段文本发到话题。HTTP 是阻塞调用，放线程池。"""
        if not piece:
            return
        assert self._bridge is not None
        await asyncio.to_thread(self._bridge.reply_in_thread, thread_root, piece)

    async def _safe_reply(self, root_message_id: str, text: str) -> None:
        """发消息但吞掉异常（只记录日志），避免一条失败拖垮 daemon。"""
        assert self._bridge is not None
        try:
            await asyncio.to_thread(self._bridge.reply_in_thread, root_message_id, text)
        except Exception:
            logger.exception("飞书发送失败 root=%s", root_message_id)

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
