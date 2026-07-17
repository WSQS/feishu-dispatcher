"""ACP session_update 文本抽取的单元测试。"""

from __future__ import annotations

from acp import (
    update_agent_message_text,
    update_agent_thought_text,
    start_tool_call,
    update_tool_call,
)

from feishu_dispatcher.acp_client import _extract_text


def test_agent_message_chunk_extracts_plain_text():
    update = update_agent_message_text("hello world")
    assert _extract_text(update) == "hello world"


def test_agent_thought_chunk_gets_thought_prefix():
    update = update_agent_thought_text("let me think")
    assert _extract_text(update) == "💭 let me think"


def test_tool_call_start_emits_title_line():
    update = start_tool_call("tc1", "Editing src/foo.py", kind="edit")
    out = _extract_text(update)
    assert "Editing src/foo.py" in out
    assert "🔧" in out


def test_tool_call_completed_emits_checkmark():
    update = update_tool_call("tc1", title="Editing src/foo.py", status="completed")
    out = _extract_text(update)
    assert "✅" in out
    assert "Editing src/foo.py" in out


def test_tool_call_failed_emits_cross():
    update = update_tool_call("tc1", title="Running tests", status="failed")
    out = _extract_text(update)
    assert "❌" in out


def test_tool_call_in_progress_emits_nothing():
    update = update_tool_call("tc1", title="x", status="in_progress")
    assert _extract_text(update) == ""


def test_agent_message_chunk_empty_text():
    update = update_agent_message_text("")
    assert _extract_text(update) == ""