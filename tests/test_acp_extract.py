"""ACP session_update 流式格式化的单元测试。"""

from __future__ import annotations

from acp import (
    update_agent_message_text,
    update_agent_thought_text,
    start_tool_call,
    update_tool_call,
)
from acp.connection import StreamDirection
from acp.schema import ToolCallLocation

import asyncio
import logging
from types import SimpleNamespace as NS

import pytest

from feishu_dispatcher.acp_client import (
    AcpAgent,
    AgentSpawn,
    _Callbacks,
    _ClientImpl,
    _MethodNotFoundTap,
    _StreamFormatter,
    _extract_action,
    _extract_model,
    _extract_model_options,
    _extract_tool_detail,
    _extract_usage_tokens,
    _proc_tree_pids,
)


async def _noop_out(_t: str) -> None:
    pass


def _agent(start_timeout=5.0, resume=None, stall_timeout=0) -> AcpAgent:
    return AcpAgent(
        AgentSpawn(command=["x"], cwd="."),
        _noop_out,
        resume_session_id=resume,
        start_timeout=start_timeout,
        stall_timeout=stall_timeout,
    )


def fmt(update) -> str:
    """单条 update 用全新格式化器（等价于旧的无状态 _extract_text）。"""
    return _StreamFormatter().format(update)


# --- 审计动作抽取 (_extract_action) ------------------------------------- #


def test_extract_action_from_tool_call():
    action = _extract_action(start_tool_call("tc1", "Editing src/foo.py", kind="edit"))
    assert action == {"kind": "edit", "title": "Editing src/foo.py"}


def test_extract_action_ignores_message_and_thought():
    assert _extract_action(update_agent_message_text("hi")) is None
    assert _extract_action(update_agent_thought_text("thinking")) is None


def test_extract_action_ignores_tool_call_update():
    # 只认首次通告，完成/失败的状态更新不重复记
    upd = update_tool_call("tc1", title="Editing src/foo.py", status="completed")
    assert _extract_action(upd) is None


def test_extract_action_skips_titleless_tool_call():
    assert _extract_action(start_tool_call("tc1", "", kind="edit")) is None


# --- 当前模型抽取 (_extract_model) ------------------------------------- #


def test_extract_model_by_id():
    # opencode：config_options 里 id=="model" 的 select 的 current_value
    resp = NS(
        config_options=[
            NS(id="mode", category="mode", current_value="build"),
            NS(
                id="model",
                category="model",
                current_value="ns-deepseek/deepseek-v4-pro",
            ),
        ]
    )
    assert _extract_model(resp) == "ns-deepseek/deepseek-v4-pro"


def test_extract_model_by_category():
    resp = NS(config_options=[NS(id="x", category="model", current_value="glm-5")])
    assert _extract_model(resp) == "glm-5"


def test_extract_model_absent_returns_empty():
    # copilot：只有 mode/agent/allow_all，无 model 项
    resp = NS(
        config_options=[
            NS(id="mode", category="mode", current_value="agent"),
            NS(id="allow_all", category="permissions", current_value="off"),
        ]
    )
    assert _extract_model(resp) == ""


def test_extract_model_no_config_options():
    assert _extract_model(NS()) == ""  # 无 config_options 字段也不抛


def test_extract_model_options_lists_values():
    resp = NS(
        config_options=[
            NS(id="mode", category="mode", current_value="build", options=[]),
            NS(
                id="model",
                category="model",
                current_value="deepseek-v4",
                options=[
                    NS(value="deepseek-v4", name="DeepSeek V4"),
                    NS(value="zhipuai/glm-5", name="GLM-5"),
                ],
            ),
        ]
    )
    assert _extract_model_options(resp) == ["deepseek-v4", "zhipuai/glm-5"]


def test_extract_model_options_absent_returns_empty():
    resp = NS(config_options=[NS(id="mode", category="mode", current_value="x")])
    assert _extract_model_options(resp) == []


# --- token 用量抽取 (_extract_usage_tokens) --------------------------- #


def test_extract_usage_tokens_from_response():
    resp = NS(usage=NS(total_tokens=3210, input_tokens=3000, output_tokens=210))
    assert _extract_usage_tokens(resp) == 3210


def test_extract_usage_tokens_absent_returns_none():
    assert _extract_usage_tokens(NS(usage=None)) is None
    assert _extract_usage_tokens(NS()) is None  # 无 usage 字段也不抛


def test_extract_usage_tokens_ignores_non_int_and_negative():
    assert _extract_usage_tokens(NS(usage=NS(total_tokens=None))) is None
    assert _extract_usage_tokens(NS(usage=NS(total_tokens=-1))) is None


async def test_client_captures_streamed_usage_update():
    from feishu_dispatcher.acp_client import _Callbacks, _ClientImpl

    async def noop(_t: str) -> None:
        pass

    impl = _ClientImpl(_Callbacks(on_output=noop))
    assert impl.usage_tokens() is None
    await impl.session_update(
        "s", NS(session_update="usage_update", used=1234, size=200000)
    )
    assert impl.usage_tokens() == 1234
    # 后续更新覆盖为最新值
    await impl.session_update(
        "s", NS(session_update="usage_update", used=5678, size=200000)
    )
    assert impl.usage_tokens() == 5678
    # 回合重置后清空
    impl.reset_formatter()
    assert impl.usage_tokens() is None


async def test_client_usage_update_suppressed_during_load():
    from feishu_dispatcher.acp_client import _Callbacks, _ClientImpl

    async def noop(_t: str) -> None:
        pass

    impl = _ClientImpl(_Callbacks(on_output=noop))
    impl.set_suppress(True)  # load_session 重放历史期间不计
    await impl.session_update(
        "s", NS(session_update="usage_update", used=999, size=200000)
    )
    assert impl.usage_tokens() is None


# --- 收尾回复累积 (_ClientImpl.last_message) ---------------------------- #


async def test_client_accumulates_agent_message_not_thought():
    from feishu_dispatcher.acp_client import _Callbacks, _ClientImpl

    async def noop(_t: str) -> None:
        pass

    impl = _ClientImpl(_Callbacks(on_output=noop))
    await impl.session_update("s", update_agent_message_text("Hello "))
    await impl.session_update("s", update_agent_thought_text("(思考)"))  # 不计入
    await impl.session_update("s", update_agent_message_text("world"))
    assert impl.last_message() == "Hello world"
    # 回合重置后清空
    impl.reset_formatter()
    assert impl.last_message() == ""


# --- 单条 update（无前置状态）------------------------------------------- #


def test_agent_message_chunk_extracts_plain_text():
    assert fmt(update_agent_message_text("hello world")) == "hello world"


def test_agent_thought_chunk_gets_thought_prefix():
    assert fmt(update_agent_thought_text("let me think")) == "💭 let me think"


def test_tool_call_start_emits_title_line():
    out = fmt(start_tool_call("tc1", "Editing src/foo.py", kind="edit"))
    assert "Editing src/foo.py" in out
    assert "🔧" in out


def test_tool_call_completed_emits_checkmark():
    out = fmt(update_tool_call("tc1", title="Editing src/foo.py", status="completed"))
    assert "✅" in out
    assert "Editing src/foo.py" in out


def test_tool_call_failed_emits_cross():
    out = fmt(update_tool_call("tc1", title="Running tests", status="failed"))
    assert "❌" in out


def test_tool_call_in_progress_emits_nothing():
    assert fmt(update_tool_call("tc1", title="x", status="in_progress")) == ""


# --- 工具行显示命令/路径（#46，卡片侧）---------------------------------- #


def test_extract_tool_detail_command_from_raw_input():
    upd = update_tool_call("c1", raw_input={"command": "git status"})
    assert _extract_tool_detail(upd) == "git status"


def test_extract_tool_detail_command_list_joined():
    upd = update_tool_call("c1", raw_input={"command": ["git", "status"]})
    assert _extract_tool_detail(upd) == "git status"


def test_extract_tool_detail_path_from_locations_for_file_kind():
    upd = start_tool_call(
        "e1", "Edit", kind="edit", locations=[ToolCallLocation(path="src/foo.py")]
    )
    assert _extract_tool_detail(upd) == "src/foo.py"


def test_extract_tool_detail_execute_cwd_locations_ignored():
    # 命令类的 locations 是 cwd（实测 opencode bash pending），不能当命令/文件显示
    upd = start_tool_call(
        "b1", "bash", kind="execute", locations=[ToolCallLocation(path="/work")]
    )
    assert _extract_tool_detail(upd) == ""


def test_extract_tool_detail_none_when_no_signal():
    assert _extract_tool_detail(update_agent_message_text("hi")) == ""


def test_command_shown_immediately_when_present_at_start():
    # Pattern A：命令在初次事件的 raw_input 里 → 立刻显示
    out = fmt(
        start_tool_call("c1", "bash", kind="execute", raw_input={"command": "ls -la"})
    )
    assert "🔧 bash: ls -la" in out


def test_command_appears_at_in_progress_like_opencode():
    # 实测 opencode 序列：pending 只有 cwd → 不出行；命令在 in_progress 才带
    f = _StreamFormatter()
    out0 = f.format(
        start_tool_call(
            "c1",
            "bash",
            kind="execute",
            raw_input={"cwd": "/tmp"},
            locations=[ToolCallLocation(path="/tmp")],
        )
    )
    assert out0 == ""  # 命令类初次无命令 → 延后，不显示 cwd
    out1 = f.format(
        update_tool_call(
            "c1",
            title="git status",
            status="in_progress",
            raw_input={"command": "git status", "workdir": "/tmp"},
        )
    )
    assert "🔧" in out1 and "bash: git status" in out1  # 泛称 label + 命令
    # 多条 in_progress 去重
    out2 = f.format(
        update_tool_call(
            "c1",
            title="git status",
            status="in_progress",
            raw_input={"command": "git status"},
        )
    )
    assert out2 == ""
    # 完成事件稀疏（raw_input 缺），用记住的 label+detail 合成
    out3 = f.format(update_tool_call("c1", title="git status", status="completed"))
    assert "✅" in out3 and "bash: git status" in out3


def test_command_kind_other_powershell_from_in_progress():
    # PowerShell 是 kind=other，同样按「有 command 字段」显示（不看 title/kind）
    f = _StreamFormatter()
    f.format(start_tool_call("p1", "PowerShell", kind="other", raw_input={"cwd": "/w"}))
    out = f.format(
        update_tool_call(
            "p1",
            title="PowerShell",
            status="in_progress",
            raw_input={"command": "Get-ChildItem"},
        )
    )
    assert "🔧 PowerShell: Get-ChildItem" in out


def test_file_tool_shows_path_at_start():
    out = fmt(
        start_tool_call(
            "e1",
            "Edit",
            kind="edit",
            locations=[ToolCallLocation(path="src/foo.py")],
            raw_input={"path": "src/foo.py", "content": "x"},
        )
    )
    assert "🔧 Edit: src/foo.py" in out


def test_long_command_truncated_to_one_line():
    long = "echo " + "x" * 300
    out = fmt(
        start_tool_call("c1", "bash", kind="execute", raw_input={"command": long})
    )
    line = out.strip()
    assert line.startswith("🔧 bash: ")
    assert line.endswith("…")
    assert len(line) < 130


def test_completion_without_prior_start_uses_event_title():
    # 只见完成事件（无先前 pending）：退回事件自带 title
    out = fmt(update_tool_call("c1", title="Running tests", status="completed"))
    assert "✅ Running tests" in out


def test_agent_message_chunk_empty_text():
    assert fmt(update_agent_message_text("")) == ""


def test_plan_update_renders_entries_with_status_marks():
    from acp.schema import AgentPlanUpdate, PlanEntry

    update = AgentPlanUpdate(
        session_update="plan",
        entries=[
            PlanEntry(content="read files", priority="medium", status="completed"),
            PlanEntry(content="write code", priority="high", status="in_progress"),
            PlanEntry(content="run tests", priority="high", status="pending"),
        ],
    )
    out = fmt(update)
    assert "📋" in out
    assert "☑️ read files" in out
    assert "🔄 write code" in out
    assert "⬜ run tests" in out


def test_plan_update_with_no_entries_emits_nothing():
    from acp.schema import AgentPlanUpdate

    assert fmt(AgentPlanUpdate(session_update="plan", entries=[])) == ""


# --- 跨 chunk 状态（💭 碎前缀修复的核心）-------------------------------- #


def test_consecutive_thought_chunks_prefix_only_once():
    """逐 token 的连续 thought 只在开头加一次 💭，后续原样追加。"""
    f = _StreamFormatter()
    assert f.format(update_agent_thought_text("The")) == "💭 The"
    assert f.format(update_agent_thought_text(" user")) == " user"
    assert f.format(update_agent_thought_text(" asks")) == " asks"
    # 拼接后是干净的一行「💭 The user asks」而非「💭 The💭 user💭 asks」


def test_message_after_thought_gets_newline_separator():
    f = _StreamFormatter()
    f.format(update_agent_thought_text("thinking…"))
    assert f.format(update_agent_message_text("answer")) == "\nanswer"


def test_consecutive_messages_no_extra_prefix():
    f = _StreamFormatter()
    assert f.format(update_agent_message_text("a")) == "a"
    assert f.format(update_agent_message_text("b")) == "b"


def test_thought_after_toolcall_reprefixes():
    """离散事件（tool_call）后新的 thought 段重新加 💭。"""
    f = _StreamFormatter()
    f.format(update_agent_thought_text("t1"))
    f.format(start_tool_call("tc1", "Editing", kind="edit"))
    assert f.format(update_agent_thought_text("t2")) == "💭 t2"


def test_reset_reprefixes_next_thought():
    """回合重置后，本轮首个 thought 重新加 💭。"""
    f = _StreamFormatter()
    f.format(update_agent_thought_text("a"))
    f.reset()
    assert f.format(update_agent_thought_text("b")) == "💭 b"


def test_empty_thought_chunk_does_not_change_state():
    f = _StreamFormatter()
    assert f.format(update_agent_thought_text("start")) == "💭 start"
    assert f.format(update_agent_thought_text("")) == ""
    # 空 chunk 不重置连续态，后续 thought 仍不重复加 💭
    assert f.format(update_agent_thought_text(" more")) == " more"


# --- 未接线 client 方法观察器 (_MethodNotFoundTap) --------------------- #


def _event(direction, message):
    return NS(direction=direction, message=message)


def test_method_not_found_tap_names_the_method(caplog):
    tap = _MethodNotFoundTap()
    tap(_event(StreamDirection.INCOMING, {"id": 7, "method": "cursor/doThing"}))
    with caplog.at_level(logging.WARNING):
        tap(
            _event(
                StreamDirection.OUTGOING,
                {"id": 7, "error": {"code": -32601, "message": "Method not found"}},
            )
        )
    assert "cursor/doThing" in caplog.text


def test_method_not_found_tap_ignores_success_and_other_errors(caplog):
    tap = _MethodNotFoundTap()
    tap(_event(StreamDirection.INCOMING, {"id": 1, "method": "fs/read_text_file"}))
    tap(
        _event(
            StreamDirection.INCOMING, {"id": 2, "method": "session/request_permission"}
        )
    )
    with caplog.at_level(logging.WARNING):
        tap(_event(StreamDirection.OUTGOING, {"id": 1, "error": {"code": -32603}}))
        tap(_event(StreamDirection.OUTGOING, {"id": 2, "result": {"ok": True}}))
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_method_not_found_tap_bounded_pops_pending():
    # 每条 agent 请求都会有一条我们回的响应弹出登记，_pending 不无限增长
    tap = _MethodNotFoundTap()
    tap(_event(StreamDirection.INCOMING, {"id": 5, "method": "x/y"}))
    assert tap._pending
    tap(_event(StreamDirection.OUTGOING, {"id": 5, "result": {}}))
    assert not tap._pending


# --- Windows 进程树计算 (_proc_tree_pids, #92) ------------------------- #


def test_proc_tree_pids_collects_cmd_powershell_node_chain():
    # 复刻 cursor 的 shim 链：daemon → cmd → powershell → node（+ 旁支）
    ppid_of = {
        1000: 1,  # daemon
        2000: 1000,  # cmd.exe（传入的 root）
        3000: 2000,  # powershell
        4000: 3000,  # node
        4100: 3000,  # 同 powershell 下另一个后代
        5000: 1,  # 无关进程
        2001: 1000,  # daemon 的另一个 cmd（不该被算进 2000 的树）
    }
    assert set(_proc_tree_pids(2000, ppid_of)) == {2000, 3000, 4000, 4100}


def test_proc_tree_pids_leaf_returns_only_self():
    assert _proc_tree_pids(4000, {4000: 3000}) == [4000]


def test_proc_tree_pids_ignores_ppid_cycle():
    # 防御坏快照里的环：不死循环、每个 pid 只算一次
    assert set(_proc_tree_pids(10, {10: 11, 11: 10})) == {10, 11}


# --- 握手/会话恢复超时 (AcpAgent.start, #94) --------------------------- #


def test_start_timeout_normalizes_nonpositive():
    # <=0 归一化为 None（不超时）；正值保留
    assert _agent(start_timeout=0)._start_timeout is None
    assert _agent(start_timeout=-1)._start_timeout is None
    assert _agent(start_timeout=30)._start_timeout == 30


def test_stall_timeout_normalizes_nonpositive():
    # <=0 关闭看门狗；正值保留（#208）
    assert _agent(stall_timeout=0)._stall_timeout is None
    assert _agent(stall_timeout=-1)._stall_timeout is None
    assert _agent(stall_timeout=900)._stall_timeout == 900


async def test_session_update_touches_progress():
    impl = _ClientImpl(_Callbacks(_noop_out))
    impl.reset_formatter()
    t0 = impl.last_progress_at
    await asyncio.sleep(0.02)
    await impl.session_update("s", update_agent_message_text("hi"))
    assert impl.last_progress_at > t0


async def test_prompt_stall_cancels_when_no_progress():
    # prompt 挂起且无 session_update → 看门狗 cancel，stalled=True
    class _FakeConn:
        def __init__(self) -> None:
            self.cancel_calls = 0
            self._done = asyncio.Event()

        async def prompt(self, **_k):
            await self._done.wait()
            return NS(stop_reason="cancelled", usage=None)

        async def cancel(self, **_k):
            self.cancel_calls += 1
            self._done.set()

    agent = _agent(stall_timeout=0.15)
    conn = _FakeConn()
    agent._conn = conn  # type: ignore[assignment]
    agent._session_id = "s-stall"
    agent._client_impl = _ClientImpl(_Callbacks(_noop_out))
    reason = await agent.prompt("hang")
    assert reason == "cancelled"
    assert agent.stalled
    assert conn.cancel_calls == 1


async def test_prompt_stall_disabled_does_not_cancel():
    # stall_timeout<=0：无看门狗；prompt 自行结束后 stalled 仍 False
    class _FakeConn:
        def __init__(self) -> None:
            self.cancel_calls = 0

        async def prompt(self, **_k):
            await asyncio.sleep(0.05)
            return NS(stop_reason="end_turn", usage=None)

        async def cancel(self, **_k):
            self.cancel_calls += 1

    agent = _agent(stall_timeout=0)
    conn = _FakeConn()
    agent._conn = conn  # type: ignore[assignment]
    agent._session_id = "s-off"
    agent._client_impl = _ClientImpl(_Callbacks(_noop_out))
    reason = await agent.prompt("ok")
    assert reason == "end_turn"
    assert not agent.stalled
    assert conn.cancel_calls == 0


async def test_prompt_stall_resets_on_session_update():
    # 持续有 session_update 时不误杀；停更后才 cancel
    class _FakeConn:
        def __init__(self) -> None:
            self.cancel_calls = 0
            self._done = asyncio.Event()

        async def prompt(self, **_k):
            await self._done.wait()
            return NS(stop_reason="cancelled", usage=None)

        async def cancel(self, **_k):
            self.cancel_calls += 1
            self._done.set()

    agent = _agent(stall_timeout=0.2)
    conn = _FakeConn()
    agent._conn = conn  # type: ignore[assignment]
    agent._session_id = "s-prog"
    impl = _ClientImpl(_Callbacks(_noop_out))
    agent._client_impl = impl

    async def drip():
        for _ in range(6):
            await asyncio.sleep(0.08)
            if conn.cancel_calls:
                return
            await impl.session_update("s-prog", update_agent_message_text("."))

    drip_task = asyncio.create_task(drip())
    reason = await agent.prompt("slow")
    await drip_task
    assert reason == "cancelled"
    assert agent.stalled
    assert conn.cancel_calls == 1


async def test_await_start_passes_through_fast_result():
    async def quick():
        return 42

    assert await _agent(start_timeout=5)._await_start(quick(), "initialize") == 42


async def test_await_start_raises_timeout_on_hang():
    async def hang():
        await asyncio.sleep(10)

    with pytest.raises(TimeoutError, match="load_session"):
        await _agent(start_timeout=0.05)._await_start(hang(), "load_session")


async def test_start_raises_timeout_when_load_session_hangs(monkeypatch):
    # 复刻真机：initialize 成功但 load_session 永不返回 → start() 抛 TimeoutError 而非挂起
    import feishu_dispatcher.acp_client as mod

    class _FakeTransport:
        async def __aenter__(self):
            return (object(), object(), NS(stderr=None))

        async def __aexit__(self, *a):
            return False

    class _FakeConn:
        async def initialize(self, **_k):
            return NS(agent_info="fake", agent_capabilities="caps")

        async def load_session(self, **_k):
            await asyncio.sleep(10)  # 卡死

    monkeypatch.setattr(mod, "spawn_stdio_transport", lambda *a, **k: _FakeTransport())
    monkeypatch.setattr(mod.acp, "connect_to_agent", lambda *a, **k: _FakeConn())

    agent = _agent(start_timeout=0.1, resume="sess-1")
    with pytest.raises(TimeoutError, match="load_session"):
        await agent.start()
