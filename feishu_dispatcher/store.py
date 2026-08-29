"""Session 台账持久化：Session 是 daemon 拥有的内存模型。

一个 Session = 派发在某项目上的一个工作单元，持有它的 agent_session_id（agent 侧记忆）、
ConversationRef + thread_root_id（交互入口）、workspace（工作目录）。落盘到 tasks.json，
按 `session_id`（短自增 `t<N>`，持久单调计数器、**永不复用**）索引；按
ConversationRef + thread_root_id 路由交互线程。

status 生命周期：
- 机械态（worker 自动）：starting → running ↔ idle → suspended；turn 异常 → failed
- 语义终止态（人/调度器）：done（归档）/ stopped（中途结束）
`suspended`/`idle`/`failed` 都可 load_session 惰性恢复——failed = turn 中途异常「卡住等
恢复」而非「死了」：turn 失败时 session 已建，多半能接回；恢复失败才真停在 failed
（startup 失败无 session，天然挡回 `/run`）。failed 不自动清理（同 suspended，可恢复态
不进历史修剪）。历史留最近 N 个终止 Session。

``path=None`` 为纯内存（测试）。原子写 + 读损坏容错。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ._atomic import atomic_write
from .config import Project
from .conversation import ConversationRef

logger = logging.getLogger(__name__)


# ---- 台账落盘的持久化原语（四个 store 共用，#83）---- #
#
# 事故背景：系统硬崩（掉电 / BSOD / 内核 panic）后 tasks.json 损坏。根因是原来的
# 「写临时文件 + replace」只挡**应用层**崩溃——数据先进 OS page cache，replace 改的是
# 目录项；机器硬崩时可能改名已生效、数据块还没落盘 → 重启得到 0 字节/半截文件。
# 三件事把这个窗口关掉：写临时文件后 fsync 让数据真正落盘、保留 .bak 作回退源、读
# 损坏时存档而非静默清空（守住 session_id/seq 单调，避免撞回旧 id）。
#
# 持久化原语（temp+fsync+replace+fsync_dir）已抽到 ``_atomic.py`` 共享。


def _atomic_write_json(path: Path, payload: dict) -> None:
    """原子且**持久**地把 payload 写到 path（台账：留 .bak 回退）。

    ``json.dumps`` 后调用 ``_atomic.atomic_write(keep_bak=True)``：写临时文件 →
    flush+fsync（数据落盘）→ 把旧主文件改名成 .bak（回退源，与新写互不覆盖）→
    replace 临时文件（原子改名）→ fsync 父目录（改名持久）。

    .bak 用改名而非拷贝：便宜、原子，且天然 = 上一份好数据。改名到 replace 之间有个极
    短窗口主文件暂缺，但 _read_json 会在主文件缺失/损坏时回退 .bak，故可安全降级。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    atomic_write(path, text, keep_bak=True)


def _archive_corrupt(path: Path) -> None:
    """把损坏的台账主文件改名存档（.corrupt-<秒级时间戳>），不再挡下次启动、留作诊断。"""
    try:
        dest = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        path.replace(dest)
        logger.warning("损坏台账已存档: %s", dest)
    except OSError:
        logger.warning("存档损坏台账失败（忽略）: %s", path, exc_info=True)


def _read_json(path: Path) -> dict | None:
    """读台账 JSON，带损坏容错与备份回退。

    主文件 OK → 返回其 payload；主文件损坏 → 存档 .corrupt-<ts> 再回退 .bak；
    主文件缺失 → 直接试 .bak；主/备都不可用 → None（调用方空起）。

    关键：损坏时**不再静默清空**——先存档主文件、再尽力从 .bak 恢复，守住 session_id/seq
    的单调（丢台账 + seq 归零会让新 task 撞回飞书话题仍引用的旧 id）。
    """
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "台账主文件损坏，存档并尝试回退备份: %s", path, exc_info=True
            )
            _archive_corrupt(path)
    bak = path.with_name(path.name + ".bak")
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            logger.warning("已从备份恢复台账: %s", bak)
            return data
        except Exception:
            logger.warning("台账备份也损坏，忽略: %s", bak, exc_info=True)
    return None


#: 仍在活跃视图里的状态（failed = turn 异常卡住、可恢复，不算终止）
ACTIVE_STATES = frozenset({"starting", "running", "idle", "suspended", "failed"})
#: 话题回复即可 load_session 恢复的状态（failed 有 session 时可接回）
RESUMABLE_STATES = frozenset({"idle", "suspended", "failed"})
#: 终止状态（移出活跃，进历史；只剩人/调度器主动结束的）
TERMINAL_STATES = frozenset({"done", "stopped"})

#: 每个 Session 最多保留的动作条数（审计日志，超出丢最旧，防 tasks.json 无限涨）
_MAX_ACTIONS = 200

_SESSION_RECORD_FIELDS = (
    "session_id",
    "project_name",
    "agent_label",
    "description",
    "status",
    "agent_session_id",
    "channel_key",
    "conversation_id",
    "thread_root_id",
    "workspace",
    "turns",
    "created_at",
    "updated_at",
    "actions",
    "last_output",
    "model",
    "error_message",
    "issue_url",
    "origin",
)


@dataclass
class Session:
    session_id: str
    project_name: str
    agent_label: str
    description: str
    status: str  # starting/running/idle/suspended/done/stopped/failed
    agent_session_id: str = ""
    channel_key: str = ""
    conversation_id: str = ""
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
    #: turn 异常时的诊断（异常类型 + 片段），供 /task /agents / 恢复判断；正常时空
    error_message: str = ""
    #: 关联的 forge issue URL（派活时锚定的意图/brief，#63）；空 = 未绑定。
    #: 单字段、控制平面拥有；PR 不存这里（经 forge 的 Closes #N 反查）。
    issue_url: str = ""
    #: 会话来源：spawn = daemon 新建会话（/run、spawn_agent），attach = 附着外部会话
    #: （/attach，#99）。旧 tasks.json 无此键时缺省为 spawn（向后兼容）。
    origin: str = "spawn"

    @property
    def conversation_ref(self) -> ConversationRef:
        return ConversationRef(self.channel_key, self.conversation_id)

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATES

    @property
    def is_resumable(self) -> bool:
        return self.status in RESUMABLE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES


def _session_from_record(record: dict) -> Session:
    if "task_id" in record:
        raise ValueError("不支持旧版 Session 记录字段 task_id")
    values = {key: record[key] for key in _SESSION_RECORD_FIELDS if key in record}
    return Session(**values)


def _session_to_record(session: Session) -> dict:
    return asdict(session)


class SessionStore:
    """session_id → Session 台账 + Channel-scoped thread 路由 + 单调计数器。

    只被单个 daemon 实例（单线程 event loop）读写，无需加锁。
    ``keep_terminal`` 限制终止 Session 的历史条数，防 tasks.json 无限涨。
    """

    def __init__(self, path: Path | None, *, keep_terminal: int = 50) -> None:
        self._path = path
        self._keep = keep_terminal
        self._sessions: dict[str, Session] = {}
        self._seq = 0  # 单调计数器，永不复用
        if path is not None:
            self._load()

    # ---- 持久化 ---- #

    def _load(self) -> None:
        assert self._path is not None
        data = _read_json(self._path)
        if data is None:
            return
        try:
            self._seq = int(data.get("seq", 0))
            for session_id, record in (data.get("tasks") or {}).items():
                session = _session_from_record(record)
                if session.session_id != session_id:
                    raise ValueError(
                        "Session 记录主键不一致: "
                        f"{session_id!r} != {session.session_id!r}"
                    )
                self._sessions[session_id] = session
            logger.info("已加载 %d 个任务: %s", len(self._sessions), self._path)
        except Exception:
            logger.warning("任务台账解析失败，忽略: %s", self._path, exc_info=True)
            self._sessions = {}
            self._seq = 0

    def _flush(self) -> None:
        if self._path is None:
            return
        payload = {
            "seq": self._seq,
            "tasks": {
                session_id: _session_to_record(session)
                for session_id, session in self._sessions.items()
            },
        }
        try:
            _atomic_write_json(self._path, payload)
        except Exception:
            logger.warning("任务台账写入失败: %s", self._path, exc_info=True)

    @staticmethod
    def _now() -> float:
        return time.time()

    # ---- 读 ---- #

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def by_conversation(self, conversation: ConversationRef) -> Session | None:
        if (
            not conversation.channel_key().strip()
            or not conversation.conversation_id.strip()
        ):
            return None
        for session in self._sessions.values():
            if session.conversation_ref == conversation:
                return session
        return None

    def by_thread(
        self, conversation: ConversationRef, thread_root_id: str
    ) -> Session | None:
        if (
            not conversation.channel_key().strip()
            or not conversation.conversation_id.strip()
            or not thread_root_id
        ):
            return None
        for session in self._sessions.values():
            if (
                session.conversation_ref == conversation
                and session.thread_root_id == thread_root_id
            ):
                return session
        return None

    def all(self) -> list[Session]:
        return list(self._sessions.values())

    def active(self) -> list[Session]:
        return [session for session in self._sessions.values() if session.is_active]

    def by_agent_session(self, agent_label: str, session_id: str) -> Session | None:
        """按 ``(agent, session_id)`` 组合查已附着的 Session，供 /attach 去重。

        二者**同时**匹配才算重复：不同 agent 的 session 命名空间互相独立，同名
        session_id 跨 agent 不冲突。空值（无 agent 或无 session）视为不匹配。
        """
        if not agent_label or not session_id:
            return None
        for session in self._sessions.values():
            if (
                session.agent_label == agent_label
                and session.agent_session_id == session_id
            ):
                return session
        return None

    # ---- 写 ---- #

    def create(
        self,
        *,
        project_name: str,
        agent_label: str,
        description: str,
        conversation: ConversationRef,
        thread_root_id: str,
        workspace: str,
        agent_session_id: str = "",
        status: str = "starting",
        issue_url: str = "",
        model: str = "",
        origin: str = "spawn",
    ) -> Session:
        if not conversation.channel_key().strip():
            raise ValueError("ConversationRef.channel_key 不能为空")
        if not conversation.conversation_id.strip():
            raise ValueError("ConversationRef.conversation_id 不能为空")
        # 铸号自愈守卫（#81）：不只信持久化的 seq——同时从现有 session id 推出下界，
        # 取两者较大再 +1。即使 seq 因故被回退/污染（多实例踩踏、手工改 tasks.json、
        # 半截原子写），也绝不落到已存在的 id 上，守住「session_id 永不复用」不变量。
        floor = max(
            (
                int(session_id[1:])
                for session_id in self._sessions
                if session_id[1:].isdigit()
            ),
            default=0,
        )
        self._seq = max(self._seq, floor) + 1
        assert f"t{self._seq}" not in self._sessions, f"session_id 冲突: t{self._seq}"
        now = self._now()
        session = Session(
            session_id=f"t{self._seq}",
            project_name=project_name,
            agent_label=agent_label,
            description=description,
            status=status,
            agent_session_id=agent_session_id,
            channel_key=conversation.channel_key(),
            conversation_id=conversation.conversation_id,
            thread_root_id=thread_root_id,
            workspace=workspace,
            created_at=now,
            updated_at=now,
            issue_url=issue_url,
            model=model,
            origin=origin,
        )
        self._sessions[session.session_id] = session
        self._flush()
        return session

    def update(self, session_id: str, **changes) -> Session | None:
        """就地更新 Session 字段（status/agent_session_id/turns…），刷新 updated_at 并落盘。

        改成终止态时顺带修剪历史。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        for k, v in changes.items():
            setattr(session, k, v)
        session.updated_at = self._now()
        if session.is_terminal:
            self._prune()
        self._flush()
        return session

    def add_action(self, session_id: str, action: dict) -> None:
        """追加一条动作到 Session 的审计日志（超 ``_MAX_ACTIONS`` 丢最旧），落盘。

        写透式：每条 tool_call 都刷一次盘，与 store 其余部分一致；chatty agent
        的写量对个人工具可接受（max_agents 默认 3），需要再批量化。
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.actions.append(action)
        if len(session.actions) > _MAX_ACTIONS:
            del session.actions[:-_MAX_ACTIONS]
        session.updated_at = self._now()
        self._flush()

    def _prune(self) -> None:
        """只保留最近 ``keep_terminal`` 个终止 Session。"""
        terminal = sorted(
            (session for session in self._sessions.values() if session.is_terminal),
            key=lambda session: session.updated_at,
        )
        for session in terminal[: -self._keep] if self._keep else terminal:
            del self._sessions[session.session_id]

    def clear_terminal(self) -> int:
        """清空所有终止 Session（/clear），返回清掉的条数。"""
        gone = [
            session_id
            for session_id, session in self._sessions.items()
            if session.is_terminal
        ]
        for session_id in gone:
            del self._sessions[session_id]
        if gone:
            self._flush()
        return len(gone)


class ProjectStore:
    """运行时注册的项目：name → Project，落盘 projects.json。

    与 config.toml 的 ``[[projects]]`` 种子集**分开**——种子是引导集（只读，
    改配置文件才能动），这里是用户在飞书里 ``/project add`` / ``register_project``
    注册的、可增删的项目。daemon 加载时把两者合并成有效项目表（种子 + 注册）。

    ``path=None`` 为纯内存（测试）。原子写 + 读损坏容错，与 SessionStore 一致。
    只被单个 daemon 实例（单线程 event loop）读写，无需加锁。
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._projects: dict[str, Project] = {}
        if path is not None:
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        data = _read_json(self._path)
        if data is None:
            return
        try:
            for name, d in (data.get("projects") or {}).items():
                self._projects[name] = Project(
                    name=d["name"],
                    path=Path(d["path"]),
                    default_agent=d["default_agent"],
                    repo=str(d.get("repo", "")).strip(),
                )
            logger.info("已加载 %d 个注册项目: %s", len(self._projects), self._path)
        except Exception:
            logger.warning("项目台账解析失败，忽略: %s", self._path, exc_info=True)
            self._projects = {}

    def _flush(self) -> None:
        if self._path is None:
            return
        payload = {
            "projects": {
                name: {
                    "name": p.name,
                    "path": str(p.path),
                    "default_agent": p.default_agent,
                    "repo": p.repo,
                }
                for name, p in self._projects.items()
            }
        }
        try:
            _atomic_write_json(self._path, payload)
        except Exception:
            logger.warning("项目台账写入失败: %s", self._path, exc_info=True)

    def get(self, name: str) -> Project | None:
        return self._projects.get(name)

    def all(self) -> dict[str, Project]:
        return dict(self._projects)

    def add(self, project: Project) -> None:
        """注册或更新一个项目（同名 upsert），落盘。"""
        self._projects[project.name] = project
        self._flush()

    def remove(self, name: str) -> bool:
        """删除一个已注册项目，返回是否存在。"""
        if name not in self._projects:
            return False
        del self._projects[name]
        self._flush()
        return True


#: Job 落盘/加载的字段白名单（向后兼容：只读认识的键，忽略未知/缺失）
_JOB_FIELDS = (
    "job_id",
    "task_id",
    "command",
    "cwd",
    "status",
    "exit_code",
    "output_file",
    "created_at",
    "finished_at",
    "timed_out",
)

#: 后台任务的机械态：running（跑着）→ exited（正常退出）/ killed（被杀/异常）
JOB_RUNNING = "running"
JOB_TERMINAL = frozenset({"exited", "killed"})


@dataclass
class Job:
    """一个 daemon 拥有的后台进程（#68）。

    与 Task 的关系：Job 绑定一个 ``task_id``——agent 经 CLI 请求 daemon 起的长任务
    （训练/build/测试）。daemon 拥有该进程（不是 agent 的子进程），故 agent 挂起不影响
    它；进程退出时 daemon 把「完成 + 输出尾部」入队该 task，agent 自动接续。

    ``command`` 是 argv 列表（exec，不经 shell）；``output_file`` 是重定向的输出文件。
    """

    job_id: str
    task_id: str
    command: list[str]
    cwd: str
    status: str = JOB_RUNNING
    exit_code: int | None = None
    output_file: str = ""
    created_at: float = 0.0
    finished_at: float = 0.0
    #: 是否因超时被 daemon 杀掉（区别于进程自己退出）；#68
    timed_out: bool = False

    @property
    def is_terminal(self) -> bool:
        return self.status in JOB_TERMINAL


class JobStore:
    """job_id → Job 台账（落盘 jobs.json），供后台任务追踪、``bg list/logs`` 与
    完成后路由回 Task。按 ``j<N>`` 短自增、持久单调计数器、**永不复用**。

    ``path=None`` 为纯内存（测试）。原子写 + 读损坏容错，与 SessionStore 一套。
    只被单个 daemon 实例（单线程 event loop）读写，无需加锁。
    v1 不做重启穿越——daemon 重启会丢在飞的 Job 的 await（记录仍在盘上，标记为
    running 的历史 Job 视为「结果未知」，不自动恢复，见 #68）。
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._jobs: dict[str, Job] = {}
        self._seq = 0
        if path is not None:
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        data = _read_json(self._path)
        if data is None:
            return
        try:
            self._seq = int(data.get("seq", 0))
            for jid, d in (data.get("jobs") or {}).items():
                self._jobs[jid] = Job(**{k: d[k] for k in _JOB_FIELDS if k in d})
        except Exception:
            logger.warning("后台任务台账解析失败，忽略: %s", self._path, exc_info=True)
            self._jobs = {}
            self._seq = 0

    def _flush(self) -> None:
        if self._path is None:
            return
        payload = {
            "seq": self._seq,
            "jobs": {jid: asdict(j) for jid, j in self._jobs.items()},
        }
        try:
            _atomic_write_json(self._path, payload)
        except Exception:
            logger.warning("后台任务台账写入失败: %s", self._path, exc_info=True)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def by_task(self, task_id: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.task_id == task_id]

    def create(
        self, *, task_id: str, command: list[str], cwd: str, output_file: str = ""
    ) -> Job:
        self._seq += 1
        job = Job(
            job_id=f"j{self._seq}",
            task_id=task_id,
            command=list(command),
            cwd=cwd,
            status=JOB_RUNNING,
            output_file=output_file,
            created_at=time.time(),
        )
        self._jobs[job.job_id] = job
        self._flush()
        return job

    def update(self, job_id: str, **changes) -> Job | None:
        job = self._jobs.get(job_id)
        if job is None:
            return None
        for k, v in changes.items():
            setattr(job, k, v)
        self._flush()
        return job


class ModelStore:
    """按 agent backend 缓存其 available_models（ACP 只在活 session 报，故缓存下来
    让 spawn 前也能列/校验）。落盘 models.json，原子写 + 读损坏容错。

    两条更新路径：worker 启动读到 available_models 时被动刷新（免费）；``/models
    refresh`` 临时起一次性 agent 主动刷新。copilot 不暴露模型 → 存空列表。

    ``path=None`` 为纯内存（测试）。只被单个 daemon 实例（单线程 event loop）读写。
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        #: backend -> {"models": list[str], "refreshed_at": float}
        self._by_backend: dict[str, dict] = {}
        if path is not None:
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        data = _read_json(self._path)
        if data is None:
            return
        try:
            for backend, d in (data.get("backends") or {}).items():
                models = [str(m) for m in (d.get("models") or [])]
                self._by_backend[backend] = {
                    "models": models,
                    "refreshed_at": float(d.get("refreshed_at", 0.0)),
                }
        except Exception:
            logger.warning("模型缓存解析失败，忽略: %s", self._path, exc_info=True)
            self._by_backend = {}

    def _flush(self) -> None:
        if self._path is None:
            return
        try:
            _atomic_write_json(self._path, {"backends": self._by_backend})
        except Exception:
            logger.warning("模型缓存写入失败: %s", self._path, exc_info=True)

    def get(self, backend: str) -> list[str]:
        """某 backend 已知的模型列表（无缓存则空列表）。"""
        return list((self._by_backend.get(backend) or {}).get("models") or [])

    def all(self) -> dict[str, dict]:
        """全部缓存：backend -> {models, refreshed_at}（副本）。"""
        return {k: dict(v) for k, v in self._by_backend.items()}

    def update(self, backend: str, models: list[str]) -> None:
        """写入/刷新某 backend 的模型清单（含 refreshed_at 时间戳），落盘。

        被动刷新会带着 models 反复调用——值没变（含空列表，如 copilot）时只更新
        时间戳、仍落盘，让 ``refreshed_at`` 反映「最近一次确认」。
        """
        self._by_backend[backend] = {
            "models": [str(m) for m in models],
            "refreshed_at": time.time(),
        }
        self._flush()
