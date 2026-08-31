"""Session 运行事实的领域事件。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, TypeAlias, cast

from .conversation import ConversationRef

SESSION_EVENT_SCHEMA_VERSION = 1

ConversationRefSerializer: TypeAlias = Callable[
    [ConversationRef],
    dict[str, object],
]
ConversationRefDeserializer: TypeAlias = Callable[
    [str, dict[str, object]],
    ConversationRef,
]

OutputStream = Literal["message", "thought"]
OutputOutcome = Literal["completed", "cancelled", "failed"]
PlanEntryStatus = Literal["pending", "in_progress", "completed"]
ToolCallStatus = Literal["started", "completed", "failed"]
SessionState = Literal[
    "starting",
    "running",
    "idle",
    "suspended",
    "done",
    "stopped",
    "failed",
]

_OUTPUT_STREAMS = frozenset({"message", "thought"})
_OUTPUT_OUTCOMES = frozenset({"completed", "cancelled", "failed"})
_PLAN_ENTRY_STATUSES = frozenset({"pending", "in_progress", "completed"})
_TOOL_CALL_STATUSES = frozenset({"started", "completed", "failed"})
_SESSION_STATES = frozenset(
    {"starting", "running", "idle", "suspended", "done", "stopped", "failed"}
)


@dataclass(frozen=True)
class SessionInputAccepted:
    """Session 已接受的一轮输入。"""

    text: str
    source: ConversationRef | None = None


@dataclass(frozen=True)
class AgentOutputStarted:
    """Agent 已开始为当前 Turn 产生输出。"""


@dataclass(frozen=True)
class AgentOutputDelta:
    """Agent 某类文本流的增量片段。"""

    stream: OutputStream
    text: str


@dataclass(frozen=True)
class AgentPlanEntry:
    """Agent 计划中的一个步骤快照。"""

    content: str
    status: PlanEntryStatus


@dataclass(frozen=True)
class AgentPlanUpdated:
    """Agent 更新了当前 Turn 的计划。"""

    entries: tuple[AgentPlanEntry, ...]


@dataclass(frozen=True)
class AgentOutputFinished:
    """Agent 当前 Turn 的权威完整输出。"""

    message: str
    thought: str
    outcome: OutputOutcome


@dataclass(frozen=True)
class ToolCallObserved:
    """Session 观察到的一次工具调用生命周期变化。"""

    tool_call_id: str
    kind: str
    title: str
    status: ToolCallStatus
    detail: str | None = None


@dataclass(frozen=True)
class SessionStateChanged:
    """Session 生命周期状态发生变化。"""

    previous_state: SessionState
    current_state: SessionState


@dataclass(frozen=True)
class SessionErrorOccurred:
    """Session 运行期间发生的错误摘要。"""

    phase: str
    message: str


SessionEventBody: TypeAlias = (
    SessionInputAccepted
    | AgentOutputStarted
    | AgentOutputDelta
    | AgentPlanUpdated
    | AgentOutputFinished
    | ToolCallObserved
    | SessionStateChanged
    | SessionErrorOccurred
)

_TURN_SCOPED_EVENT_BODIES = (
    SessionInputAccepted,
    AgentOutputStarted,
    AgentOutputDelta,
    AgentPlanUpdated,
    AgentOutputFinished,
    ToolCallObserved,
)

_EVENT_TYPES = {
    SessionInputAccepted: "session.input.accepted",
    AgentOutputStarted: "agent.output.started",
    AgentOutputDelta: "agent.output.delta",
    AgentPlanUpdated: "agent.plan.updated",
    AgentOutputFinished: "agent.output.finished",
    ToolCallObserved: "tool.call.observed",
    SessionStateChanged: "session.state.changed",
    SessionErrorOccurred: "session.error.occurred",
}


@dataclass(frozen=True)
class SessionEvent:
    """一个 Session 内已经发生的、与呈现方式无关的运行事实。"""

    event_id: str
    session_id: str
    turn_id: str | None
    occurred_at: datetime
    body: SessionEventBody

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("event_id 不能为空")
        if not self.session_id:
            raise ValueError("session_id 不能为空")
        if type(self.body) not in _EVENT_TYPES:
            raise ValueError(f"不支持的 SessionEvent body: {type(self.body).__name__}")
        if self.turn_id == "":
            raise ValueError("turn_id 不能为空字符串")
        if self.turn_id is None and isinstance(self.body, _TURN_SCOPED_EVENT_BODIES):
            raise ValueError(f"{type(self.body).__name__} 必须携带 turn_id")
        _validate_body(self.body)
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() != timedelta(
            0
        ):
            raise ValueError("occurred_at 必须使用 UTC 时区")


def session_event_to_dict(
    event: SessionEvent,
    *,
    conversation_ref_serializer: ConversationRefSerializer | None = None,
) -> dict[str, object]:
    """把 SessionEvent 编码为 V1 JSON 兼容字典。"""

    body = event.body
    event_type = _EVENT_TYPES.get(type(body))
    if event_type is None:
        raise ValueError(f"不支持的 SessionEvent body: {type(body).__name__}")

    if isinstance(body, SessionInputAccepted):
        payload: dict[str, object] = {"text": body.text, "source": None}
        if body.source is not None:
            if conversation_ref_serializer is None:
                raise ValueError(
                    "SessionInputAccepted.source 需要 ConversationRef serializer"
                )
            source_payload = conversation_ref_serializer(body.source)
            if "channel_key" in source_payload:
                raise ValueError("ConversationRef codec payload 不能包含 channel_key")
            payload["source"] = {
                "channel_key": body.source.channel_key(),
                **source_payload,
            }
    elif isinstance(body, AgentOutputStarted):
        payload = {}
    elif isinstance(body, AgentOutputDelta):
        _require_enum("stream", body.stream, _OUTPUT_STREAMS)
        payload = {"stream": body.stream, "text": body.text}
    elif isinstance(body, AgentPlanUpdated):
        payload = {
            "entries": [
                {"content": entry.content, "status": entry.status}
                for entry in body.entries
            ]
        }
    elif isinstance(body, AgentOutputFinished):
        _require_enum("outcome", body.outcome, _OUTPUT_OUTCOMES)
        payload = {
            "message": body.message,
            "thought": body.thought,
            "outcome": body.outcome,
        }
    elif isinstance(body, ToolCallObserved):
        _require_enum("status", body.status, _TOOL_CALL_STATUSES)
        payload = {
            "tool_call_id": body.tool_call_id,
            "kind": body.kind,
            "title": body.title,
            "status": body.status,
            "detail": body.detail,
        }
    elif isinstance(body, SessionStateChanged):
        _require_enum("previous_state", body.previous_state, _SESSION_STATES)
        _require_enum("current_state", body.current_state, _SESSION_STATES)
        payload = {
            "previous_state": body.previous_state,
            "current_state": body.current_state,
        }
    else:
        payload = {"phase": body.phase, "message": body.message}

    return {
        "schema_version": SESSION_EVENT_SCHEMA_VERSION,
        "type": event_type,
        "event_id": event.event_id,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }


def session_event_from_dict(
    record: dict[str, object],
    *,
    conversation_ref_deserializer: ConversationRefDeserializer | None = None,
) -> SessionEvent:
    """从 V1 JSON 兼容字典解码 SessionEvent。"""

    schema_version = record.get("schema_version")
    if (
        type(schema_version) is not int
        or schema_version != SESSION_EVENT_SCHEMA_VERSION
    ):
        raise ValueError(f"不支持的 SessionEvent schema_version: {schema_version!r}")

    event_type = _require_string(record, "type")
    payload = _require_dict(record, "payload")
    body = _decode_body(
        event_type,
        payload,
        conversation_ref_deserializer=conversation_ref_deserializer,
    )

    turn_id_value = record.get("turn_id")
    if turn_id_value is not None and not isinstance(turn_id_value, str):
        raise ValueError("turn_id 必须是字符串或 null")

    occurred_at_text = _require_string(record, "occurred_at")
    try:
        occurred_at = datetime.fromisoformat(occurred_at_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("occurred_at 必须是 ISO 8601 时间") from exc

    return SessionEvent(
        event_id=_require_string(record, "event_id"),
        session_id=_require_string(record, "session_id"),
        turn_id=turn_id_value,
        occurred_at=occurred_at,
        body=body,
    )


def _decode_body(
    event_type: str,
    payload: dict[str, object],
    *,
    conversation_ref_deserializer: ConversationRefDeserializer | None = None,
) -> SessionEventBody:
    if event_type == "session.input.accepted":
        source_value = payload.get("source")
        source = None
        if source_value is not None:
            if not isinstance(source_value, dict):
                raise ValueError("source 必须是对象或 null")
            channel_key = _require_string(source_value, "channel_key")
            source_payload = {
                key: value
                for key, value in source_value.items()
                if key != "channel_key"
            }
            if conversation_ref_deserializer is None:
                raise ValueError(
                    "SessionInputAccepted.source 需要 ConversationRef deserializer"
                )
            source = conversation_ref_deserializer(channel_key, source_payload)
        return SessionInputAccepted(
            text=_require_string(payload, "text"),
            source=source,
        )
    if event_type == "agent.output.started":
        return AgentOutputStarted()
    if event_type == "agent.output.delta":
        stream = _require_enum(
            "stream", _require_string(payload, "stream"), _OUTPUT_STREAMS
        )
        return AgentOutputDelta(
            stream=cast(OutputStream, stream),
            text=_require_string(payload, "text"),
        )
    if event_type == "agent.plan.updated":
        entries_value = payload.get("entries")
        if not isinstance(entries_value, list):
            raise ValueError("entries 必须是数组")
        entries = []
        for entry_value in entries_value:
            if not isinstance(entry_value, dict):
                raise ValueError("entries 元素必须是对象")
            status = _require_enum(
                "status",
                _require_string(entry_value, "status"),
                _PLAN_ENTRY_STATUSES,
            )
            entries.append(
                AgentPlanEntry(
                    content=_require_string(entry_value, "content"),
                    status=cast(PlanEntryStatus, status),
                )
            )
        return AgentPlanUpdated(entries=tuple(entries))
    if event_type == "agent.output.finished":
        outcome = _require_enum(
            "outcome", _require_string(payload, "outcome"), _OUTPUT_OUTCOMES
        )
        return AgentOutputFinished(
            message=_require_string(payload, "message"),
            thought=_require_string(payload, "thought"),
            outcome=cast(OutputOutcome, outcome),
        )
    if event_type == "tool.call.observed":
        status = _require_enum(
            "status", _require_string(payload, "status"), _TOOL_CALL_STATUSES
        )
        detail_value = payload.get("detail")
        if detail_value is not None and not isinstance(detail_value, str):
            raise ValueError("detail 必须是字符串或 null")
        return ToolCallObserved(
            tool_call_id=_require_string(payload, "tool_call_id"),
            kind=_require_string(payload, "kind"),
            title=_require_string(payload, "title"),
            status=cast(ToolCallStatus, status),
            detail=detail_value,
        )
    if event_type == "session.state.changed":
        previous_state = _require_enum(
            "previous_state",
            _require_string(payload, "previous_state"),
            _SESSION_STATES,
        )
        current_state = _require_enum(
            "current_state",
            _require_string(payload, "current_state"),
            _SESSION_STATES,
        )
        return SessionStateChanged(
            previous_state=cast(SessionState, previous_state),
            current_state=cast(SessionState, current_state),
        )
    if event_type == "session.error.occurred":
        return SessionErrorOccurred(
            phase=_require_string(payload, "phase"),
            message=_require_string(payload, "message"),
        )
    raise ValueError(f"未知的 SessionEvent type: {event_type!r}")


def _validate_body(body: SessionEventBody) -> None:
    if isinstance(body, AgentOutputDelta):
        _require_enum("stream", body.stream, _OUTPUT_STREAMS)
    elif isinstance(body, AgentPlanUpdated):
        for entry in body.entries:
            _require_enum("status", entry.status, _PLAN_ENTRY_STATUSES)
    elif isinstance(body, AgentOutputFinished):
        _require_enum("outcome", body.outcome, _OUTPUT_OUTCOMES)
    elif isinstance(body, ToolCallObserved):
        _require_enum("status", body.status, _TOOL_CALL_STATUSES)
    elif isinstance(body, SessionStateChanged):
        _require_enum("previous_state", body.previous_state, _SESSION_STATES)
        _require_enum("current_state", body.current_state, _SESSION_STATES)


def _require_dict(record: dict[str, object], field: str) -> dict[str, object]:
    value = record.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return value


def _require_string(record: dict[str, object], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    return value


def _require_enum(field: str, value: str, allowed: frozenset[str]) -> str:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{field} 必须是以下值之一: {choices}")
    return value
