"""SessionRuntimeRegistry 的行为测试。"""

from __future__ import annotations

import pytest

from feishu_dispatcher.agent.scheduler import SchedulerMemory
from feishu_dispatcher.agent.session import (
    DispatcherSessionRuntime,
    SessionRuntimeRegistry,
)


def runtime(session_id: str) -> DispatcherSessionRuntime:
    return DispatcherSessionRuntime(
        session_id=session_id,
        llm_provider=lambda: None,
        memory=SchedulerMemory(None),
        tools_provider=lambda _conversation: [],
    )


def test_registry_manages_runtime_by_session() -> None:
    session_runtime = runtime("session-a")
    registry = SessionRuntimeRegistry()

    assert registry.register(session_runtime)
    assert not registry.register(session_runtime)

    assert registry.get_for_session("session-a") is session_runtime
    assert registry.get_for_session("missing") is None
    assert registry.values() == [session_runtime]


def test_registry_rejects_conflicting_runtime() -> None:
    registry = SessionRuntimeRegistry()
    registry.register(runtime("session-a"))

    with pytest.raises(RuntimeError, match="Session Runtime 已注册: session-a"):
        registry.register(runtime("session-a"))


def test_registry_removes_only_the_current_runtime_generation() -> None:
    registry = SessionRuntimeRegistry()
    first = runtime("session-a")
    replacement = runtime("session-a")
    registry.register(first)

    assert registry.is_current(first)
    assert not registry.is_current(replacement)
    assert not registry.remove_if_current(replacement)
    assert registry.remove_if_current(first)
    assert not registry.remove_if_current(first)
    assert registry.count() == 0
