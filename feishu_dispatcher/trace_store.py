"""Session Trace 的 SQLite 持久化。"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .session_event import SessionEvent, session_event_from_dict, session_event_to_dict


@dataclass(frozen=True)
class SessionTraceRecord:
    """Session Trace 中带稳定顺序号的事件记录。"""

    sequence: int
    event: SessionEvent


class SessionTraceStore:
    """按 Session 持久化、顺序读取并幂等追加 SessionEvent。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> SessionTraceStore:
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def append(self, event: SessionEvent) -> SessionTraceRecord:
        _validate_session_id(event.session_id)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    """
                    SELECT sequence, event_json
                    FROM session_trace_events
                    WHERE session_id = ? AND event_id = ?
                    """,
                    (event.session_id, event.event_id),
                ).fetchone()
                if existing is not None:
                    record = self._record_from_row(existing)
                    if record.event != event:
                        raise ValueError("event_id 已存在但对应的 SessionEvent 不一致")
                    self._connection.execute("COMMIT")
                    return record

                sequence = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM session_trace_events
                    WHERE session_id = ?
                    """,
                    (event.session_id,),
                ).fetchone()[0]
                event_json = json.dumps(
                    session_event_to_dict(event),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self._connection.execute(
                    """
                    INSERT INTO session_trace_events
                        (session_id, sequence, event_id, event_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (event.session_id, sequence, event.event_id, event_json),
                )
                self._connection.execute("COMMIT")
                return SessionTraceRecord(sequence=sequence, event=event)
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def read_after(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 100,
    ) -> tuple[SessionTraceRecord, ...]:
        _validate_session_id(session_id)
        if after < 0:
            raise ValueError("after 不能小于 0")
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT sequence, event_json
                FROM session_trace_events
                WHERE session_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (session_id, after, limit),
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

    def _create_schema(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_trace_events (
                    session_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (session_id, sequence),
                    UNIQUE (session_id, event_id)
                );
                """
            )

    @staticmethod
    def _record_from_row(row: tuple[int, str]) -> SessionTraceRecord:
        sequence, event_json = row
        event = session_event_from_dict(json.loads(event_json))
        return SessionTraceRecord(sequence=sequence, event=event)


def _validate_session_id(session_id: str) -> None:
    if not session_id.strip():
        raise ValueError("session_id 不能为空")
