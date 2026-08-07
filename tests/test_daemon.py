"""daemon 生命周期集成测试（fake bridge + fake agent，不碰网络/子进程）。"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from feishu_dispatcher.config import Config, LLMSettings, Project
from feishu_dispatcher.daemon import _Daemon
from feishu_dispatcher.feishu import IncomingMessage
from feishu_dispatcher.scheduler import LLMResponse, ToolCall
from feishu_dispatcher.store import ProjectStore, TaskStore


class FakeBridge:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.stopped = False
        self.cards: list[dict] = []
        self.card_replies: list[tuple[str, dict]] = []
        self.card_patches: list[tuple[str, dict]] = []
        self.reply_card_errors: int = 0
        self.patch_card_errors: int = 0

        self.roots: list[tuple[str, str]] = []
        self.plain: list[tuple[str, str]] = []  # reply_in_thread=False（不建话题）

    def reply_in_thread(self, root_message_id: str, text: str) -> str:
        self.replies.append((root_message_id, text))
        return f"om_reply_{len(self.replies)}"

    def reply(self, message_id: str, text: str) -> str:
        self.replies.append((message_id, text))
        self.plain.append((message_id, text))
        return f"om_reply_{len(self.replies)}"

    def send_root_message(self, chat_id: str, text: str) -> str:
        self.roots.append((chat_id, text))
        return f"om_newroot_{len(self.roots)}"

    def reply_card(self, root_message_id: str, card: dict) -> str:
        if self.reply_card_errors > 0:
            self.reply_card_errors -= 1
            raise RuntimeError("reply_card boom")
        self.card_replies.append((root_message_id, card))
        mid = f"om_card_{len(self.card_replies)}"
        self.cards.append(card)
        return mid

    def patch_card(self, message_id: str, card: dict) -> None:
        if self.patch_card_errors > 0:
            self.patch_card_errors -= 1
            raise RuntimeError("patch_card boom")
        self.card_patches.append((message_id, card))
        self.cards.append(card)

    def stop(self) -> None:
        self.stopped = True

    def texts(self, root: str | None = None) -> list[str]:
        return [t for r, t in self.replies if root is None or r == root]


class FakeAgent:
    def __init__(
        self, spawn, on_output, on_action=None, *, resume_session_id=None
    ) -> None:
        self.spawn = spawn
        self.on_output = on_output
        self.on_action = on_action
        self.resume_session_id = resume_session_id
        self.prompts: list[str] = []
        self.start_count = 0
        self.closed = False
        self.session_id = resume_session_id
        self.last_message = ""
        self.model = ""  # 默认无模型（似 copilot）；ModelAgent 覆盖
        self.available_models: list[str] = []
        self.set_model_calls: list[str] = []
        self.cancel_calls = 0

    async def start(self) -> None:
        self.start_count += 1
        # 新会话给个假 id；恢复则沿用传入的 session_id
        if self.session_id is None:
            self.session_id = f"fake_sid_{id(self)}"

    async def prompt(self, text: str) -> str:
        self.prompts.append(text)
        self.last_message = f"reply:{text}"
        await self.on_output(f"echo:{text}")
        return "end_turn"

    async def cancel(self) -> None:
        self.cancel_calls += 1

    async def set_model(self, name: str) -> None:
        self.set_model_calls.append(name)
        self.model = name

    async def aclose(self) -> None:
        self.closed = True


class FailingAgent(FakeAgent):
    async def prompt(self, text: str) -> str:
        raise RuntimeError("boom")


class FailUnlessResumedAgent(FakeAgent):
    """新建会话的那一轮 prompt 抛错（模拟 turn 异常）；恢复后（resume_session_id
    有值）的新实例成功——用于验证 failed → load_session 接回。"""

    async def prompt(self, text: str) -> str:
        self.prompts.append(text)
        if self.resume_session_id is None:
            raise RuntimeError("boom")
        self.last_message = f"reply:{text}"
        await self.on_output(f"echo:{text}")
        return "end_turn"


class StartupFailAgent(FakeAgent):
    async def start(self) -> None:
        raise RuntimeError("startup boom")


class CancelableAgent(FakeAgent):
    """prompt() 阻塞直到被 cancel()，然后返回 stop_reason='cancelled'（模拟在途取消）。"""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.in_prompt = asyncio.Event()
        self._cancelled = asyncio.Event()

    async def prompt(self, text: str) -> str:
        self.prompts.append(text)
        self.in_prompt.set()
        await self._cancelled.wait()
        self.in_prompt.clear()
        self._cancelled.clear()
        return "cancelled"

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self._cancelled.set()


class ModelAgent(FakeAgent):
    """启动后上报一个模型 + 可选列表（似 opencode），验证模型采集/展示/切换链路。"""

    async def start(self) -> None:
        await super().start()
        self.model = "ns-deepseek/deepseek-v4-pro"
        self.available_models = ["ns-deepseek/deepseek-v4-pro", "zhipuai/glm-5"]


class UsageAgent(ModelAgent):
    """启动上报模型（似 opencode）且每轮 prompt 后带 token 用量，验证 footer 拼接。"""

    async def prompt(self, text: str) -> str:
        reason = await super().prompt(text)
        self.last_usage_tokens = 3210
        return reason


class GatedAgent(FakeAgent):
    """首轮 prompt() 阻塞在 gate 事件上（保持 turn_in_flight），之后各轮立即完成——
    用于确定性地在「turn 在途」窗口内投递 bg 结果、观察合并（#79）。"""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.gate = asyncio.Event()
        self._first = True

    async def prompt(self, text: str) -> str:
        self.prompts.append(text)
        if self._first:
            self._first = False
            await self.gate.wait()
        self.last_message = f"reply:{text}"
        await self.on_output(f"echo:{text}")
        return "end_turn"


def make_daemon(
    agent_cls: type[FakeAgent] = FakeAgent,
    *,
    stream_mode: str = "text",
    store: TaskStore | None = None,
    project_store: ProjectStore | None = None,
    idle_timeout: float = 1800.0,
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"], "opencode": ["opencode", "acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        idle_timeout=idle_timeout,
        stream_mode=stream_mode,
    )
    daemon = _Daemon(
        cfg,
        store=store or TaskStore(None),
        project_store=project_store or ProjectStore(None),
    )
    bridge = FakeBridge()
    daemon._bridge = bridge  # 绕过 run()，直接注入
    created: list[FakeAgent] = []

    def factory(spawn, on_output, on_action=None, *, resume_session_id=None):
        agent = agent_cls(
            spawn, on_output, on_action, resume_session_id=resume_session_id
        )
        created.append(agent)
        return agent

    daemon._make_agent = factory  # type: ignore[method-assign]
    return daemon, bridge, created


def root_msg(text: str, mid: str = "om_root1") -> IncomingMessage:
    return IncomingMessage(
        chat_id="oc_1",
        message_id=mid,
        thread_root_id=None,
        text=text,
        chat_type="group",
        sender_id="ou_user",
    )


def thread_msg(
    text: str, root: str = "om_root1", mid: str = "om_t1"
) -> IncomingMessage:
    return IncomingMessage(
        chat_id="oc_1",
        message_id=mid,
        thread_root_id=root,
        text=text,
        chat_type="group",
        sender_id="ou_user",
    )


async def wait_until(cond, timeout: float = 2.0) -> None:
    async def _poll():
        while not cond():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


def test_fmt_tokens_scales_units():
    from feishu_dispatcher.daemon import _fmt_tokens

    assert _fmt_tokens(0) == "~0 tok"
    assert _fmt_tokens(850) == "~850 tok"
    assert _fmt_tokens(3210) == "~3.2k tok"
    assert _fmt_tokens(32000) == "~32k tok"  # 整千不留 .0
    assert _fmt_tokens(1_200_000) == "~1.2M tok"


def test_with_tokens_appends_to_footer():
    from feishu_dispatcher.daemon import _with_tokens

    assert _with_tokens("demo · 模型：X", 3210) == "demo · 模型：X · ~3.2k tok"
    assert _with_tokens("", 3210) == "~3.2k tok"  # 空 footer 不带前导分隔


async def test_run_dispatches_and_streams_output():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo do stuff"))
    await wait_until(
        lambda: any("echo:do stuff" in t for t in bridge.texts("om_root1"))
    )
    await wait_until(lambda: any("✅" in t for t in bridge.texts("om_root1")))
    assert len(created) == 1
    assert created[0].prompts == ["do stuff"]
    assert created[0].start_count == 1


async def test_run_agent_flag_overrides_default():
    daemon, bridge, created = make_daemon()
    # demo 默认 copilot；--agent opencode 覆盖
    await daemon._handle_message(root_msg("/run demo 做点事 --agent opencode"))
    await wait_until(lambda: created and created[0].prompts == ["做点事"])
    assert daemon.store.by_thread("om_root1").agent_label == "opencode"
    assert any("opencode" in t for t in bridge.texts("om_root1"))


async def test_run_without_agent_flag_uses_default():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo 做点事"))
    await wait_until(lambda: created and created[0].prompts == ["做点事"])
    assert daemon.store.by_thread("om_root1").agent_label == "copilot"  # 项目默认


async def test_run_unknown_agent_errors_no_spawn():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo 做点事 --agent nope"))
    assert any("未知 agent" in t for m, t in bridge.plain if m == "om_root1")
    assert created == []  # 未知 agent 直接报错，不启动
    assert daemon.store.by_thread("om_root1") is None


async def test_thread_reply_reuses_same_agent_without_restart():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo first task"))
    await wait_until(lambda: created and created[0].prompts == ["first task"])

    await daemon._handle_message(thread_msg("second task"))
    await wait_until(lambda: created[0].prompts == ["first task", "second task"])
    await wait_until(
        lambda: any("echo:second task" in t for t in bridge.texts("om_root1"))
    )
    # 核心断言（R2/R3）：同一 agent、只 start 一次、进程未被关闭
    assert len(created) == 1
    assert created[0].start_count == 1
    assert not created[0].closed


async def test_stop_command_closes_agent_and_removes_session():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])

    await daemon._handle_message(thread_msg("/stop"))
    await wait_until(lambda: created[0].closed)
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    assert any("🛑" in t for t in bridge.texts("om_root1"))


async def test_stop_cancels_in_flight_turn():
    daemon, bridge, created = make_daemon(agent_cls=CancelableAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    # 等 agent 进入在途 turn（prompt() 阻塞中）
    await wait_until(lambda: created and created[0].in_prompt.is_set())
    # 此时 /stop：应触发 cancel 打断在途轮，而非傻等整轮跑完
    await daemon._handle_message(thread_msg("/stop"))
    await wait_until(lambda: created[0].cancel_calls == 1)
    # 取消后 agent 收尾关闭、session 移除、任务标 stopped
    await wait_until(lambda: created[0].closed)
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    assert any("🛑" in t for t in bridge.texts("om_root1"))
    assert daemon.store.by_thread("om_root1").status == "stopped"


async def test_stop_when_idle_does_not_cancel():
    # 无在途 turn 时 /stop 不应调用 cancel（避免多余的 session/cancel）
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    await wait_until(lambda: not daemon._sessions["om_root1"].turn_in_flight)
    await daemon._handle_message(thread_msg("/stop"))
    await wait_until(lambda: created[0].closed)
    assert created[0].cancel_calls == 0


async def test_cancel_stops_turn_but_keeps_agent():
    daemon, bridge, created = make_daemon(agent_cls=CancelableAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].in_prompt.is_set())
    await daemon._handle_message(thread_msg("/cancel"))
    await wait_until(lambda: created[0].cancel_calls == 1)
    await wait_until(lambda: not daemon._sessions["om_root1"].turn_in_flight)
    # agent 保留：未关闭、session 还在、任务回 idle（非 stopped）
    assert not created[0].closed
    assert "om_root1" in daemon._sessions
    await wait_until(lambda: daemon.store.by_thread("om_root1").status == "idle")
    assert any("已取消当前轮" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_cancel_with_input_runs_new_turn():
    daemon, bridge, created = make_daemon(agent_cls=CancelableAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].in_prompt.is_set())
    await daemon._handle_message(thread_msg("/cancel do this instead"))
    await wait_until(lambda: created[0].cancel_calls == 1)
    # 取消后新输入作为下一轮被拾起执行（FIFO），agent 仍存活
    await wait_until(lambda: created[0].prompts == ["task", "do this instead"])
    assert not created[0].closed
    assert "om_root1" in daemon._sessions
    await daemon._shutdown()


async def test_cancel_when_idle_reports_nothing_to_cancel():
    daemon, bridge, created = make_daemon()  # FakeAgent（回合秒完）
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    await wait_until(lambda: not daemon._sessions["om_root1"].turn_in_flight)
    await daemon._handle_message(thread_msg("/cancel"))
    assert created[0].cancel_calls == 0
    assert any("没有在跑的轮" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_help_in_thread_shows_usage_not_forwarded_to_agent():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])

    await daemon._handle_message(thread_msg("/help", mid="om_help"))
    # 回了话题内用法，且 /help 没被当 prompt 排给 agent（不入队、不关 agent）
    assert any("话题内用法" in t for t in bridge.texts("om_root1"))
    assert created[0].prompts == ["task"]
    assert not created[0].closed
    await daemon._shutdown()


async def test_help_in_dormant_thread_replies_without_recovery():
    daemon, bridge, created = make_daemon()
    # 没有活跃 session 的话题里发 /help：仍回用法，且不为此拉起/恢复任何 agent
    await daemon._handle_message(thread_msg("/help", root="om_orphan", mid="om_z"))
    assert any("话题内用法" in t for t in bridge.texts("om_orphan"))
    assert created == []


async def test_help_on_root_shows_console_usage():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/help", mid="om_h"))
    # root 主线 /help 走普通回复（不建话题），给控制台用法
    assert any(m == "om_h" and "用法" in t for m, t in bridge.plain)


async def test_raw_forwards_reserved_command_to_agent():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    # /raw /model：/model 逐字转发给 agent，而非被 daemon 当模型命令拦截
    await daemon._handle_message(thread_msg("/raw /model", mid="om_raw1"))
    await wait_until(lambda: created[0].prompts == ["task", "/model"])
    assert not created[0].closed
    await daemon._shutdown()


async def test_raw_bare_shows_usage_hint_not_forwarded():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    # 裸 /raw（无内容）：给用法提示，不入队给 agent
    await daemon._handle_message(thread_msg("/raw", mid="om_raw0"))
    assert any(t.startswith("用法：") for t in bridge.texts("om_root1"))
    assert created[0].prompts == ["task"]
    await daemon._shutdown()


async def test_raw_forwards_stop_literally_keeps_agent():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    # /raw /stop：/stop 逐字转发，绝不把 agent 当 /stop 结束
    await daemon._handle_message(thread_msg("/raw /stop", mid="om_raw2"))
    await wait_until(lambda: created[0].prompts == ["task", "/stop"])
    assert not created[0].closed
    assert "om_root1" in daemon._sessions
    await daemon._shutdown()


async def test_raw_in_dormant_thread_recovers_not_stops():
    store = TaskStore(None)
    _seed_task(store, thread="om_orphan")  # 可恢复的挂起任务
    daemon, bridge, created = make_daemon(store=store)
    # 挂起话题里 /raw /stop：恢复 agent 并把 /stop 当首轮转发，不当停止命令
    await daemon._handle_message(
        thread_msg("/raw /stop", root="om_orphan", mid="om_rz")
    )
    await wait_until(lambda: created and created[0].prompts == ["/stop"])
    assert store.by_thread("om_orphan").status != "stopped"
    await daemon._shutdown()


async def test_duplicate_message_id_spawns_only_once():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task", mid="om_dup"))
    await daemon._handle_message(root_msg("/run demo task", mid="om_dup"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    assert len(created) == 1


async def test_agent_error_reports_and_closes_session():
    daemon, bridge, created = make_daemon(agent_cls=FailingAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: any("❌" in t for t in bridge.texts("om_root1")))
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    assert created[0].closed


async def test_unknown_project_replies_error():
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(root_msg("/run nope task"))
    assert any("未知项目" in t for t in bridge.texts("om_root1"))


async def test_plain_root_message_replies_usage():
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(root_msg("你好"))
    assert any("用法" in t for t in bridge.texts("om_root1"))


async def test_shutdown_cancels_workers_and_stops_bridge():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])

    await daemon._shutdown()
    assert bridge.stopped
    assert daemon._sessions == {}
    assert created[0].closed


async def test_shutdown_survives_hung_control_stop():
    """#81：即使 control.stop() 永久阻塞，_shutdown 也在超时内返回、后续步骤照走。

    复现旧 bug 的场景（关控制面卡死冻住关闭流程 → reboot 停不干净 → 僵尸堆积），
    验证 to_thread + 超时保护让它安全退化。
    """
    import threading

    import feishu_dispatcher.daemon as daemon_mod

    daemon, bridge, created = make_daemon()
    unblock = threading.Event()

    class HungControl:
        def stop(self) -> None:
            unblock.wait(30)  # 模拟 server.shutdown() 卡死，直到测试放行

    daemon._control = HungControl()  # type: ignore[assignment]
    orig = daemon_mod._CONTROL_STOP_TIMEOUT
    daemon_mod._CONTROL_STOP_TIMEOUT = 0.2
    try:
        # 无超时保护会等 30s；有保护则 ~0.2s 返回。外层 5s 上限做安全网。
        await asyncio.wait_for(daemon._shutdown(), timeout=5.0)
    finally:
        daemon_mod._CONTROL_STOP_TIMEOUT = orig
        unblock.set()  # 放行后台线程，避免 executor 关闭时 join 卡住
    assert bridge.stopped  # 控制面卡住不影响后续清理（停 bridge 等照常走完）


# ---------------------------------------------------------------------- #
# R11: max_agents 并发上限
# ---------------------------------------------------------------------- #


def make_daemon_with_limit(
    max_agents: int,
    agent_cls: type[FakeAgent] = FakeAgent,
    *,
    store: TaskStore | None = None,
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"], "opencode": ["opencode", "acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        max_agents=max_agents,
        stream_mode="text",
    )
    daemon = _Daemon(cfg, store=store or TaskStore(None))
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []

    def factory(spawn, on_output, on_action=None, *, resume_session_id=None):
        agent = agent_cls(
            spawn, on_output, on_action, resume_session_id=resume_session_id
        )
        created.append(agent)
        return agent

    daemon._make_agent = factory  # type: ignore[method-assign]
    return daemon, bridge, created


async def test_max_agents_limit_blocks_excess_spawns():
    # 用一个「不会自己结束」的 agent 占住 session 槽位：
    # FakeAgent.prompt 返回即可，但 session 仍存活在 _sessions 里
    daemon, bridge, created = make_daemon_with_limit(max_agents=1)
    await daemon._handle_message(root_msg("/run demo task1", mid="om_r1"))
    await wait_until(lambda: created and created[0].prompts == ["task1"])
    # 此时已有 1 个活跃 agent，第二个 /run 应被拒绝
    await daemon._handle_message(root_msg("/run demo task2", mid="om_r2"))
    assert len(created) == 1
    assert any("上限" in t for t in bridge.texts("om_r2"))
    # 清理
    await daemon._shutdown()


# ---------------------------------------------------------------------- #
# R10: sender_whitelist 过滤 + discover 模式
# ---------------------------------------------------------------------- #


def make_daemon_with_whitelist(
    sender_whitelist: list[str],
    *,
    stream_mode: str = "text",
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"], "opencode": ["opencode", "acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        sender_whitelist=sender_whitelist,
        stream_mode=stream_mode,
    )
    daemon = _Daemon(cfg)
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []
    daemon._make_agent = (
        lambda spawn, on_output, on_action=None, *, resume_session_id=None: (  # noqa: E731
            created.append(
                FakeAgent(
                    spawn, on_output, on_action, resume_session_id=resume_session_id
                )
            )
            or created[-1]
        )
    )
    return daemon, bridge, created


async def test_sender_whitelist_blocks_non_whitelisted():
    daemon, bridge, created = make_daemon_with_whitelist(["ou_allowed"])
    # 非白名单发送者（root_msg 默认 sender_id=ou_user）
    await daemon._handle_message(root_msg("/run demo task"))
    assert created == []
    assert bridge.texts() == []


async def test_sender_whitelist_allows_whitelisted():
    daemon, bridge, created = make_daemon_with_whitelist(["ou_user"])
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    await daemon._shutdown()


async def test_discover_mode_does_not_execute_commands():
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="",
        agents={"copilot": ["copilot", "--acp"], "opencode": ["opencode", "acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        stream_mode="text",
    )
    daemon = _Daemon(cfg, discover=True)
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []
    daemon._make_agent = (
        lambda spawn, on_output, on_action=None, *, resume_session_id=None: (  # noqa: E731
            created.append(
                FakeAgent(
                    spawn, on_output, on_action, resume_session_id=resume_session_id
                )
            )
            or created[-1]
        )
    )
    await daemon._handle_message(root_msg("/run demo task"))
    assert created == []
    assert bridge.texts() == []


# ---------------------------------------------------------------------- #
# Card 模式测试
# ---------------------------------------------------------------------- #


async def test_card_mode_run_echo_in_card_and_done_status():
    daemon, bridge, created = make_daemon(stream_mode="card")
    await daemon._handle_message(root_msg("/run demo do stuff"))
    await wait_until(
        lambda: any(
            "echo:do stuff" in card["body"]["elements"][0]["content"]
            for _, card in bridge.card_replies
        )
    )
    await wait_until(lambda: any("✅" in t for t in bridge.texts("om_root1")))
    assert len(created) == 1
    assert created[0].prompts == ["do stuff"]
    assert created[0].start_count == 1
    assert len(bridge.card_replies) >= 1
    all_cards = bridge.card_replies + bridge.card_patches
    last_card = all_cards[-1][1]
    assert last_card["header"]["template"] == "green"


async def test_card_mode_footer_shows_token_usage():
    daemon, bridge, created = make_daemon(agent_cls=UsageAgent, stream_mode="card")
    await daemon._handle_message(root_msg("/run demo do stuff"))
    await wait_until(lambda: any("✅" in t for t in bridge.texts("om_root1")))
    all_cards = bridge.card_replies + bridge.card_patches
    last_card = all_cards[-1][1]
    foot = last_card["body"]["elements"][-1]["content"]
    # footer = 项目 · 模型 · token 用量（#53）
    assert "demo" in foot
    assert "ns-deepseek/deepseek-v4-pro" in foot
    assert "~3.2k tok" in foot


async def test_card_mode_thread_reply_reuses_same_agent():
    daemon, bridge, created = make_daemon(stream_mode="card")
    await daemon._handle_message(root_msg("/run demo first task"))
    await wait_until(lambda: created and created[0].prompts == ["first task"])

    await daemon._handle_message(thread_msg("second task"))
    await wait_until(lambda: created[0].prompts == ["first task", "second task"])
    assert len(created) == 1
    assert created[0].start_count == 1
    assert not created[0].closed


async def test_card_mode_agent_error_sets_error_status():
    from tests.test_daemon import FailingAgent

    daemon, bridge, created = make_daemon(agent_cls=FailingAgent, stream_mode="card")
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: any("❌" in t for t in bridge.texts("om_root1")))
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    assert created[0].closed


async def test_card_mode_stop_command_closes_agent():
    daemon, bridge, created = make_daemon(stream_mode="card")
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])

    await daemon._handle_message(thread_msg("/stop"))
    await wait_until(lambda: created[0].closed)
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    assert any("🛑" in t for t in bridge.texts("om_root1"))


# ---------------------------------------------------------------------- #
# 会话恢复（跨 daemon 重启）
# ---------------------------------------------------------------------- #


def _seed_task(
    store, *, thread, agent="copilot", session_id="sid_x", status="suspended"
):
    """在台账里塞一个可恢复的历史任务（模拟重启前留下的）。"""
    t = store.create(
        project_name="demo",
        agent_label=agent,
        description="旧任务",
        thread_root_id=thread,
        workspace="C:/tmp/demo",
    )
    store.update(t.task_id, session_id=session_id, status=status)
    return t


async def test_run_creates_task():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(
        lambda: store.by_thread("om_root1") and store.by_thread("om_root1").session_id
    )
    t = store.by_thread("om_root1")
    assert t.project_name == "demo"
    assert t.agent_label == "copilot"
    assert t.session_id == created[0].session_id
    assert t.description == "task"
    await daemon._shutdown()


async def test_recovery_after_restart_uses_load_session():
    store = TaskStore(None)  # 内存 store 跨两个 daemon 实例共享 = 模拟重启
    d1, b1, c1 = make_daemon(store=store)
    await d1._handle_message(root_msg("/run demo task1"))
    await wait_until(
        lambda: store.by_thread("om_root1") and store.by_thread("om_root1").session_id
    )
    saved_sid = store.by_thread("om_root1").session_id
    await d1._shutdown()  # 服务停止；任务标记 suspended、记录保留
    assert store.by_thread("om_root1").status == "suspended"

    d2, b2, c2 = make_daemon(store=store)
    assert d2._sessions == {}
    await d2._handle_message(thread_msg("follow up", root="om_root1", mid="om_t2"))
    await wait_until(lambda: c2 and c2[0].prompts == ["follow up"])
    assert c2[0].resume_session_id == saved_sid
    assert c2[0].start_count == 1
    assert any("恢复" in t for t in b2.texts("om_root1"))
    await d2._shutdown()


async def test_reply_to_unknown_topic_notifies_not_silent():
    daemon, bridge, created = make_daemon()  # 空 store
    await daemon._handle_message(thread_msg("hello", root="om_unknown", mid="om_x"))
    assert created == []
    assert any("没有对应任务" in t for t in bridge.texts("om_unknown"))


async def test_stop_marks_task_stopped():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: store.by_thread("om_root1") is not None)
    await daemon._handle_message(thread_msg("/stop"))
    await wait_until(lambda: store.by_thread("om_root1").status == "stopped")


async def test_recovery_fails_when_agent_unconfigured():
    store = TaskStore(None)
    _seed_task(store, thread="om_orphan", agent="ghost")  # agent 已不在配置
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(thread_msg("hello", root="om_orphan", mid="om_y"))
    assert created == []
    assert any("未配置" in t for t in bridge.texts("om_orphan"))
    assert store.by_thread("om_orphan").status == "failed"


async def test_orphan_stop_marks_stopped_without_recovering():
    store = TaskStore(None)
    _seed_task(store, thread="om_orphan")
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(thread_msg("/stop", root="om_orphan", mid="om_z"))
    assert created == []  # 没为了停而恢复
    assert store.by_thread("om_orphan").status == "stopped"
    assert any("已结束" in t for t in bridge.texts("om_orphan"))


async def test_terminal_task_reply_not_auto_resumed():
    store = TaskStore(None)
    _seed_task(store, thread="om_done", status="done")
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(thread_msg("continue", root="om_done", mid="om_d"))
    assert created == []
    assert any("已结束" in t for t in bridge.texts("om_done"))


async def test_recovery_respects_max_agents():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon_with_limit(max_agents=1, store=store)
    await daemon._handle_message(root_msg("/run demo task1", mid="om_r1"))
    await wait_until(lambda: created and created[0].prompts == ["task1"])
    _seed_task(store, thread="om_orphan")  # 可恢复，但已达上限
    await daemon._handle_message(thread_msg("hi", root="om_orphan", mid="om_r2"))
    assert len(created) == 1  # 未恢复
    assert any("上限" in t for t in bridge.texts("om_orphan"))
    await daemon._shutdown()


# ---------------------------------------------------------------------- #
# 空闲挂起 + max_agents 名额释放（坑 1/2/3）
# ---------------------------------------------------------------------- #


async def test_idle_timeout_suspends_but_keeps_record_recoverable():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, idle_timeout=0.1)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    saved_sid = created[0].session_id
    # 空闲超时 → 挂起：关进程、腾名额、但任务留存为 suspended
    await wait_until(lambda: any("💤" in t for t in bridge.texts("om_root1")))
    await wait_until(lambda: "om_root1" not in daemon._sessions)  # 名额已释放
    assert created[0].closed
    await wait_until(lambda: store.by_thread("om_root1").status == "suspended")

    # 在话题里回复 → 自动 load_session 恢复
    await daemon._handle_message(thread_msg("more", root="om_root1", mid="om_t2"))
    await wait_until(lambda: len(created) == 2 and created[1].prompts == ["more"])
    assert created[1].resume_session_id == saved_sid
    await daemon._shutdown()


async def test_idle_timeout_zero_disables_suspend():
    daemon, bridge, created = make_daemon(idle_timeout=0)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: any("✅" in t for t in bridge.texts("om_root1")))
    # 关闭自动挂起：跑完后 session 仍存活
    await asyncio.sleep(0.15)
    assert "om_root1" in daemon._sessions
    assert not created[0].closed
    await daemon._shutdown()


async def test_max_agents_cap_atomic_under_concurrent_run():
    # 坑 3：两条 /run 并发到达、正好在上限边界，不应突破上限。
    daemon, bridge, created = make_daemon_with_limit(max_agents=1)
    await asyncio.gather(
        daemon._handle_message(root_msg("/run demo t1", mid="om_a")),
        daemon._handle_message(root_msg("/run demo t2", mid="om_b")),
    )
    await wait_until(lambda: created and created[0].prompts)
    assert len(created) == 1  # 只起了一个，没突破上限
    rejected = bridge.texts("om_a") + bridge.texts("om_b")
    assert any("上限" in t for t in rejected)
    await daemon._shutdown()


# ---------------------------------------------------------------------- #
# P2：调度器 LLM 自然语言派发
# ---------------------------------------------------------------------- #


class ScriptedLLM:
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)

    async def chat(self, messages, tools) -> LLMResponse:
        return self.script.pop(0)


async def test_nl_dispatch_spawns_agent_via_llm():
    daemon, bridge, created = make_daemon()
    daemon._llm = ScriptedLLM(
        [
            LLMResponse(tool_calls=[ToolCall("1", "list_projects", {})]),
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        "2", "spawn_agent", {"project": "demo", "task": "加 dark mode"}
                    )
                ]
            ),
            LLMResponse(content="已给 demo 派发：加 dark mode"),
        ]
    )
    await daemon._handle_message(root_msg("帮 demo 加个 dark mode", mid="om_nl"))
    await wait_until(lambda: created and created[0].prompts == ["加 dark mode"])
    assert bridge.roots  # agent 有自己的话题根消息
    # LLM 对用户的回复是**普通回复、不建话题**（bug 修复：只有派 agent 才建话题）
    assert any(m == "om_nl" and "已给 demo 派发" in t for m, t in bridge.plain)
    # 用户的对话消息 om_nl 不应成为任何 agent 话题的根
    assert all(root != "om_nl" for root, _ in bridge.roots)
    await daemon._shutdown()


async def test_nl_reply_does_not_create_thread():
    daemon, bridge, created = make_daemon()
    daemon._llm = ScriptedLLM([LLMResponse(content="你好，需要我做什么？")])
    await daemon._handle_message(root_msg("在吗", mid="om_chat"))
    # 纯对话（无 spawn）：回复走普通回复、不建话题、不起 agent
    assert any(m == "om_chat" and "需要我做什么" in t for m, t in bridge.plain)
    assert created == []
    assert bridge.roots == []


# ---------------------------------------------------------------------- #
# 调度器：对话记忆 + 完成通知 + 状态
# ---------------------------------------------------------------------- #


async def test_scheduler_records_exchange_in_memory():
    daemon, bridge, created = make_daemon()
    daemon._llm = ScriptedLLM([LLMResponse(content="收到")])
    await daemon._handle_message(root_msg("记住我叫小明", mid="om_m"))
    assert daemon._sched_memory.history() == [
        {"role": "user", "content": "记住我叫小明"},
        {"role": "assistant", "content": "收到"},
    ]


async def test_scheduler_feeds_history_on_next_message():
    daemon, bridge, created = make_daemon()

    class RecordingLLM:
        def __init__(self) -> None:
            self.n = 0
            self.second_messages: list = []

        async def chat(self, messages, tools) -> LLMResponse:
            self.n += 1
            if self.n == 1:
                return LLMResponse(content="好的，小明")
            self.second_messages = list(messages)
            return LLMResponse(content="你叫小明")

    daemon._llm = RecordingLLM()
    await daemon._handle_message(root_msg("我叫小明", mid="om_1"))
    await daemon._handle_message(root_msg("我叫什么", mid="om_2"))
    contents = [m.get("content") for m in daemon._llm.second_messages]
    assert "我叫小明" in contents and "好的，小明" in contents


async def test_agent_completion_notifies_main_line():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: any("🔔" in t for _, t in bridge.roots))
    assert any("demo 完成" in t for _, t in bridge.roots)
    await daemon._shutdown()


async def test_agent_error_pauses_recoverable_notifies_main_line():
    daemon, bridge, created = make_daemon(agent_cls=FailingAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    # turn 异常 → 主线通知「已暂停」，session 关闭腾名额
    await wait_until(lambda: any("❌" in t and "暂停" in t for _, t in bridge.roots))
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    # 关键：failed 是可恢复态（非终止），且记下诊断
    task = daemon.store.by_thread("om_root1")
    assert task.status == "failed"
    assert task.is_resumable and not task.is_terminal
    assert "RuntimeError" in task.error_message and "boom" in task.error_message


async def test_failed_task_resumes_on_thread_reply():
    daemon, bridge, created = make_daemon(agent_cls=FailUnlessResumedAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    # 第一轮异常 → failed（有 session），worker 关闭
    await wait_until(lambda: daemon.store.by_thread("om_root1").status == "failed")
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    assert daemon.store.by_thread("om_root1").session_id  # turn 失败时 session 已建
    # 话题回复 → load_session 恢复（起第二个 agent，带 resume_session_id）→ 成功
    await daemon._handle_message(thread_msg("再试一次"))
    await wait_until(lambda: len(created) == 2)
    assert created[1].resume_session_id  # 第二个 agent 走 load_session
    await wait_until(
        lambda: any("echo:再试一次" in t for t in bridge.texts("om_root1"))
    )
    # 恢复成功 → 回 idle，error_message 清空
    await wait_until(lambda: daemon.store.by_thread("om_root1").status == "idle")
    assert daemon.store.by_thread("om_root1").error_message == ""
    await daemon._shutdown()


async def test_startup_failure_stays_unresumable_guides_to_run():
    daemon, bridge, created = make_daemon(agent_cls=StartupFailAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: daemon.store.by_thread("om_root1").status == "failed")
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    task = daemon.store.by_thread("om_root1")
    assert not task.session_id  # startup 失败没建会话
    # 话题回复 → 尝试恢复但无 session → 挡回 /run（不丢人，只是没得恢复）
    await daemon._handle_message(thread_msg("再试"))
    await wait_until(
        lambda: any("重开" in t or "/run" in t for t in bridge.texts("om_root1"))
    )


async def test_list_tasks_reports_task_status_and_turns():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    await wait_until(
        lambda: (
            daemon._sched_list_tasks()
            and daemon._sched_list_tasks()[0]["status"] == "idle"
        )
    )
    info = daemon._sched_list_tasks()[0]
    assert info["project"] == "demo"
    assert info["task_id"] == "t1"
    assert info["turns"] == 1
    assert info["description"] == "task"
    await daemon._shutdown()


async def test_nl_dispatch_unknown_project_reported_to_llm():
    daemon, bridge, created = make_daemon()
    # LLM 先试图派给不存在的项目，工具返回错误 → LLM 收尾说明
    daemon._llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall("1", "spawn_agent", {"project": "ghost", "task": "x"})
                ]
            ),
            LLMResponse(content="没找到项目 ghost。"),
        ]
    )
    await daemon._handle_message(root_msg("给 ghost 做点事", mid="om_g"))
    assert created == []  # 未 spawn
    assert any("没找到项目" in t for t in bridge.texts("om_g"))


async def test_nl_without_llm_falls_back_to_usage():
    daemon, bridge, created = make_daemon()  # _llm is None
    await daemon._handle_message(root_msg("帮我做点什么", mid="om_x"))
    assert created == []
    assert any("用法" in t for t in bridge.texts("om_x"))


# ---------------------------------------------------------------------- #
# 任务系统 Phase 2：调度器操作已有任务 + /done、/clear
# ---------------------------------------------------------------------- #


async def test_send_to_task_enqueues_to_running_task():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo first"))
    await wait_until(lambda: created and created[0].prompts == ["first"])
    out = await daemon._sched_send_to_task("t1", "more work")
    await wait_until(lambda: created[0].prompts == ["first", "more work"])
    assert "t1" in out and "转达" in out
    assert len(created) == 1  # 复用同一 agent，未新建
    await daemon._shutdown()


async def test_send_to_task_resumes_suspended_task():
    store = TaskStore(None)
    _seed_task(store, thread="om_s", session_id="sid_s", status="suspended")
    daemon, bridge, created = make_daemon(store=store)
    out = await daemon._sched_send_to_task("t1", "继续")
    await wait_until(lambda: created and created[0].prompts == ["继续"])
    assert created[0].resume_session_id == "sid_s"
    assert "恢复" in out
    await daemon._shutdown()


async def test_send_to_task_terminal_points_to_resume():
    store = TaskStore(None)
    _seed_task(store, thread="om_d", status="done")
    daemon, bridge, created = make_daemon(store=store)
    out = await daemon._sched_send_to_task("t1", "继续")
    assert created == []  # 终止任务不自动恢复
    assert "resume_task" in out


async def test_send_to_task_unknown_id():
    daemon, bridge, created = make_daemon()
    out = await daemon._sched_send_to_task("t99", "x")
    assert "未找到" in out
    assert created == []


async def test_resume_task_revives_suspended_without_running_a_turn():
    store = TaskStore(None)
    _seed_task(store, thread="om_s", session_id="sid_s", status="suspended")
    daemon, bridge, created = make_daemon(store=store)
    out = await daemon._sched_resume_task("t1")
    await wait_until(lambda: created and created[0].start_count == 1)
    assert created[0].resume_session_id == "sid_s"
    assert created[0].prompts == []  # 仅拉起在线，不跑首轮
    await wait_until(lambda: store.get("t1").status == "idle")
    assert "恢复" in out
    await daemon._shutdown()


async def test_resume_task_already_running_is_noop():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    out = await daemon._sched_resume_task("t1")
    assert "已在运行" in out
    assert len(created) == 1
    await daemon._shutdown()


async def test_mark_done_active_archives_and_closes_agent():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    out = await daemon._sched_mark_done("t1")
    assert "done" in out
    await wait_until(lambda: store.get("t1").status == "done")
    await wait_until(lambda: "om_root1" not in daemon._sessions)
    assert created[0].closed
    assert any("归档" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_mark_done_inactive_task_updates_ledger():
    store = TaskStore(None)
    _seed_task(store, thread="om_s", status="suspended")
    daemon, bridge, created = make_daemon(store=store)
    out = await daemon._sched_mark_done("t1")
    assert store.get("t1").status == "done"
    assert created == []  # 无活跃 session 时不拉起 agent
    assert "done" in out


async def test_mark_done_unknown_id():
    daemon, bridge, created = make_daemon()
    out = await daemon._sched_mark_done("t42")
    assert "未找到" in out


async def test_done_command_in_thread_archives_and_closes():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    await daemon._handle_message(thread_msg("/done"))
    await wait_until(lambda: store.get("t1").status == "done")
    await wait_until(lambda: created[0].closed)
    assert any("归档" in t for t in bridge.texts("om_root1"))


async def test_done_command_on_suspended_task_without_recovering():
    store = TaskStore(None)
    _seed_task(store, thread="om_s", status="suspended")
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(thread_msg("/done", root="om_s", mid="om_dn"))
    assert created == []  # 不为了归档而恢复
    assert store.get("t1").status == "done"
    assert any("归档" in t for t in bridge.texts("om_s"))


async def test_clear_command_clears_terminal_history():
    store = TaskStore(None)
    _seed_task(store, thread="om_old", status="stopped")  # 终止历史
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/clear", mid="om_c"))
    assert any("已清理 1" in t for t in bridge.texts("om_c"))
    assert store.get("t1") is None  # 终止任务被清掉


async def test_reboot_command_requests_restart_and_replies():
    daemon, bridge, created = make_daemon()
    daemon._stop_event = asyncio.Event()  # run() 正常会建，测试里手动注入
    await daemon._handle_message(root_msg("/reboot", mid="om_rb"))
    # 置位 + 唤醒主循环（run() 返回 True → cli.py re-exec）；先回执再重启
    assert daemon._reboot_requested is True
    assert daemon._stop_event.is_set()
    assert any("重启" in t for t in bridge.texts("om_rb"))


async def test_get_task_returns_detail():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo do it"))
    await wait_until(lambda: store.get("t1") and store.get("t1").session_id)
    info = daemon._sched_get_task("t1")
    assert info["task_id"] == "t1"
    assert info["project"] == "demo"
    assert info["description"] == "do it"
    assert info["has_session"] is True
    assert info["active"] is True
    assert info["action_count"] == 0  # FakeAgent 不发 tool_call
    assert daemon._sched_get_task("t404") is None
    await daemon._shutdown()


# ---------------------------------------------------------------------- #
# 审计 A：agent 动作日志（ACP tool_call → Task.actions → get_task / /task）
# ---------------------------------------------------------------------- #


class ActionAgent(FakeAgent):
    """每个 prompt 回合先发两个 tool_call 审计动作，再 echo。"""

    async def prompt(self, text: str) -> None:
        self.prompts.append(text)
        if self.on_action is not None:
            await self.on_action({"kind": "edit", "title": f"Editing {text}.py"})
            await self.on_action({"kind": "execute", "title": "pytest"})
        await self.on_output(f"echo:{text}")


async def test_tool_call_actions_logged_to_task_with_turn():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, agent_cls=ActionAgent)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and len(store.get("t1").actions) == 2)
    actions = store.get("t1").actions
    assert actions[0] == {"turn": 1, "kind": "edit", "title": "Editing build.py"}
    assert actions[1] == {"turn": 1, "kind": "execute", "title": "pytest"}
    await daemon._shutdown()


async def test_actions_tagged_with_incrementing_turn():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, agent_cls=ActionAgent)
    await daemon._handle_message(root_msg("/run demo first"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    await daemon._handle_message(thread_msg("second"))
    await wait_until(lambda: store.get("t1") and len(store.get("t1").actions) == 4)
    # 第二轮的动作标 turn=2
    assert store.get("t1").actions[-1]["turn"] == 2
    await daemon._shutdown()


async def test_get_task_includes_action_log():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, agent_cls=ActionAgent)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    info = daemon._sched_get_task("t1")
    assert info["action_count"] == 2
    assert [a["title"] for a in info["recent_actions"]] == [
        "Editing build.py",
        "pytest",
    ]
    await daemon._shutdown()


async def test_task_command_shows_detail_and_actions():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, agent_cls=ActionAgent)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    await daemon._handle_message(root_msg("/task t1", mid="om_q"))
    reply = "\n".join(bridge.texts("om_q"))
    assert "t1" in reply and "Editing build.py" in reply and "pytest" in reply
    await daemon._shutdown()


async def test_task_command_unknown_id_replies_not_found():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/task t404", mid="om_q"))
    assert any("未找到" in t for t in bridge.texts("om_q"))


async def test_last_output_captured_from_agent_reply():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    assert store.get("t1").last_output == "reply:build"
    await daemon._shutdown()


async def test_get_task_includes_last_output():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    assert daemon._sched_get_task("t1")["last_output"] == "reply:build"
    await daemon._shutdown()


async def test_completion_notification_includes_reply_snippet():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: any("完成第 1 轮" in t for _, t in bridge.roots))
    note = next(t for _, t in bridge.roots if "完成第 1 轮" in t)
    assert "reply:build" in note  # 通知带上了收尾摘要
    await daemon._shutdown()


async def test_task_command_shows_last_output():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    await daemon._handle_message(root_msg("/task t1", mid="om_q"))
    reply = "\n".join(bridge.texts("om_q"))
    assert "最近回复: reply:build" in reply
    await daemon._shutdown()


# ---------------------------------------------------------------------- #
# agent 当前模型（opencode 上报；copilot 不暴露则留空）
# ---------------------------------------------------------------------- #


async def test_model_captured_and_surfaced():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, agent_cls=ModelAgent)
    await daemon._handle_message(root_msg("/run demo build"))
    # 等一轮跑完：此时 start（含采集模型）+ 就绪消息都已落地，避开采集/发消息竞态
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    m = "ns-deepseek/deepseek-v4-pro"
    assert store.get("t1").model == m
    assert daemon._sched_get_task("t1")["model"] == m
    # 就绪消息里带上模型（在话题里直接可见）
    assert any(m in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_task_command_shows_model():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, agent_cls=ModelAgent)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    await daemon._handle_message(root_msg("/task t1", mid="om_q"))
    reply = "\n".join(bridge.texts("om_q"))
    assert "模型: ns-deepseek/deepseek-v4-pro" in reply
    await daemon._shutdown()


async def test_model_pinned_as_card_footer():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(
        store=store, agent_cls=ModelAgent, stream_mode="card"
    )
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    # 卡片最下方固定显示模型（footer：小字号 markdown 元素）
    all_cards = bridge.card_replies + bridge.card_patches
    assert any(
        any(
            el.get("tag") == "markdown"
            and el.get("text_size") == "notation"
            and "ns-deepseek/deepseek-v4-pro" in el.get("content", "")
            for el in card["body"]["elements"]
        )
        for _, card in all_cards
    )
    await daemon._shutdown()


async def test_no_model_agent_leaves_blank():
    # 默认 FakeAgent 不上报模型（似 copilot）→ Task.model 空、就绪消息无模型后缀
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    assert store.get("t1").model == ""
    assert daemon._sched_get_task("t1")["model"] == ""
    assert not any("模型：" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_card_footer_shows_project_and_model():
    # #44：卡片 footer 与模型同一行显示项目名，滚到任意卡片都可辨这条话题的归属
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(
        store=store, agent_cls=ModelAgent, stream_mode="card"
    )
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    all_cards = bridge.card_replies + bridge.card_patches
    # footer（notation 小字 markdown 元素）里项目名与模型同行
    assert any(
        any(
            el.get("tag") == "markdown"
            and el.get("text_size") == "notation"
            and "demo" in el.get("content", "")
            and "ns-deepseek/deepseek-v4-pro" in el.get("content", "")
            for el in card["body"]["elements"]
        )
        for _, card in all_cards
    )
    await daemon._shutdown()


async def test_card_footer_project_only_when_no_model():
    # 无模型（似 copilot）：footer 仍显示项目名（不带「模型：」）
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store, stream_mode="card")
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    all_cards = bridge.card_replies + bridge.card_patches
    assert any(
        any(
            el.get("tag") == "markdown"
            and el.get("text_size") == "notation"
            and el.get("content", "") == "demo"
            for el in card["body"]["elements"]
        )
        for _, card in all_cards
    )
    await daemon._shutdown()


# ---------------------------------------------------------------------- #
# 话题内 /model：查看 + 切换模型（ACP set_config_option）
# ---------------------------------------------------------------------- #


async def _run_model_agent(store):
    daemon, bridge, created = make_daemon(store=store, agent_cls=ModelAgent)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    return daemon, bridge, created


async def test_model_command_lists_current_and_available():
    store = TaskStore(None)
    daemon, bridge, created = await _run_model_agent(store)
    await daemon._handle_message(thread_msg("/model", mid="om_m"))
    reply = "\n".join(bridge.texts("om_root1"))
    assert "当前模型：ns-deepseek/deepseek-v4-pro" in reply
    assert "zhipuai/glm-5" in reply
    await daemon._shutdown()


async def test_model_command_switches_and_persists():
    store = TaskStore(None)
    daemon, bridge, created = await _run_model_agent(store)
    await daemon._handle_message(thread_msg("/model zhipuai/glm-5", mid="om_m"))
    assert created[0].set_model_calls == ["zhipuai/glm-5"]  # 调了 ACP set_config_option
    assert store.get("t1").model == "zhipuai/glm-5"  # 台账更新
    assert any("已切换模型为 zhipuai/glm-5" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_model_choice_survives_suspend_resume():
    # 复现 bug：/model 切换后任务挂起，load_session 恢复时模型被还原回默认。
    # ModelAgent.start() 每次都上报默认模型（模拟 opencode 重载后会话配置回默认）——
    # 恢复不应把用户切过的 Task.model 覆盖回去，且应把选择重新 apply 回 agent。
    store = TaskStore(None)  # 跨两个 daemon 实例共享 store = 模拟挂起 + 恢复
    d1, b1, c1 = make_daemon(store=store, agent_cls=ModelAgent)
    await d1._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)

    # 切到 glm-5 → 台账记成 glm-5
    await d1._handle_message(thread_msg("/model zhipuai/glm-5", mid="om_m"))
    assert store.get("t1").model == "zhipuai/glm-5"
    saved_sid = store.by_thread("om_root1").session_id

    # 挂起：任务标 suspended、记录保留，Task.model 应仍是 glm-5
    await d1._shutdown()
    assert store.by_thread("om_root1").status == "suspended"
    assert store.get("t1").model == "zhipuai/glm-5"

    # 新 daemon（共享 store）+ 话题回复 → load_session 恢复（新 agent 上报默认模型）
    d2, b2, c2 = make_daemon(store=store, agent_cls=ModelAgent)
    await d2._handle_message(thread_msg("more", root="om_root1", mid="om_t2"))
    await wait_until(lambda: c2 and c2[0].prompts == ["more"])
    assert c2[0].resume_session_id == saved_sid

    # 期望：用户切过的模型跨挂起/恢复保持（当前 FAIL → 复现 bug）
    assert store.get("t1").model == "zhipuai/glm-5"
    # 期望：恢复后把模型重新 apply 回 agent，实际模型不还原（修复后成立）
    assert "zhipuai/glm-5" in c2[0].set_model_calls
    await d2._shutdown()


async def test_model_command_rejects_unknown():
    store = TaskStore(None)
    daemon, bridge, created = await _run_model_agent(store)
    await daemon._handle_message(thread_msg("/model no-such-model", mid="om_m"))
    assert created[0].set_model_calls == []  # 未知模型不下发
    assert any("未知模型" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_model_command_unsupported_agent():
    # 默认 FakeAgent 无 available_models（似 copilot）→ 提示不支持
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo build"))
    await wait_until(lambda: store.get("t1") and store.get("t1").turns == 1)
    await daemon._handle_message(thread_msg("/model glm-5", mid="om_m"))
    assert created[0].set_model_calls == []
    assert any("不支持切换模型" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_load_session_replay_does_not_log_actions():
    # 恢复时 load_session 会重放历史 session/update；抑制期不应重复记动作。
    # 这里直接验证：suppress=True 时 session_update 不触发 on_action。
    from feishu_dispatcher.acp_client import _Callbacks, _ClientImpl
    from acp import start_tool_call

    logged: list[dict] = []

    async def on_action(a: dict) -> None:
        logged.append(a)

    async def on_output(_t: str) -> None:
        pass

    impl = _ClientImpl(_Callbacks(on_output=on_output, on_action=on_action))
    impl.set_suppress(True)
    await impl.session_update("s1", start_tool_call("tc1", "Editing x.py", kind="edit"))
    assert logged == []  # 抑制期不记
    impl.set_suppress(False)
    await impl.session_update("s1", start_tool_call("tc2", "Editing y.py", kind="edit"))
    assert [a["title"] for a in logged] == ["Editing y.py"]


# ---------------------------------------------------------------------- #
# 项目注册：/project（列出）/ add / remove + register_project 工具
# ---------------------------------------------------------------------- #


async def test_project_list_shows_seed():
    daemon, bridge, _ = make_daemon()  # cfg 里有种子项目 demo
    await daemon._handle_message(root_msg("/project"))
    reply = "\n".join(bridge.texts())
    assert "demo" in reply
    assert "[种子]" in reply


async def test_project_add_registers_and_run_resolves_it(tmp_path):
    daemon, bridge, created = make_daemon()
    (tmp_path / ".git").mkdir()  # 是 git 仓 → 无 warning
    await daemon._handle_message(
        root_msg(f"/project add newp copilot {tmp_path}", mid="om_p")
    )
    assert any("已注册项目 newp" in t for t in bridge.texts())
    assert daemon.project_store.get("newp") is not None
    assert "newp" in daemon._all_projects()
    # /run 现在能解析这个新注册的项目并派发
    await daemon._handle_message(root_msg("/run newp do it", mid="om_r2"))
    await wait_until(lambda: created and created[0].prompts == ["do it"])
    await daemon._shutdown()


async def test_project_add_non_git_path_warns_but_registers(tmp_path):
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(
        root_msg(f"/project add ng copilot {tmp_path}", mid="om_p")
    )
    reply = "\n".join(bridge.texts())
    assert "已注册项目 ng" in reply
    assert "不是 git 仓库" in reply  # warning 放行
    assert daemon.project_store.get("ng") is not None


async def test_project_add_rejects_unknown_agent(tmp_path):
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(
        root_msg(f"/project add p ghost {tmp_path}", mid="om_p")
    )
    assert any("未知 agent" in t for t in bridge.texts())
    assert daemon.project_store.get("p") is None


async def test_project_add_rejects_nonexistent_path():
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(
        root_msg("/project add p copilot C:/no/such/dir_xyz", mid="om_p")
    )
    assert any("路径不存在" in t for t in bridge.texts())
    assert daemon.project_store.get("p") is None


async def test_project_add_rejects_config_seed_name(tmp_path):
    daemon, bridge, _ = make_daemon()  # demo 是 config 种子
    await daemon._handle_message(
        root_msg(f"/project add demo copilot {tmp_path}", mid="om_p")
    )
    assert any("config.toml 里的项目" in t for t in bridge.texts())
    assert daemon.project_store.get("demo") is None  # 没被写进注册表


async def test_project_add_bad_format():
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(root_msg("/project add onlyname", mid="om_p"))
    assert any("格式" in t for t in bridge.texts())


async def test_project_register_rejects_name_with_space(tmp_path):
    # 命令解析会把空格切成多字段，故直接测底层校验：名字含空格必须拒绝
    daemon, _, _ = make_daemon()
    ok, msg = daemon._register_project("a b", "copilot", str(tmp_path))
    assert ok is False
    assert "空格" in msg


async def test_project_remove(tmp_path):
    daemon, bridge, _ = make_daemon()
    daemon.project_store.add(
        Project(name="tmp", path=Path(tmp_path), default_agent="copilot")
    )
    await daemon._handle_message(root_msg("/project remove tmp", mid="om_p"))
    assert any("已删除项目 tmp" in t for t in bridge.texts())
    assert daemon.project_store.get("tmp") is None


async def test_project_remove_seed_refused():
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(root_msg("/project remove demo", mid="om_p"))
    assert any("改配置文件" in t for t in bridge.texts())


async def test_project_remove_not_found():
    daemon, bridge, _ = make_daemon()
    await daemon._handle_message(root_msg("/project remove ghost", mid="om_p"))
    assert any("未找到已注册项目" in t for t in bridge.texts())


async def test_registered_project_survives_restart(tmp_path):
    # 共享文件版 ProjectStore 模拟重启：注册的项目跨 daemon 实例保留
    ps_path = tmp_path / "projects.json"
    proj_dir = tmp_path / "repo"
    proj_dir.mkdir()
    d1, b1, _ = make_daemon(project_store=ProjectStore(ps_path))
    await d1._handle_message(root_msg(f"/project add persistp copilot {proj_dir}"))
    assert d1.project_store.get("persistp") is not None

    d2, b2, created = make_daemon(project_store=ProjectStore(ps_path))
    assert d2.project_store.get("persistp") is not None
    await d2._handle_message(root_msg("/run persistp go", mid="om_r2"))
    await wait_until(lambda: created and created[0].prompts == ["go"])
    await d2._shutdown()


async def test_scheduler_register_project_tool(tmp_path):
    daemon, _, _ = make_daemon()
    (tmp_path / ".git").mkdir()
    out = await daemon._sched_register_project("schedp", "copilot", str(tmp_path))
    assert "已注册项目 schedp" in out
    assert daemon.project_store.get("schedp") is not None
    # 注册后 list_projects 里能看到
    names = {p["name"] for p in daemon._sched_list_projects()}
    assert "schedp" in names


async def test_scheduler_unregister_project_tool(tmp_path):
    daemon, _, _ = make_daemon()
    daemon.project_store.add(
        Project(name="delp", path=Path(tmp_path), default_agent="copilot")
    )
    out = await daemon._sched_unregister_project("delp")
    assert "已删除项目 delp" in out
    assert daemon.project_store.get("delp") is None
    # 种子项目删不了
    out2 = await daemon._sched_unregister_project("demo")
    assert "改配置文件" in out2


# ------------------------- forge 只读获取工具（#56） ------------------------- #


async def test_sched_get_forge_unknown_project():
    daemon, _, _ = make_daemon()
    out = await daemon._sched_get_forge("nope", "issue", 1)
    assert "未找到项目 nope" in out


async def test_sched_get_forge_no_binding(monkeypatch):
    from feishu_dispatcher import forge

    async def no_ref(project):
        return None

    monkeypatch.setattr(forge, "resolve_forge", no_ref)
    daemon, _, _ = make_daemon()
    out = await daemon._sched_get_forge("demo", "issue", 1)
    assert "没有可用的 forge 绑定" in out


async def test_sched_get_forge_happy(monkeypatch):
    from feishu_dispatcher import forge

    async def fake_ref(project):
        return forge.ForgeRef("github", "o/r", "github.com", "u")

    async def fake_get(ref, kind, number):
        return {"number": number, "kind": kind, "title": "hello"}

    monkeypatch.setattr(forge, "resolve_forge", fake_ref)
    monkeypatch.setattr(forge, "get_item", fake_get)
    daemon, _, _ = make_daemon()
    out = await daemon._sched_get_forge("demo", "pr", 55)
    assert json.loads(out) == {"number": 55, "kind": "pr", "title": "hello"}


async def test_sched_get_forge_error_is_readable(monkeypatch):
    from feishu_dispatcher import forge

    async def fake_ref(project):
        return forge.ForgeRef("github", "o/r", "github.com", "u")

    async def boom(ref, kind, number):
        raise forge.ForgeError("Not Found (HTTP 404)")

    monkeypatch.setattr(forge, "resolve_forge", fake_ref)
    monkeypatch.setattr(forge, "get_item", boom)
    daemon, _, _ = make_daemon()
    out = await daemon._sched_get_forge("demo", "issue", 999)
    assert "失败" in out and "404" in out


async def test_sched_list_forge_single_project(monkeypatch):
    from feishu_dispatcher import forge

    async def fake_ref(project):
        return forge.ForgeRef("github", "o/r", "github.com", "u")

    async def fake_list(ref, *, state, limit):
        return {"repo": ref.slug, "count": 1, "items": [{"number": 1, "type": "issue"}]}

    monkeypatch.setattr(forge, "resolve_forge", fake_ref)
    monkeypatch.setattr(forge, "list_items", fake_list)
    daemon, _, _ = make_daemon()
    out = await daemon._sched_list_forge("demo", "open", 20)
    data = json.loads(out)
    assert data["results"][0]["project"] == "demo"
    assert data["results"][0]["count"] == 1


async def test_sched_list_forge_fans_out_and_reports_skipped(monkeypatch):
    from feishu_dispatcher import forge

    # demo 有绑定；extra 无绑定（resolve 返回 None）
    daemon, _, _ = make_daemon()
    daemon.project_store.add(
        Project(name="extra", path=Path("C:/tmp/extra"), default_agent="copilot")
    )

    async def fake_ref(project):
        return (
            forge.ForgeRef("github", "o/r", "github.com", "u")
            if project.name == "demo"
            else None
        )

    async def fake_list(ref, *, state, limit):
        return {"repo": ref.slug, "count": 0, "items": []}

    monkeypatch.setattr(forge, "resolve_forge", fake_ref)
    monkeypatch.setattr(forge, "list_items", fake_list)
    out = await daemon._sched_list_forge("", "open", 20)  # project 空 = 全部
    data = json.loads(out)
    assert [r["project"] for r in data["results"]] == ["demo"]
    assert any("extra" in s for s in data["skipped"])


async def test_sched_list_forge_all_skipped_is_explicit(monkeypatch):
    from feishu_dispatcher import forge

    async def no_ref(project):
        return None

    monkeypatch.setattr(forge, "resolve_forge", no_ref)
    daemon, _, _ = make_daemon()
    out = await daemon._sched_list_forge("demo", "open", 20)
    assert "未能获取任何仓库" in out


# ------------------------- Task 绑定 issue 作 brief（#63） ------------------------- #


def test_issue_tag_extracts_number():
    from feishu_dispatcher.daemon import _issue_tag

    assert _issue_tag("https://github.com/o/r/issues/3") == "#3"
    assert _issue_tag("https://gitlab.com/g/p/-/issues/42") == "#42"
    assert _issue_tag("") == ""
    assert _issue_tag("https://x/no/number/here") == ""  # 末段非数字 → 不显示


async def test_sched_spawn_with_issue_uses_body_as_brief(monkeypatch):
    from feishu_dispatcher import forge

    async def fake_ref(project):
        return forge.ForgeRef("github", "o/r", "github.com", "u")

    async def fake_get(ref, kind, number, *, body_limit=forge._BODY_CLIP):
        assert kind == "issue" and body_limit is None  # brief 取全文
        return {
            "number": number,
            "title": "Fix bug",
            "body": "详细复现步骤……",
            "url": "https://github.com/o/r/issues/3",
        }

    monkeypatch.setattr(forge, "resolve_forge", fake_ref)
    monkeypatch.setattr(forge, "get_item", fake_get)
    daemon, bridge, created = make_daemon()
    out = await daemon._sched_spawn_agent("demo", "照这个改", issue=3)
    # Task 锚定了 issue_url
    t = daemon.store.all()[0]
    assert t.issue_url == "https://github.com/o/r/issues/3"
    assert "issue" in out and "issues/3" in out
    # 就绪消息带 issue 链接
    assert any("issues/3" in text for _, text in bridge.roots)
    # 首轮 brief = 用户任务 + issue 标题/正文
    await wait_until(lambda: bool(created and created[0].prompts))
    brief = created[0].prompts[0]
    assert "照这个改" in brief and "Fix bug" in brief and "详细复现步骤" in brief
    await daemon._shutdown()


async def test_sched_spawn_with_issue_no_binding_degrades(monkeypatch):
    from feishu_dispatcher import forge

    async def no_ref(project):
        return None

    monkeypatch.setattr(forge, "resolve_forge", no_ref)
    daemon, _, created = make_daemon()
    out = await daemon._sched_spawn_agent("demo", "照这个改", issue=3)
    # 取不到 forge → 优雅退化：任务照建但没绑 issue，brief 就是原任务
    t = daemon.store.all()[0]
    assert t.issue_url == ""
    assert "未关联" in out
    await wait_until(lambda: created and created[0].prompts == ["照这个改"])
    await daemon._shutdown()


async def test_sched_get_task_reports_issue_url():
    daemon, _, _ = make_daemon()
    t = daemon.store.create(
        project_name="demo",
        agent_label="copilot",
        description="x",
        thread_root_id="om_x",
        workspace="C:/tmp/demo",
        issue_url="https://github.com/o/r/issues/7",
    )
    info = daemon._sched_get_task(t.task_id)
    assert info["issue_url"] == "https://github.com/o/r/issues/7"
    assert daemon._sched_list_tasks()[0]["issue_url"] == (
        "https://github.com/o/r/issues/7"
    )


# ------------------------- spawn 指定模型 + 模型缓存（#65） ------------------------- #


async def test_sched_spawn_with_model_pins_it():
    # ModelAgent 报 available=[v4-pro, glm-5]；指定 glm-5 → 启动后下发并记台账
    daemon, _, created = make_daemon(ModelAgent)
    await daemon._sched_spawn_agent("demo", "X", model="zhipuai/glm-5")
    await wait_until(lambda: bool(created and created[0].set_model_calls))
    assert "zhipuai/glm-5" in created[0].set_model_calls
    await wait_until(lambda: daemon.store.all()[0].model == "zhipuai/glm-5")
    await daemon._shutdown()


async def test_worker_passively_caches_models():
    # 真实 agent 一启动，worker 就把它报的 available_models 存进缓存（键 = agent_label）
    daemon, _, _ = make_daemon(ModelAgent)
    await daemon._sched_spawn_agent("demo", "X")
    await wait_until(lambda: bool(daemon.model_store.get("copilot")))
    assert "zhipuai/glm-5" in daemon.model_store.get("copilot")
    await daemon._shutdown()


def test_sched_list_models():
    daemon, _, _ = make_daemon()
    daemon.model_store.update("opencode", ["a", "b"])
    assert daemon._sched_list_models("opencode") == {"opencode": ["a", "b"]}
    assert daemon._sched_list_models() == {"opencode": ["a", "b"]}


async def test_models_cmd_lists_and_empty():
    daemon, bridge, _ = make_daemon()
    # 空缓存
    await daemon._handle_message(root_msg("/models"))
    assert any("缓存为空" in t for _, t in bridge.plain)
    # 有缓存
    daemon.model_store.update("opencode", ["m1", "m2"])
    await daemon._handle_message(root_msg("/models", mid="om_r2"))
    assert any("opencode" in t and "m1" in t for _, t in bridge.plain)


async def test_models_refresh_command_boots_throwaway_agent():
    # /models refresh 起一个一次性 ModelAgent 采集后关掉，刷新缓存，不占 max_agents
    daemon, _, created = make_daemon(ModelAgent)
    await daemon._handle_message(root_msg("/models refresh copilot"))
    assert "zhipuai/glm-5" in daemon.model_store.get("copilot")
    assert created and created[-1].closed  # 一次性 agent 已关闭
    assert daemon._sessions == {}  # 没占用 session 名额


# ---------------------------------------------------------------------- #
# /llm：调度器 LLM profile 列出 + 运行时切换（#74）
# ---------------------------------------------------------------------- #


def _daemon_with_llm_profiles() -> tuple[_Daemon, FakeBridge]:
    ds = LLMSettings(base_url="u1", api_key="k", model="deepseek-chat", api="chat")
    g5 = LLMSettings(base_url="u2", api_key="k", model="gpt-5.4", api="responses")
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"]},
        llm=ds,
        llm_profiles={"deepseek": ds, "gpt5": g5},
        llm_active="deepseek",
    )
    daemon = _Daemon(cfg)
    bridge = FakeBridge()
    daemon._bridge = bridge
    daemon._llm_active = "deepseek"  # 模拟 run() 后状态（run() 未调用）
    return daemon, bridge


async def test_llm_command_lists_profiles_marks_active():
    daemon, bridge = _daemon_with_llm_profiles()
    await daemon._handle_message(root_msg("/llm", mid="om_l1"))
    txt = "\n".join(t for m, t in bridge.plain if m == "om_l1")
    assert "deepseek" in txt and "gpt5" in txt
    assert "gpt-5.4" in txt and "responses" in txt
    assert "▶" in txt  # 标了激活的


async def test_llm_command_switches_and_rebuilds_client():
    from feishu_dispatcher.llm import ResponsesAPIClient

    daemon, bridge = _daemon_with_llm_profiles()
    await daemon._handle_message(root_msg("/llm gpt5", mid="om_l2"))
    assert daemon._llm_active == "gpt5"
    assert isinstance(daemon._llm, ResponsesAPIClient)  # 重建成 responses client
    assert any("切换" in t and "gpt5" in t for m, t in bridge.plain if m == "om_l2")


async def test_llm_command_unknown_profile():
    daemon, bridge = _daemon_with_llm_profiles()
    await daemon._handle_message(root_msg("/llm nope", mid="om_l3"))
    assert daemon._llm_active == "deepseek"  # 未切
    assert any("未知 profile" in t for m, t in bridge.plain if m == "om_l3")


async def test_llm_command_no_profiles_configured():
    daemon, bridge, _ = make_daemon()  # 无 [llm]
    await daemon._handle_message(root_msg("/llm", mid="om_l4"))
    assert any("未配置调度器 LLM" in t for m, t in bridge.plain if m == "om_l4")


# ---------------------------------------------------------------------- #
# 后台任务（#68）：fdx bg run → 控制面 → daemon 拥有进程 → 完成唤回
# ---------------------------------------------------------------------- #


class _StubControl:
    """假控制面：只提供 base_url，用于验证身份 env 注入（不起真 HTTP）。"""

    base_url = "http://127.0.0.1:65000"

    def stop(self) -> None:
        pass


class _FakeProc:
    """假子进程：wait() 立刻返回给定退出码，供 _watch_bg_job 单测（不起真进程）。"""

    def __init__(self, rc: int) -> None:
        self._rc = rc
        self.pid = 4242

    async def wait(self) -> int:
        return self._rc


class _HangingProc:
    """假子进程：wait() 一直阻塞直到被 kill()——供超时路径单测。"""

    def __init__(self) -> None:
        self.pid = 999
        self.killed = False

    async def wait(self) -> int:
        if self.killed:
            return -9
        await asyncio.Event().wait()  # 永久阻塞，等 wait_for 超时取消

    def kill(self) -> None:
        self.killed = True


async def test_ctl_bg_run_rejects_bad_command():
    daemon, _, _ = make_daemon()
    status, payload = await daemon._ctl_bg_run("t1", {"command": []})
    assert status == 400
    status2, _ = await daemon._ctl_bg_run("t1", {"command": "not a list"})
    assert status2 == 400


async def test_ctl_bg_run_unknown_task():
    daemon, _, _ = make_daemon()
    status, payload = await daemon._ctl_bg_run("t404", {"command": ["python", "x.py"]})
    assert status == 404


async def test_launch_injects_bg_identity_env_and_revokes_on_close():
    daemon, bridge, created = make_daemon()
    daemon._control = _StubControl()  # type: ignore[assignment]
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    env = created[0].spawn.env
    assert env["FEISHU_DISPATCHER_URL"] == "http://127.0.0.1:65000"
    assert env["FEISHU_DISPATCHER_TASK_ID"] == "t1"
    token = env["FEISHU_DISPATCHER_TOKEN"]
    assert daemon._bg_tokens.get(token) == "t1"  # token → task 已登记
    await daemon._shutdown()
    assert token not in daemon._bg_tokens  # 关 session 后 token 作废


async def test_bg_job_completion_enqueues_resume_to_active_agent():
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    job = daemon.job_store.create(task_id="t1", command=["x"], cwd="C:/tmp/demo")
    daemon.job_store.update(job.job_id, exit_code=0, finished_at=time.time())
    await daemon._deliver_bg_result(daemon.job_store.get(job.job_id), 0)
    await wait_until(
        lambda: any(p.startswith("<bg_job_done>") for p in created[0].prompts)
    )
    assert any("🔔" in t and "j1" in t and "成功" in t for _, t in bridge.roots)
    await daemon._shutdown()


async def test_bg_job_completion_posts_visible_result_to_thread(tmp_path: Path):
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    out = tmp_path / "j1.log"
    out.write_bytes(b"line1\nfdx test done\n")
    job = daemon.job_store.create(task_id="t1", command=["pwsh", "-c", "x"], cwd="c")
    daemon.job_store.update(
        job.job_id, output_file=str(out), exit_code=0, finished_at=time.time()
    )
    await daemon._deliver_bg_result(daemon.job_store.get(job.job_id), 0)
    # 话题里出现**可见**的完成消息 + 输出尾部（不只是主线 🔔）
    thread_texts = bridge.texts("om_root1")
    assert any("后台任务 j1 完成" in t and "exit 0" in t for t in thread_texts)
    assert any("fdx test done" in t for t in thread_texts)
    await daemon._shutdown()


async def test_bg_job_completion_resumes_suspended_task():
    store = TaskStore(None)
    _seed_task(store, thread="om_s", session_id="sid_s", status="suspended")
    daemon, bridge, created = make_daemon(store=store)
    job = daemon.job_store.create(task_id="t1", command=["x"], cwd="c")
    await daemon._deliver_bg_result(daemon.job_store.get(job.job_id), 0)
    await wait_until(
        lambda: (
            created
            and created[0].prompts
            and created[0].prompts[0].startswith("<bg_job_done>")
        )
    )
    assert created[0].resume_session_id == "sid_s"  # 走 load_session
    await daemon._shutdown()


async def test_bg_job_completion_terminal_task_notifies_only():
    store = TaskStore(None)
    _seed_task(store, thread="om_x", status="done")
    daemon, bridge, created = make_daemon(store=store)
    job = daemon.job_store.create(task_id="t1", command=["x"], cwd="c")
    await daemon._deliver_bg_result(daemon.job_store.get(job.job_id), 1)
    assert created == []  # 终止任务不恢复
    assert any("未自动继续" in t and "失败" in t for _, t in bridge.roots)


async def test_bg_completions_merge_into_single_batch_and_one_turn():
    # #79：turn 在途时相继完成的多个 job 合并进同一批次，只唤回一轮
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(GatedAgent, store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    agent = created[0]
    sess = daemon._sessions["om_root1"]
    assert sess.turn_in_flight  # 首轮卡在 gate 上
    j1 = daemon.job_store.create(task_id="t1", command=["a"], cwd="c")
    daemon.job_store.update(j1.job_id, exit_code=0, finished_at=time.time())
    await daemon._deliver_bg_result(daemon.job_store.get(j1.job_id), 0)
    j2 = daemon.job_store.create(task_id="t1", command=["b"], cwd="c")
    daemon.job_store.update(j2.job_id, exit_code=0, finished_at=time.time())
    await daemon._deliver_bg_result(daemon.job_store.get(j2.job_id), 0)
    # 两个 job 合并进队尾同一批次，只入队一次
    assert sess.pending_bg is not None and len(sess.pending_bg.blocks) == 2
    assert sess.queue.qsize() == 1
    # 放行首轮 → worker 取走批次 → 一轮 prompt 同时含 j1 与 j2
    agent.gate.set()
    await wait_until(
        lambda: any("Job: j1" in p and "Job: j2" in p for p in agent.prompts)
    )
    assert sum(p.startswith("<bg_job_done>") for p in agent.prompts) == 1  # 只一轮
    assert sess.pending_bg is None  # 消费后清空
    await daemon._shutdown()


async def test_normal_reply_between_bg_completions_prevents_merge():
    # #79：两个 bg 完成之间夹了用户话题回复 → 断开合并邻接，保 FIFO（不 reorder）
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(GatedAgent, store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    sess = daemon._sessions["om_root1"]
    j1 = daemon.job_store.create(task_id="t1", command=["a"], cwd="c")
    await daemon._deliver_bg_result(daemon.job_store.get(j1.job_id), 0)
    batch1 = sess.pending_bg
    assert batch1 is not None
    await daemon._handle_message(thread_msg("hi there"))  # 用户回复夹在中间
    assert sess.pending_bg is None  # enqueue 清了合并邻接
    j2 = daemon.job_store.create(task_id="t1", command=["b"], cwd="c")
    await daemon._deliver_bg_result(daemon.job_store.get(j2.job_id), 0)
    # j2 另起批次、不并入 batch1（保 FIFO：j2 晚于 "hi there"）
    assert sess.pending_bg is not None and sess.pending_bg is not batch1
    assert len(batch1.blocks) == 1 and len(sess.pending_bg.blocks) == 1
    assert sess.queue.qsize() == 3  # [batch1, "hi there", batch2]
    await daemon._shutdown()


async def test_stop_drops_pending_bg_batch():
    # #79：/stop 立即停、丢弃未处理的 bg 批次（不排空后台结果再停）
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(CancelableAgent, store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await created[0].in_prompt.wait()  # 首轮在途
    sess = daemon._sessions["om_root1"]
    j1 = daemon.job_store.create(task_id="t1", command=["a"], cwd="c")
    daemon.job_store.update(j1.job_id, exit_code=0, finished_at=time.time())
    await daemon._deliver_bg_result(daemon.job_store.get(j1.job_id), 0)
    assert sess.pending_bg is not None and sess.queue.qsize() == 1  # 批次已入队
    await daemon._handle_message(thread_msg("/stop"))
    await wait_until(lambda: daemon.store.get("t1").status == "stopped")
    # agent 只收到过首轮，从未收到被丢弃的 bg 批次
    assert created[0].prompts == ["task"]
    assert sess.pending_bg is None
    await daemon._shutdown()


async def test_watch_bg_job_updates_status_and_delivers(tmp_path: Path):
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    out = tmp_path / "j1.log"
    out.write_bytes(b"training done\nfinal loss 0.1\n")
    job = daemon.job_store.create(task_id="t1", command=["python", "train.py"], cwd="c")
    daemon.job_store.update(job.job_id, output_file=str(out))
    await daemon._watch_bg_job(job.job_id, _FakeProc(0), open(out, "rb"), 0)
    updated = daemon.job_store.get(job.job_id)
    assert updated.status == "exited" and updated.exit_code == 0
    assert updated.finished_at > 0
    assert updated.timed_out is False
    await wait_until(lambda: any("final loss 0.1" in p for p in created[0].prompts))
    await daemon._shutdown()


async def test_watch_bg_job_timeout_kills_marks_and_still_resumes(tmp_path: Path):
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    out = tmp_path / "j1.log"
    out.write_bytes(b"stuck loading profile...\n")
    job = daemon.job_store.create(task_id="t1", command=["hang"], cwd="c")
    daemon.job_store.update(job.job_id, output_file=str(out))
    proc = _HangingProc()
    await daemon._watch_bg_job(job.job_id, proc, open(out, "rb"), 0.2)
    assert proc.killed  # 超时后被杀
    updated = daemon.job_store.get(job.job_id)
    assert updated.timed_out is True and updated.status == "killed"
    # 仍唤回 agent（prompt 标 Timed Out: yes），话题可见消息标「超时被杀」
    await wait_until(
        lambda: any(
            "<bg_job_done>" in p and "Timed Out: yes" in p for p in created[0].prompts
        )
    )
    assert any("超时被杀" in t for t in bridge.texts("om_root1"))
    await daemon._shutdown()


async def test_launch_bg_job_uses_devnull_stdin(monkeypatch, tmp_path: Path):
    # 回归 #68 真机坑：不给 DEVNULL 的话，子进程继承 daemon 控制台 stdin，
    # 交互式 shell profile（opam env）会卡死。断言 spawn 一定用 DEVNULL。
    daemon, bridge, created = make_daemon()
    daemon._bg_logs_dir = tmp_path / "bg"
    captured: dict = {}

    class _DummyProc:
        pid = 1

        async def wait(self) -> int:
            return 0

    async def fake_exec(*args, **kwargs):
        captured["kwargs"] = kwargs
        return _DummyProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await daemon._launch_bg_job("t1", ["python", "x.py"], str(tmp_path))
    assert captured["kwargs"]["stdin"] is asyncio.subprocess.DEVNULL
    await asyncio.sleep(0.05)  # 让 watcher 收尾
    await daemon._shutdown()


async def test_launch_bg_job_keeps_watcher_reference(tmp_path: Path):
    # 回归 #68：asyncio 只对 task 持弱引用，必须自存强引用否则 watcher 会被 GC
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    daemon._bg_logs_dir = tmp_path / "bg"
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    job = await daemon._launch_bg_job(
        "t1", [sys.executable, "-c", "import time; time.sleep(0.3)"], str(tmp_path)
    )
    name = f"bgjob-{job.job_id}"
    assert any(t.get_name() == name for t in daemon._bg_watchers)  # 未完成时被强引用
    await wait_until(
        lambda: daemon.job_store.get(job.job_id).status == "exited", timeout=10
    )
    # 完成后从集合移除（done_callback）
    await wait_until(lambda: not any(t.get_name() == name for t in daemon._bg_watchers))
    await daemon._shutdown()


def test_build_bg_prompt_formats_block(tmp_path: Path):
    daemon, _, _ = make_daemon()
    out = tmp_path / "j1.log"
    out.write_bytes(b"epoch 1\nepoch 2\nDONE\n")
    job = daemon.job_store.create(task_id="t1", command=["python", "train.py"], cwd="c")
    daemon.job_store.update(
        job.job_id, output_file=str(out), finished_at=job.created_at + 65.0
    )
    prompt = daemon._build_bg_prompt(daemon.job_store.get(job.job_id), 0)
    assert "<bg_job_done>" in prompt and "</bg_job_done>" in prompt
    assert "Job: j1" in prompt
    assert "Exit Code: 0" in prompt
    assert "python train.py" in prompt
    assert "1m05s" in prompt  # 65s → 1m05s
    assert "DONE" in prompt  # 输出尾部


async def test_launch_bg_job_real_subprocess_roundtrip(tmp_path: Path):
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    daemon._bg_logs_dir = tmp_path / "bg-logs"
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    job = await daemon._launch_bg_job(
        "t1", [sys.executable, "-c", "print('HELLO_BG_MARKER')"], str(tmp_path)
    )
    await wait_until(
        lambda: daemon.job_store.get(job.job_id).status == "exited", timeout=15
    )
    assert daemon.job_store.get(job.job_id).exit_code == 0
    # 真进程输出经 <bg_job_done> 入队回 agent
    await wait_until(
        lambda: any("HELLO_BG_MARKER" in p for p in created[0].prompts), timeout=15
    )
    await daemon._shutdown()


# ---- bg list / logs / kill（#70）---- #


class _KillableProc:
    def __init__(self) -> None:
        self.killed = False
        self.pid = 7

    def kill(self) -> None:
        self.killed = True


async def test_ctl_bg_list_scoped_to_task():
    daemon, _, _ = make_daemon()
    daemon.job_store.create(task_id="t1", command=["a"], cwd="c")
    daemon.job_store.create(task_id="t2", command=["b"], cwd="c")
    daemon.job_store.create(task_id="t1", command=["c"], cwd="c")
    status, payload = await daemon._ctl_bg_list("t1", {})
    assert status == 200
    ids = {j["job_id"] for j in payload["jobs"]}
    assert ids == {"j1", "j3"}  # 只本 task 的，t2 的 j2 不出现
    assert all("command" in j and "status" in j for j in payload["jobs"])


async def test_ctl_bg_logs_returns_tail_and_status(tmp_path: Path):
    daemon, _, _ = make_daemon()
    out = tmp_path / "j1.log"
    out.write_bytes(b"l1\nl2\nl3\nl4\n")
    job = daemon.job_store.create(task_id="t1", command=["python", "x"], cwd="c")
    daemon.job_store.update(
        job.job_id, output_file=str(out), status="exited", exit_code=0
    )
    status, payload = await daemon._ctl_bg_logs("t1", {"id": job.job_id, "tail": 2})
    assert status == 200
    assert payload["output"] == "l3\nl4"  # 末 2 行
    assert payload["exit_code"] == 0
    assert payload["command"] == "python x"


async def test_ctl_bg_logs_rejects_cross_task_and_unknown():
    daemon, _, _ = make_daemon()
    job = daemon.job_store.create(task_id="t2", command=["x"], cwd="c")
    status, _ = await daemon._ctl_bg_logs("t1", {"id": job.job_id, "tail": 10})
    assert status == 404  # 别的 task 的 job 看不到
    status2, _ = await daemon._ctl_bg_logs("t1", {"id": "j999", "tail": 10})
    assert status2 == 404  # 不存在


async def test_ctl_bg_kill_running_cross_task_and_not_running():
    daemon, _, _ = make_daemon()
    # 在跑的：kill 成功
    job = daemon.job_store.create(task_id="t1", command=["x"], cwd="c")
    proc = _KillableProc()
    daemon._bg_procs[job.job_id] = proc
    status, payload = await daemon._ctl_bg_kill("t1", {"id": job.job_id})
    assert status == 200 and payload["killed"] is True and proc.killed
    # 跨 task：拒绝
    job2 = daemon.job_store.create(task_id="t2", command=["y"], cwd="c")
    daemon._bg_procs[job2.job_id] = _KillableProc()
    s2, _ = await daemon._ctl_bg_kill("t1", {"id": job2.job_id})
    assert s2 == 404
    # 不在跑（无 proc）：killed False
    job3 = daemon.job_store.create(task_id="t1", command=["z"], cwd="c")
    daemon.job_store.update(job3.job_id, status="exited")
    s3, p3 = await daemon._ctl_bg_kill("t1", {"id": job3.job_id})
    assert s3 == 200 and p3["killed"] is False


async def test_launch_then_kill_real_subprocess(tmp_path: Path):
    store = TaskStore(None)
    daemon, bridge, created = make_daemon(store=store)
    daemon._bg_logs_dir = tmp_path / "bg-logs"
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    job = await daemon._launch_bg_job(
        "t1", [sys.executable, "-c", "import time; time.sleep(30)"], str(tmp_path)
    )
    await wait_until(lambda: job.job_id in daemon._bg_procs)
    status, payload = await daemon._ctl_bg_kill("t1", {"id": job.job_id})
    assert status == 200 and payload["killed"] is True
    # 被杀后很快结束（不等满 30s），且从「在跑」表清掉
    await wait_until(
        lambda: daemon.job_store.get(job.job_id).status == "exited", timeout=10
    )
    await wait_until(lambda: job.job_id not in daemon._bg_procs)
    await daemon._shutdown()


async def test_launch_threads_agent_env_into_spawn():
    # [agents.<名>].env 声明的追加环境变量应进到该 agent 的 AcpAgent spawn.env
    # （codex 靠 CODEX_PATH 复用全局 codex）。无控制面时不叠 bg 身份 env。
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"codex": ["codex-acp"]},
        agent_env={"codex": {"CODEX_PATH": "codex"}},
        projects={
            "demo": Project(
                name="demo", path=Path("C:/tmp/demo"), default_agent="codex"
            )
        },
        throttle_window=0.01,
        stream_mode="text",
    )
    daemon = _Daemon(cfg, store=TaskStore(None), project_store=ProjectStore(None))
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []

    def factory(spawn, on_output, on_action=None, *, resume_session_id=None):
        agent = FakeAgent(
            spawn, on_output, on_action, resume_session_id=resume_session_id
        )
        created.append(agent)
        return agent

    daemon._make_agent = factory  # type: ignore[method-assign]
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    assert created[0].spawn.env.get("CODEX_PATH") == "codex"
    await daemon._shutdown()
