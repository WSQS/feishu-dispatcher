"""ACP session_update 流式格式化的单元测试。"""

from __future__ import annotations

from acp import (
    update_agent_message_text,
    update_agent_thought_text,
    start_tool_call,
    update_tool_call,
)

from feishu_dispatcher.acp_client import _StreamFormatter, _extract_action


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
