import json
from datetime import datetime, timezone

import pytest

from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputStarted,
    AgentPlanEntry,
    AgentPlanUpdated,
    SessionErrorOccurred,
    SessionEvent,
    SessionInputAccepted,
    SessionStateChanged,
    ToolCallObserved,
    session_event_from_dict,
    session_event_to_dict,
)

_OCCURRED_AT = datetime(2026, 8, 23, 14, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "body, turn_id",
    [
        (
            SessionInputAccepted(
                text="检查当前状态",
                source=ConversationRef("feishu", "om_thread"),
            ),
            "turn-1",
        ),
        (SessionInputAccepted(text="后台恢复", source=None), "turn-2"),
        (AgentOutputStarted(), "turn-1"),
        (AgentOutputDelta(stream="message", text="正在检查"), "turn-1"),
        (AgentOutputDelta(stream="thought", text="需要读取日志"), "turn-1"),
        (
            AgentPlanUpdated(
                entries=(AgentPlanEntry(content="读取日志", status="in_progress"),)
            ),
            "turn-1",
        ),
        (
            AgentOutputFinished(
                message="检查完成",
                thought="日志状态正常",
                outcome="completed",
            ),
            "turn-1",
        ),
        (
            AgentOutputFinished(
                message="已经完成一部分",
                thought="收到取消请求",
                outcome="cancelled",
            ),
            "turn-2",
        ),
        (
            AgentOutputFinished(
                message="读取日志后中断",
                thought="连接意外关闭",
                outcome="failed",
            ),
            "turn-3",
        ),
        (
            ToolCallObserved(
                tool_call_id="call-1",
                kind="read_file",
                title="读取 daemon.py",
                status="started",
            ),
            "turn-1",
        ),
        (
            ToolCallObserved(
                tool_call_id="call-1",
                kind="read_file",
                title="读取 daemon.py",
                status="completed",
                detail="读取 245 行",
            ),
            "turn-1",
        ),
        (
            SessionStateChanged(previous_state="running", current_state="idle"),
            None,
        ),
        (
            SessionErrorOccurred(phase="agent_turn", message="连接意外关闭"),
            "turn-3",
        ),
        (
            SessionErrorOccurred(phase="startup", message="Agent 启动失败"),
            None,
        ),
    ],
)
def test_session_event_round_trip(body, turn_id):
    event = SessionEvent(
        event_id="event-1",
        session_id="t1",
        turn_id=turn_id,
        occurred_at=_OCCURRED_AT,
        body=body,
    )

    record = session_event_to_dict(event)

    assert record["schema_version"] == 1
    assert record["occurred_at"] == "2026-08-23T14:30:00Z"
    json_record = json.loads(json.dumps(record, ensure_ascii=False))

    assert session_event_from_dict(json_record) == event


@pytest.mark.parametrize(
    ("body", "turn_id", "event_type", "payload"),
    [
        (
            SessionInputAccepted(
                text="检查当前状态",
                source=ConversationRef("feishu", "om_thread"),
            ),
            "turn-1",
            "session.input.accepted",
            {
                "text": "检查当前状态",
                "source": {
                    "channel_key": "feishu",
                    "conversation_id": "om_thread",
                },
            },
        ),
        (
            AgentOutputStarted(),
            "turn-1",
            "agent.output.started",
            {},
        ),
        (
            AgentOutputDelta(stream="thought", text="需要读取日志"),
            "turn-1",
            "agent.output.delta",
            {"stream": "thought", "text": "需要读取日志"},
        ),
        (
            AgentPlanUpdated(
                entries=(AgentPlanEntry(content="读取日志", status="in_progress"),)
            ),
            "turn-1",
            "agent.plan.updated",
            {
                "entries": [
                    {"content": "读取日志", "status": "in_progress"},
                ]
            },
        ),
        (
            AgentOutputFinished(
                message="检查完成",
                thought="日志状态正常",
                outcome="completed",
            ),
            "turn-1",
            "agent.output.finished",
            {
                "message": "检查完成",
                "thought": "日志状态正常",
                "outcome": "completed",
            },
        ),
        (
            ToolCallObserved(
                tool_call_id="call-1",
                kind="read_file",
                title="读取 daemon.py",
                status="failed",
                detail="文件不存在",
            ),
            "turn-1",
            "tool.call.observed",
            {
                "tool_call_id": "call-1",
                "kind": "read_file",
                "title": "读取 daemon.py",
                "status": "failed",
                "detail": "文件不存在",
            },
        ),
        (
            SessionStateChanged(previous_state="running", current_state="idle"),
            None,
            "session.state.changed",
            {"previous_state": "running", "current_state": "idle"},
        ),
        (
            SessionErrorOccurred(phase="startup", message="Agent 启动失败"),
            None,
            "session.error.occurred",
            {"phase": "startup", "message": "Agent 启动失败"},
        ),
    ],
)
def test_session_event_wire_contract(body, turn_id, event_type, payload):
    event = SessionEvent(
        event_id="event-1",
        session_id="t1",
        turn_id=turn_id,
        occurred_at=_OCCURRED_AT,
        body=body,
    )

    record = session_event_to_dict(event)

    assert record["type"] == event_type
    assert record["payload"] == payload


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "schema_version"),
        ("schema_version", True, "schema_version"),
        ("type", "unknown.event", "未知"),
        ("occurred_at", "2026-08-23T14:30:00", "UTC"),
    ],
)
def test_session_event_rejects_invalid_envelope(field, value, message):
    record = session_event_to_dict(
        SessionEvent(
            event_id="event-1",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=_OCCURRED_AT,
            body=AgentOutputStarted(),
        )
    )
    record[field] = value

    with pytest.raises(ValueError, match=message):
        session_event_from_dict(record)


@pytest.mark.parametrize(
    ("event_type", "payload", "field"),
    [
        (
            "agent.output.delta",
            {"stream": "analysis", "text": "hidden"},
            "stream",
        ),
        (
            "agent.output.finished",
            {"message": "", "thought": "", "outcome": "stopped"},
            "outcome",
        ),
        (
            "tool.call.observed",
            {
                "tool_call_id": "call-1",
                "kind": "shell",
                "title": "运行测试",
                "status": "running",
                "detail": None,
            },
            "status",
        ),
        (
            "session.state.changed",
            {"previous_state": "running", "current_state": "unknown"},
            "current_state",
        ),
    ],
)
def test_session_event_rejects_invalid_enum(event_type, payload, field):
    record = {
        "schema_version": 1,
        "type": event_type,
        "event_id": "event-1",
        "session_id": "t1",
        "turn_id": "turn-1",
        "occurred_at": "2026-08-23T14:30:00Z",
        "payload": payload,
    }

    with pytest.raises(ValueError, match=field):
        session_event_from_dict(record)


def test_session_event_rejects_non_object_source():
    record = {
        "schema_version": 1,
        "type": "session.input.accepted",
        "event_id": "event-1",
        "session_id": "t1",
        "turn_id": "turn-1",
        "occurred_at": "2026-08-23T14:30:00Z",
        "payload": {"text": "hello", "source": "feishu:om_thread"},
    }

    with pytest.raises(ValueError, match="source"):
        session_event_from_dict(record)


@pytest.mark.parametrize(
    "body",
    [
        SessionInputAccepted(text="检查当前状态"),
        AgentOutputStarted(),
        AgentOutputDelta(stream="message", text="正在检查"),
        AgentOutputFinished(message="完成", thought="", outcome="completed"),
        ToolCallObserved(
            tool_call_id="call-1",
            kind="read_file",
            title="读取 daemon.py",
            status="started",
        ),
    ],
)
def test_turn_scoped_session_event_requires_turn_id(body):
    with pytest.raises(ValueError, match="必须携带 turn_id"):
        SessionEvent(
            event_id="event-1",
            session_id="t1",
            turn_id=None,
            occurred_at=_OCCURRED_AT,
            body=body,
        )


@pytest.mark.parametrize(
    "body",
    [
        object(),
        AgentOutputDelta(stream="analysis", text="隐藏内容"),
        AgentOutputFinished(message="完成", thought="", outcome="stopped"),
        ToolCallObserved(
            tool_call_id="call-1",
            kind="shell",
            title="运行测试",
            status="running",
        ),
        SessionStateChanged(previous_state="unknown", current_state="idle"),
    ],
)
def test_session_event_rejects_invalid_body_at_construction(body):
    with pytest.raises(ValueError):
        SessionEvent(
            event_id="event-1",
            session_id="t1",
            turn_id="turn-1",
            occurred_at=_OCCURRED_AT,
            body=body,
        )


def test_session_event_requires_utc_datetime():
    with pytest.raises(ValueError, match="UTC"):
        SessionEvent(
            event_id="event-1",
            session_id="t1",
            turn_id=None,
            occurred_at=datetime(2026, 8, 23, 14, 30),
            body=SessionStateChanged(
                previous_state="starting",
                current_state="running",
            ),
        )
