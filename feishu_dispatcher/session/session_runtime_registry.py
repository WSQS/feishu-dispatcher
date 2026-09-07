"""Session Runtime 注册表。"""

from __future__ import annotations

from .session_runtime import SessionRuntime


class SessionRuntimeRegistry:
    """按 Session 身份管理已登记的 Session Runtime。"""

    def __init__(self) -> None:
        self._by_session: dict[str, SessionRuntime] = {}

    def register(self, runtime: SessionRuntime) -> bool:
        """登记 Runtime；新登记返回 True，同一实例重复登记返回 False。"""
        session_id = runtime.session_id
        current = self._by_session.get(session_id)
        if current is runtime:
            return False
        if current is not None:
            raise RuntimeError(f"Session Runtime 已注册: {session_id}")
        self._by_session[session_id] = runtime
        return True

    def get_for_session(self, session_id: str) -> SessionRuntime | None:
        return self._by_session.get(session_id)

    def is_current(self, runtime: SessionRuntime) -> bool:
        """Runtime 是否仍是其 Session 当前登记的实例。"""
        return self._by_session.get(runtime.session_id) is runtime

    def remove_if_current(self, runtime: SessionRuntime) -> bool:
        """仅当实例仍是 current generation 时移除。"""
        if not self.is_current(runtime):
            return False
        del self._by_session[runtime.session_id]
        return True

    def values(self) -> list[SessionRuntime]:
        return list(self._by_session.values())

    def count(self) -> int:
        return len(self._by_session)
