"""daemon 生命周期集成测试（fake bridge + fake agent，不碰网络/子进程）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from feishu_dispatcher.config import Config, Project
from feishu_dispatcher.daemon import _Daemon
from feishu_dispatcher.feishu import IncomingMessage
from feishu_dispatcher.scheduler import LLMResponse, ToolCall
from feishu_dispatcher.store import SessionRecord, SessionStore


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
    def __init__(self, spawn, on_output, *, resume_session_id=None) -> None:
        self.spawn = spawn
        self.on_output = on_output
        self.resume_session_id = resume_session_id
        self.prompts: list[str] = []
        self.start_count = 0
        self.closed = False
        self.session_id = resume_session_id

    async def start(self) -> None:
        self.start_count += 1
        # 新会话给个假 id；恢复则沿用传入的 session_id
        if self.session_id is None:
            self.session_id = f"fake_sid_{id(self)}"

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
    *,
    stream_mode: str = "text",
    store: SessionStore | None = None,
    idle_timeout: float = 1800.0,
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        idle_timeout=idle_timeout,
        stream_mode=stream_mode,
    )
    daemon = _Daemon(cfg, store=store or SessionStore(None))
    bridge = FakeBridge()
    daemon._bridge = bridge  # 绕过 run()，直接注入
    created: list[FakeAgent] = []

    def factory(spawn, on_output, *, resume_session_id=None):
        agent = agent_cls(spawn, on_output, resume_session_id=resume_session_id)
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
    max_agents: int,
    agent_cls: type[FakeAgent] = FakeAgent,
    *,
    store: SessionStore | None = None,
) -> tuple[_Daemon, FakeBridge, list[FakeAgent]]:
    cfg = Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        max_agents=max_agents,
        stream_mode="text",
    )
    daemon = _Daemon(cfg, store=store or SessionStore(None))
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []

    def factory(spawn, on_output, *, resume_session_id=None):
        agent = agent_cls(spawn, on_output, resume_session_id=resume_session_id)
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
        agents={"copilot": ["copilot", "--acp"]},
        projects={"demo": Project(name="demo", path=Path("C:/tmp/demo"))},
        throttle_window=0.01,
        sender_whitelist=sender_whitelist,
        stream_mode=stream_mode,
    )
    daemon = _Daemon(cfg)
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []
    daemon._make_agent = lambda spawn, on_output, *, resume_session_id=None: (
        created.append(FakeAgent(spawn, on_output, resume_session_id=resume_session_id))
        or created[-1]
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
        stream_mode="text",
    )
    daemon = _Daemon(cfg, discover=True)
    bridge = FakeBridge()
    daemon._bridge = bridge
    created: list[FakeAgent] = []
    daemon._make_agent = lambda spawn, on_output, *, resume_session_id=None: (
        created.append(FakeAgent(spawn, on_output, resume_session_id=resume_session_id))
        or created[-1]
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
            "echo:do stuff" in card["elements"][0]["text"]["content"]
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


async def test_run_persists_session_record():
    store = SessionStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: store.get("om_root1") is not None)
    rec = store.get("om_root1")
    assert rec.project_name == "demo"
    assert rec.agent_label == "copilot"
    assert rec.session_id == created[0].session_id
    await daemon._shutdown()


async def test_recovery_after_restart_uses_load_session():
    store = SessionStore(None)  # 内存 store 跨两个 daemon 实例共享 = 模拟重启
    d1, b1, c1 = make_daemon(store=store)
    await d1._handle_message(root_msg("/run demo task1"))
    await wait_until(lambda: store.get("om_root1") is not None)
    saved_sid = store.get("om_root1").session_id
    await d1._shutdown()  # 服务停止；store 记录保留

    # 新 daemon，内存 _sessions 空
    d2, b2, c2 = make_daemon(store=store)
    assert d2._sessions == {}
    # 在旧话题里回复 → 触发惰性恢复
    await d2._handle_message(thread_msg("follow up", root="om_root1", mid="om_t2"))
    await wait_until(lambda: c2 and c2[0].prompts == ["follow up"])
    # 用 load_session 恢复：resume_session_id == 之前持久化的 session_id
    assert c2[0].resume_session_id == saved_sid
    assert c2[0].start_count == 1
    assert any("恢复" in t for t in b2.texts("om_root1"))
    await d2._shutdown()


async def test_reply_to_unknown_topic_notifies_not_silent():
    daemon, bridge, created = make_daemon()  # 空 store
    await daemon._handle_message(thread_msg("hello", root="om_unknown", mid="om_x"))
    assert created == []
    assert any("没有活跃 agent" in t for t in bridge.texts("om_unknown"))


async def test_stop_removes_persisted_record():
    store = SessionStore(None)
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: store.get("om_root1") is not None)
    await daemon._handle_message(thread_msg("/stop"))
    await wait_until(lambda: store.get("om_root1") is None)


async def test_recovery_fails_when_agent_unconfigured():
    store = SessionStore(None)
    # 手工塞一条 agent 已不在配置里的记录
    store.put(SessionRecord("om_orphan", "demo", "ghost", "sid_x", "C:/tmp/demo"))
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(thread_msg("hello", root="om_orphan", mid="om_y"))
    assert created == []
    assert any("未配置" in t for t in bridge.texts("om_orphan"))
    assert store.get("om_orphan") is None  # 陈旧记录被清掉


async def test_orphan_stop_forgets_without_recovering():
    store = SessionStore(None)
    store.put(SessionRecord("om_orphan", "demo", "copilot", "sid_x", "C:/tmp/demo"))
    daemon, bridge, created = make_daemon(store=store)
    await daemon._handle_message(thread_msg("/stop", root="om_orphan", mid="om_z"))
    assert created == []  # 没为了停而恢复
    assert store.get("om_orphan") is None
    assert any("已结束" in t for t in bridge.texts("om_orphan"))


async def test_recovery_respects_max_agents():
    store = SessionStore(None)
    daemon, bridge, created = make_daemon_with_limit(max_agents=1, store=store)
    # 占住唯一槽位
    await daemon._handle_message(root_msg("/run demo task1", mid="om_r1"))
    await wait_until(lambda: created and created[0].prompts == ["task1"])
    # 另有一条可恢复记录，但已达上限 → 拒绝恢复
    store.put(SessionRecord("om_orphan", "demo", "copilot", "sid_x", "C:/tmp/demo"))
    await daemon._handle_message(thread_msg("hi", root="om_orphan", mid="om_r2"))
    assert len(created) == 1  # 未恢复
    assert any("上限" in t for t in bridge.texts("om_orphan"))
    await daemon._shutdown()


# ---------------------------------------------------------------------- #
# 空闲挂起 + max_agents 名额释放（坑 1/2/3）
# ---------------------------------------------------------------------- #


async def test_idle_timeout_suspends_but_keeps_record_recoverable():
    store = SessionStore(None)
    daemon, bridge, created = make_daemon(store=store, idle_timeout=0.1)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    saved_sid = created[0].session_id
    # 空闲超时 → 挂起：关进程、腾名额、但记录保留
    await wait_until(lambda: any("💤" in t for t in bridge.texts("om_root1")))
    await wait_until(lambda: "om_root1" not in daemon._sessions)  # 名额已释放
    assert created[0].closed
    assert store.get("om_root1") is not None  # 记录保留（区别于 /stop）

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


async def test_agent_error_notifies_main_line():
    daemon, bridge, created = make_daemon(agent_cls=FailingAgent)
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: any("❌" in t and "出错" in t for _, t in bridge.roots))
    await wait_until(lambda: "om_root1" not in daemon._sessions)


async def test_list_agents_reports_state_and_turns():
    daemon, bridge, created = make_daemon()
    await daemon._handle_message(root_msg("/run demo task"))
    await wait_until(lambda: created and created[0].prompts == ["task"])
    await wait_until(
        lambda: (
            daemon._sched_list_agents()
            and daemon._sched_list_agents()[0]["state"] == "idle"
        )
    )
    info = daemon._sched_list_agents()[0]
    assert info["project"] == "demo"
    assert info["turns"] == 1
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
