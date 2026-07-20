"""任务台账持久化：Task 是 daemon 拥有的核心实体（概念模型见 docs/design.md）。

一个 Task = 派发在某项目上的一个工作单元，持有它的 session_id（agent 侧记忆）、
thread_root_id（飞书话题）、workspace（工作目录）。落盘到 tasks.json，按 `task_id`
（短自增 `t<N>`，持久单调计数器、**永不复用**）索引；另存 thread→task 便于路由。

status 生命周期：
- 机械态（worker 自动）：starting → running ↔ idle → suspended
- 语义终止态（人/调度器）：done（归档）/ stopped（中途结束）/ failed（出错）
终止任务默认不自动恢复；`suspended`/`idle` 才可 load_session 无缝续。历史留最近 N 个。

``path=None`` 为纯内存（测试）。原子写 + 读损坏容错。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: 仍在活跃视图里的状态
ACTIVE_STATES = frozenset({"starting", "running", "idle", "suspended"})
#: 话题回复即可 load_session 恢复的状态
RESUMABLE_STATES = frozenset({"idle", "suspended"})
#: 终止状态（移出活跃，进历史）
TERMINAL_STATES = frozenset({"done", "stopped", "failed"})

#: 每个 Task 最多保留的动作条数（审计日志，超出丢最旧，防 tasks.json 无限涨）
_MAX_ACTIONS = 200

_TASK_FIELDS = (
    "task_id",
    "project_name",
    "agent_label",
    "description",
    "status",
    "session_id",
    "thread_root_id",
    "workspace",
    "turns",
    "created_at",
    "updated_at",
    "actions",
    "last_output",
    "model",
)


@dataclass
class Task:
    task_id: str
    project_name: str
    agent_label: str
    description: str
    status: str  # starting/running/idle/suspended/done/stopped/failed
    session_id: str = ""
    thread_root_id: str = ""
    workspace: str = ""
    turns: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    #: 审计动作日志：每条 = {"turn", "kind", "title"}，来自 ACP tool_call 事件
    actions: list[dict] = field(default_factory=list)
    #: 最近一轮 agent 的收尾回复（截断），供 get_task / 完成通知摘要
    last_output: str = ""
    #: agent 当前模型（opencode 上报；copilot 不暴露则为空）
    model: str = ""

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATES

    @property
    def is_resumable(self) -> bool:
        return self.status in RESUMABLE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES


class TaskStore:
    """task_id → Task 台账 + thread_root_id → task_id 路由索引 + 单调计数器。

    只被单个 daemon 实例（单线程 event loop）读写，无需加锁。
    ``keep_terminal`` 限制终止任务的历史条数，防 tasks.json 无限涨。
    """

    def __init__(self, path: Path | None, *, keep_terminal: int = 50) -> None:
        self._path = path
        self._keep = keep_terminal
        self._tasks: dict[str, Task] = {}
        self._seq = 0  # 单调计数器，永不复用
        if path is not None and path.exists():
            self._load()

    # ---- 持久化 ---- #

    def _load(self) -> None:
        assert self._path is not None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._seq = int(data.get("seq", 0))
            for tid, d in (data.get("tasks") or {}).items():
                self._tasks[tid] = Task(**{k: d[k] for k in _TASK_FIELDS if k in d})
            logger.info("已加载 %d 个任务: %s", len(self._tasks), self._path)
        except Exception:
            logger.warning("任务台账读取失败，忽略: %s", self._path, exc_info=True)
            self._tasks = {}
            self._seq = 0

    def _flush(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            payload = {
                "seq": self._seq,
                "tasks": {tid: asdict(t) for tid, t in self._tasks.items()},
            }
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception:
            logger.warning("任务台账写入失败: %s", self._path, exc_info=True)

    @staticmethod
    def _now() -> float:
        return time.time()

    # ---- 读 ---- #

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def by_thread(self, thread_root_id: str) -> Task | None:
        if not thread_root_id:
            return None
        for t in self._tasks.values():
            if t.thread_root_id == thread_root_id:
                return t
        return None

    def all(self) -> list[Task]:
        return list(self._tasks.values())

    def active(self) -> list[Task]:
        return [t for t in self._tasks.values() if t.is_active]

    # ---- 写 ---- #

    def create(
        self,
        *,
        project_name: str,
        agent_label: str,
        description: str,
        thread_root_id: str,
        workspace: str,
        session_id: str = "",
        status: str = "starting",
    ) -> Task:
        self._seq += 1
        now = self._now()
        task = Task(
            task_id=f"t{self._seq}",
            project_name=project_name,
            agent_label=agent_label,
            description=description,
            status=status,
            session_id=session_id,
            thread_root_id=thread_root_id,
            workspace=workspace,
            created_at=now,
            updated_at=now,
        )
        self._tasks[task.task_id] = task
        self._flush()
        return task

    def update(self, task_id: str, **changes) -> Task | None:
        """就地更新任务字段（status/session_id/turns…），刷新 updated_at 并落盘。

        改成终止态时顺带修剪历史。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return None
        for k, v in changes.items():
            setattr(task, k, v)
        task.updated_at = self._now()
        if task.is_terminal:
            self._prune()
        self._flush()
        return task

    def add_action(self, task_id: str, action: dict) -> None:
        """追加一条动作到任务的审计日志（超 ``_MAX_ACTIONS`` 丢最旧），落盘。

        写透式：每条 tool_call 都刷一次盘，与 store 其余部分一致；chatty agent
        的写量对个人工具可接受（max_agents 默认 3），需要再批量化。
        """
        task = self._tasks.get(task_id)
        if task is None:
            return
        task.actions.append(action)
        if len(task.actions) > _MAX_ACTIONS:
            del task.actions[:-_MAX_ACTIONS]
        task.updated_at = self._now()
        self._flush()

    def _prune(self) -> None:
        """只保留最近 ``keep_terminal`` 个终止任务。"""
        terminal = sorted(
            (t for t in self._tasks.values() if t.is_terminal),
            key=lambda t: t.updated_at,
        )
        for t in terminal[: -self._keep] if self._keep else terminal:
            del self._tasks[t.task_id]

    def clear_terminal(self) -> int:
        """清空所有终止任务（/clear），返回清掉的条数。"""
        gone = [tid for tid, t in self._tasks.items() if t.is_terminal]
        for tid in gone:
            del self._tasks[tid]
        if gone:
            self._flush()
        return len(gone)
