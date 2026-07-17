"""daemon 生命周期集成测试（fake bridge + fake agent，不碰网络/子进程）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from feishu_dispatcher.config import Config, Project
from feishu_dispatcher.daemon import _Daemon
from feishu_dispatcher.feishu import IncomingMessage


class FakeBridge:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.stopped = False

    def reply_in_thread(self, root_message_id: str, text: str) -> str:
        self.replies.append((root_message_id, text))
        return f"om_reply_{len(self.replies)}"

    def stop(self) -> None:
        self.stopped = True

    def texts(self, root: str | None = None) -> list[str]:
        return [t for r, t in self.replies if root is None or r == root]


class FakeAgent:
    def __init__(self, spawn, on_output) -> None:
        self.spawn = spawn
        self.on_output = on_output
        self.prompts: list[str] = []
        self.start_count = 0
        self.closed = False

    async def start(self) -> None:
        self.start_count += 1

    async def prompt(self, text: str) -> None:
        self.prompts.append(text)
        await self.on_output(f"echo:{text}")

    async def aclose(self) -> None:
        self.closed = True


class FailingAgent(FakeAgent):
    async def prompt(self, text: str) -> None:
        raise RuntimeError("boom")


def make_daemon(
    agent_cls: type[FakeAgent] = FakeAgent,
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
    )
    daemon = _Daemon(cfg)
    bridge = FakeBridge()
    daemon._bridge = bridge  # 绕过 run()，直接注入
    created: list[FakeAgent] = []

    def factory(spawn, on_output):
        agent = agent_cls(spawn, on_output)
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


# ---------------------------------------------------------------------- #
# R11: max_agents 并发上限
# ---------------------------------------------------------------------- #


def make_daemon_with_limit(
    max_agents: int, agent_cls: type[FakeAgent] = FakeAgent
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        max_agents=max_agents,
    )
    daemon = _Daemon(cfg)
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []

    def factory(spawn, on_output):
        agent = agent_cls(spawn, on_output)
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
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        sender_whitelist=sender_whitelist,
    )
    daemon = _Daemon(cfg)
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []
    daemon._make_agent = lambda spawn, on_output: (
        created.append(FakeAgent(spawn, on_output)) or created[-1]
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
        agents={"copilot": ["copilot", "--acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
    )
    daemon = _Daemon(cfg, discover=True)
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []
    daemon._make_agent = lambda spawn, on_output: (
        created.append(FakeAgent(spawn, on_output)) or created[-1]
    )
    await daemon._handle_message(root_msg("/run demo task"))
    assert created == []
    assert bridge.texts() == []
