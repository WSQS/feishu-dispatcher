"""Session Trace SQLite 存储测试。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from feishu_dispatcher.session_event import (
    AgentOutputDelta,
    SessionEvent,
    SessionInputAccepted,
)
from feishu_dispatcher.trace_store import SessionTraceStore

_OCCURRED_AT = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)


def _event(
    event_id: str,
    session_id: str = "session-1",
    *,
    text: str | None = None,
) -> SessionEvent:
    return SessionEvent(
        event_id=event_id,
        session_id=session_id,
        turn_id="turn-1",
        occurred_at=_OCCURRED_AT,
        body=(
            SessionInputAccepted(text=text)
            if text is not None
            else AgentOutputDelta(stream="message", text=event_id)
        ),
    )


def test_trace_store_assigns_session_local_sequence(tmp_path):
    with SessionTraceStore(tmp_path / "trace.sqlite") as store:
        first = store.append(_event("event-1"))
        second = store.append(_event("event-2"))
        other = store.append(_event("event-other", "session-2"))

        assert first.sequence == 1
        assert second.sequence == 2
        assert other.sequence == 1
        assert store.read_after("session-1") == (first, second)


def test_trace_store_read_after_applies_cursor_and_limit(tmp_path):
    with SessionTraceStore(tmp_path / "trace.sqlite") as store:
        records = [store.append(_event(f"event-{index}")) for index in range(1, 5)]

        assert store.read_after("session-1", after=1, limit=2) == tuple(records[1:3])
        assert store.read_after("session-1", after=4) == ()


def test_trace_store_duplicate_event_is_idempotent(tmp_path):
    with SessionTraceStore(tmp_path / "trace.sqlite") as store:
        event = _event("event-1")

        first = store.append(event)
        duplicate = store.append(event)

        assert duplicate == first
        assert store.read_after("session-1") == (first,)


def test_trace_store_rejects_conflicting_duplicate_event(tmp_path):
    with SessionTraceStore(tmp_path / "trace.sqlite") as store:
        store.append(_event("event-1", text="first"))

        with pytest.raises(ValueError, match="不一致"):
            store.append(_event("event-1", text="second"))


def test_trace_store_rejects_blank_session_id_on_append(tmp_path):
    with SessionTraceStore(tmp_path / "trace.sqlite") as store:
        with pytest.raises(ValueError, match="session_id"):
            store.append(_event("event-1", "   "))


def test_trace_store_recovers_after_reopen(tmp_path):
    path = tmp_path / "trace.sqlite"
    event = _event("event-1")

    with SessionTraceStore(path) as store:
        expected = store.append(event)

    with SessionTraceStore(path) as store:
        assert store.read_after("session-1") == (expected,)


@pytest.mark.parametrize(
    ("session_id", "after", "limit", "message"),
    [
        ("", 0, 100, "session_id"),
        ("session-1", -1, 100, "after"),
        ("session-1", 0, 0, "limit"),
    ],
)
def test_trace_store_rejects_invalid_read_arguments(
    tmp_path, session_id, after, limit, message
):
    with SessionTraceStore(tmp_path / "trace.sqlite") as store:
        with pytest.raises(ValueError, match=message):
            store.read_after(session_id, after=after, limit=limit)
