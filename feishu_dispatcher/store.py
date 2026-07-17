"""会话映射持久化：让 agent 会话跨 daemon 重启可恢复。

daemon 内存里的 ``_sessions`` 重启即丢。本模块把每个话题对应的
``thread_root_id → {project, agent, session_id, cwd}`` 落盘（JSON），
重启后据此用 ACP ``load_session`` 惰性重连（详见 docs/design.md 待办）。

``session_id`` 是 **agent 专属**的（copilot 的只能 copilot 加载），故记录里
必须带 agent 名。``path=None`` 时为纯内存模式（测试用，不写盘）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SessionRecord:
    thread_root_id: str
    project_name: str
    agent_label: str
    session_id: str
    cwd: str


class SessionStore:
    """thread_root_id → SessionRecord 的持久化映射。

    只被单个 daemon 实例（单线程 event loop）读写，无需加锁。写盘用
    临时文件 + 原子 replace，避免写一半崩溃损坏文件；读盘损坏时容错跳过。
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._records: dict[str, SessionRecord] = {}
        if path is not None and path.exists():
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for tid, r in data.items():
                self._records[tid] = SessionRecord(
                    thread_root_id=r["thread_root_id"],
                    project_name=r["project_name"],
                    agent_label=r["agent_label"],
                    session_id=r["session_id"],
                    cwd=r["cwd"],
                )
            logger.info("已加载 %d 条会话记录: %s", len(self._records), self._path)
        except Exception:
            logger.warning("会话存储读取失败，忽略: %s", self._path, exc_info=True)
            self._records = {}

    def _flush(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(
                    {t: asdict(r) for t, r in self._records.items()},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)  # 原子替换
        except Exception:
            logger.warning("会话存储写入失败: %s", self._path, exc_info=True)

    def get(self, thread_root_id: str) -> SessionRecord | None:
        return self._records.get(thread_root_id)

    def put(self, record: SessionRecord) -> None:
        self._records[record.thread_root_id] = record
        self._flush()

    def remove(self, thread_root_id: str) -> None:
        if self._records.pop(thread_root_id, None) is not None:
            self._flush()

    def all(self) -> dict[str, SessionRecord]:
        return dict(self._records)
