"""SessionStore 的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from feishu_dispatcher import store as store_module
from feishu_dispatcher.config import Project
from feishu_dispatcher.store import (
    _MAX_ACTIONS,
    DelegationStore,
    JobStore,
    ModelStore,
    ProjectStore,
    Session,
    SessionStore,
)


def test_delegation_store_creates_and_reloads(tmp_path: Path):
    path = tmp_path / "delegations.json"
    store = DelegationStore(path)
    delegation = store.create(
        project_name="demo",
        manager_session_id="manager:demo",
        worker_session_id="t1",
        worker_turn_id="turn-1",
        instruction="修复测试",
    )
    store.update(
        delegation.delegation_id,
        status="waiting_manager",
        report_status="completed",
        report_message="测试通过",
    )

    loaded = DelegationStore(path)
    actual = loaded.get(delegation.delegation_id)
    assert actual is not None
    assert actual.worker_turn_id == "turn-1"
    assert actual.status == "waiting_manager"
    assert actual.report_status == "completed"
    assert (
        loaded.create(
            project_name="demo",
            manager_session_id="manager:demo",
            worker_session_id="t1",
            worker_turn_id="turn-2",
            instruction="继续检查",
        ).delegation_id
        == "d2"
    )


def conversation_payload(conversation_id: str) -> dict[str, object]:
    return {"conversation_id": conversation_id}


def make(
    store: SessionStore,
    *,
    thread: str = "om_1",
    project: str = "demo",
    desc: str = "做 X",
):
    return store.create(
        project_name=project,
        agent_label="copilot",
        description=desc,
        channel_key="feishu",
        conversation_payload={"conversation_id": thread},
        workspace="C:/x",
    )


def test_create_assigns_incrementing_ids():
    s = SessionStore(None)
    t1 = make(s, thread="om_1")
    t2 = make(s, thread="om_2")
    assert t1.session_id == "t1"
    assert t2.session_id == "t2"
    assert t1.status == "starting"


def test_session_store_returns_session_without_legacy_aliases():
    session = make(SessionStore(None))
    assert isinstance(session, Session)
    assert session.session_id == "t1"
    assert not hasattr(session, "task_id")
    assert not hasattr(store_module, "Task")
    assert not hasattr(store_module, "TaskStore")


def test_create_rejects_blank_channel_key_without_consuming_id():
    s = SessionStore(None)

    with pytest.raises(ValueError, match="channel_key"):
        s.create(
            project_name="demo",
            agent_label="copilot",
            description="做 X",
            channel_key=" ",
            conversation_payload={"conversation_id": "oc_main"},
            workspace="C:/x",
        )

    assert make(s).session_id == "t1"


def test_get_and_by_conversation():
    s = SessionStore(None)
    t = make(s, thread="om_1")
    assert s.get("t1") is t
    assert s.by_conversation("feishu", {"conversation_id": "om_1"}) is t
    assert s.by_conversation("feishu", {"conversation_id": "nope"}) is None


def test_by_conversation_is_scoped_to_channel():
    s = SessionStore(None)
    feishu_task = make(s, thread="shared-thread")
    web_conversation = conversation_payload("shared-thread")
    web_task = s.create(
        project_name="demo",
        agent_label="copilot",
        description="做 Y",
        channel_key="web",
        conversation_payload=web_conversation,
        workspace="C:/y",
    )

    assert (
        s.by_conversation("feishu", {"conversation_id": "shared-thread"}) is feishu_task
    )
    assert s.by_conversation("web", {"conversation_id": "shared-thread"}) is web_task
    assert s.by_conversation("other", {"conversation_id": "shared-thread"}) is None
    assert s.by_conversation("", {}) is None


def test_session_conversation_payload_persists_and_reloads(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s1 = SessionStore(p)
    task = s1.create(
        project_name="demo",
        agent_label="copilot",
        description="做 X",
        channel_key="web",
        conversation_payload=conversation_payload("workspace-main"),
        workspace="C:/x",
    )

    assert task.conversation_payload == {"conversation_id": "workspace-main"}
    assert SessionStore(p).get(task.session_id).conversation_payload == {
        "conversation_id": "workspace-main"
    }


def test_update_mutates_and_bumps():
    s = SessionStore(None)
    make(s)
    s.update("t1", status="idle", turns=2, agent_session_id="ses_x")
    t = s.get("t1")
    assert t.status == "idle"
    assert t.turns == 2
    assert t.agent_session_id == "ses_x"


def test_session_identity_fields_persist_with_new_disk_keys(tmp_path: Path):
    import json

    p = tmp_path / "tasks.json"
    s = SessionStore(p)
    task = s.create(
        project_name="demo",
        agent_label="copilot",
        description="做 X",
        channel_key="feishu",
        conversation_payload={"conversation_id": "om_1"},
        workspace="C:/x",
        agent_session_id="ses_x",
    )

    assert task.session_id == "t1"
    assert task.agent_session_id == "ses_x"
    assert not hasattr(task, "task_id")
    record = json.loads(p.read_text(encoding="utf-8"))["tasks"]["t1"]
    assert record["session_id"] == "t1"
    assert record["agent_session_id"] == "ses_x"
    assert record["conversation_payload"] == {"conversation_id": "om_1"}
    assert "conversation_id" not in record
    assert "task_id" not in record

    loaded = SessionStore(p).get("t1")
    assert loaded.session_id == "t1"
    assert loaded.agent_session_id == "ses_x"
    assert not hasattr(loaded, "task_id")


def test_persists_and_counter_never_reuses(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s1 = SessionStore(p)
    make(s1, thread="om_1")
    make(s1, thread="om_2")
    s1.update("t1", status="idle")
    s2 = SessionStore(p)  # reload
    assert s2.get("t1").status == "idle"
    assert s2.by_conversation("feishu", {"conversation_id": "om_2"}).session_id == "t2"
    # 计数器随之持久化 → 下一个是 t3，不复用
    assert make(s2, thread="om_3").session_id == "t3"


def test_prune_keeps_recent_terminal_but_counter_monotonic():
    s = SessionStore(None, keep_terminal=1)
    make(s, thread="om_1")
    make(s, thread="om_2")
    s.update("t1", status="done")
    s.update("t2", status="done")  # keep_terminal=1 → t1 被修剪
    assert s.get("t1") is None
    assert s.get("t2") is not None
    assert make(s, thread="om_3").session_id == "t3"  # 永不复用 t1


def test_create_self_heals_reverted_seq_never_reuses_id():
    """#81：即使 seq 被回退到已存在 id 之下，create() 也从现有 id 推下界、不复用。"""
    s = SessionStore(None)
    make(s, thread="om_1")  # t1
    make(s, thread="om_2")  # t2
    make(s, thread="om_3")  # t3
    # 模拟多实例踩踏 / 台账被回退：计数器倒退到 1
    s._seq = 1
    t = make(s, thread="om_4")
    assert t.session_id == "t4"  # 不是 t2/t3——现有最大 id 是 3，跳到 4
    assert (
        s.by_conversation("feishu", {"conversation_id": "om_1"}).session_id == "t1"
    )  # 老任务映射未被覆盖
    assert s.by_conversation("feishu", {"conversation_id": "om_4"}).session_id == "t4"


def test_create_reload_with_tampered_seq_does_not_clobber(tmp_path: Path):
    """#81 现实路径：tasks.json 里 seq 被回退后重载，新建仍不覆盖已有任务。"""
    import json

    p = tmp_path / "tasks.json"
    s1 = SessionStore(p)
    make(s1, thread="om_1")  # t1
    make(s1, thread="om_2")  # t2
    # 把落盘的 seq 篡改回退（模拟另一进程写了个旧 seq）
    data = json.loads(p.read_text(encoding="utf-8"))
    data["seq"] = 0
    p.write_text(json.dumps(data), encoding="utf-8")
    s2 = SessionStore(p)  # 重载：seq=0，但仍有 t1/t2
    t = make(s2, thread="om_3")
    assert t.session_id == "t3"  # 不复用 t1
    assert s2.by_conversation("feishu", {"conversation_id": "om_1"}).session_id == "t1"
    assert s2.by_conversation("feishu", {"conversation_id": "om_2"}).session_id == "t2"


def test_failed_is_resumable_not_terminal_and_error_persists(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s1 = SessionStore(p)
    make(s1, thread="om_1")
    s1.update("t1", status="failed", error_message="RuntimeError: boom")
    t = s1.get("t1")
    assert t.is_resumable and not t.is_terminal  # failed 可恢复、非终止
    # 持久化往返：error_message 落盘并读回
    s2 = SessionStore(p)
    assert s2.get("t1").status == "failed"
    assert s2.get("t1").error_message == "RuntimeError: boom"


def test_failed_not_pruned(tmp_path: Path):
    # failed 不进历史修剪（同 suspended，可恢复态不清）
    s = SessionStore(None, keep_terminal=1)
    make(s, thread="om_1")
    make(s, thread="om_2")
    s.update("t1", status="failed")
    s.update("t2", status="done")  # terminal，触发 _prune（只清终止态）
    assert s.get("t1") is not None  # failed 未被清


def test_active_split():
    s = SessionStore(None)
    make(s, thread="om_1")  # starting → active
    make(s, thread="om_2")
    s.update("t2", status="stopped")  # terminal
    assert [t.session_id for t in s.active()] == ["t1"]


def test_corrupt_file_tolerated(tmp_path: Path):
    p = tmp_path / "tasks.json"
    p.write_text("not json{", encoding="utf-8")
    s = SessionStore(p)
    assert s.all() == []
    make(s, thread="om_1")
    assert SessionStore(p).get("t1") is not None


def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s = SessionStore(p)
    make(s)
    assert p.exists()
    assert not (tmp_path / "tasks.json.tmp").exists()


# ---------------------------------------------------------------------- #
# 落盘鲁棒性（#83）：fsync 持久化 + .bak 回退 + 损坏存档不清空
# ---------------------------------------------------------------------- #


def test_flush_fsyncs_data(tmp_path: Path, monkeypatch):
    """落盘时对临时文件 fsync（把数据真正刷到盘，防掉电后原子改名指向未写入的块）。"""
    import os

    import feishu_dispatcher._atomic as atomic_mod

    calls = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(atomic_mod.os, "fsync", spy)
    make(SessionStore(tmp_path / "tasks.json"))
    assert calls  # 至少 fsync 了临时文件


def test_flush_keeps_backup_of_previous_version(tmp_path: Path):
    import json

    p = tmp_path / "tasks.json"
    s = SessionStore(p)
    make(s, thread="om_1")  # 首次落盘：此前无主文件 → 无 .bak
    assert not (tmp_path / "tasks.json.bak").exists()
    make(s, thread="om_2")  # 二次落盘：上一版本降级为 .bak
    bak = tmp_path / "tasks.json.bak"
    assert bak.exists()
    assert set(json.loads(bak.read_text(encoding="utf-8"))["tasks"]) == {"t1"}


def test_corrupt_primary_recovers_from_backup_not_wiped(tmp_path: Path):
    """主文件损坏时从 .bak 恢复历史（不再静默清空），seq 不归零 → 不复用已恢复的 id。"""
    p = tmp_path / "tasks.json"
    s1 = SessionStore(p)
    make(s1, thread="om_1")  # t1
    make(s1, thread="om_2")  # t2；.bak = {t1}
    make(s1, thread="om_3")  # t3；.bak = {t1, t2}
    p.write_text("truncated{", encoding="utf-8")  # 系统崩溃把主文件写花
    s2 = SessionStore(p)  # 主损坏 → 回退 .bak（含 t1、t2）
    assert s2.by_conversation("feishu", {"conversation_id": "om_1"}).session_id == "t1"
    assert s2.by_conversation("feishu", {"conversation_id": "om_2"}).session_id == "t2"
    nxt = make(s2, thread="om_4")  # seq 从 .bak 恢复 → 不落回 t1
    assert nxt.session_id not in {"t1", "t2"}
    assert int(nxt.session_id[1:]) >= 3  # 旧行为会给 t1（清空+seq 归零）


def test_corrupt_primary_without_backup_archives_and_empties(tmp_path: Path):
    """无 .bak 可退时空起，但损坏主文件被改名存档（.corrupt-*），不再挡下次启动。"""
    p = tmp_path / "tasks.json"
    p.write_text("not json{", encoding="utf-8")
    s = SessionStore(p)
    assert s.all() == []
    assert len(list(tmp_path.glob("tasks.json.corrupt-*"))) == 1
    assert not p.exists()  # 损坏主文件已移走


def test_missing_primary_recovers_from_backup(tmp_path: Path):
    """主文件丢失（.bak 尚在）时也从备份恢复，而非当作全新空台账。"""
    p = tmp_path / "tasks.json"
    s1 = SessionStore(p)
    make(s1, thread="om_1")
    make(s1, thread="om_2")  # .bak = {t1}
    p.unlink()
    s2 = SessionStore(p)
    assert s2.by_conversation("feishu", {"conversation_id": "om_1"}).session_id == "t1"


def test_clear_terminal():
    s = SessionStore(None)
    make(s, thread="om_1")
    make(s, thread="om_2")
    s.update("t2", status="done")
    assert s.clear_terminal() == 1
    assert s.get("t2") is None
    assert s.get("t1") is not None


def test_add_action_appends_and_persists(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s = SessionStore(p)
    make(s, thread="om_1")
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "Editing a.py"})
    s.add_action("t1", {"turn": 1, "kind": "execute", "title": "pytest"})
    assert [a["title"] for a in s.get("t1").actions] == ["Editing a.py", "pytest"]
    # 持久化：重载后动作还在
    assert len(SessionStore(p).get("t1").actions) == 2


def test_add_action_caps_at_max_dropping_oldest():
    s = SessionStore(None)
    make(s, thread="om_1")
    for i in range(_MAX_ACTIONS + 5):
        s.add_action("t1", {"turn": 1, "kind": "edit", "title": f"edit {i}"})
    actions = s.get("t1").actions
    assert len(actions) == _MAX_ACTIONS
    assert actions[0]["title"] == "edit 5"  # 最旧 5 条被丢
    assert actions[-1]["title"] == f"edit {_MAX_ACTIONS + 4}"


def test_add_action_unknown_task_is_noop():
    s = SessionStore(None)
    s.add_action("t404", {"turn": 1, "kind": "edit", "title": "x"})  # 不抛
    assert s.get("t404") is None


# ---------------------------------------------------------------------- #
# ProjectStore：运行时注册项目（projects.json）
# ---------------------------------------------------------------------- #


def _proj(name: str, agent: str = "opencode") -> Project:
    return Project(name=name, path=Path(f"C:/work/{name}"), default_agent=agent)


def test_project_store_add_get_all():
    s = ProjectStore(None)
    assert s.all() == {}
    s.add(_proj("foo"))
    assert s.get("foo") == _proj("foo")
    assert set(s.all()) == {"foo"}


def test_project_store_add_is_upsert():
    s = ProjectStore(None)
    s.add(_proj("foo", "copilot"))
    s.add(_proj("foo", "opencode"))  # 同名更新
    assert s.get("foo").default_agent == "opencode"
    assert len(s.all()) == 1


def test_project_store_remove():
    s = ProjectStore(None)
    s.add(_proj("foo"))
    assert s.remove("foo") is True
    assert s.get("foo") is None
    assert s.remove("foo") is False  # 已不存在


def test_project_store_persists_and_reloads(tmp_path: Path):
    path = tmp_path / "projects.json"
    s1 = ProjectStore(path)
    s1.add(_proj("foo", "opencode"))
    s1.add(_proj("bar", "copilot"))
    s1.remove("bar")
    # 新实例从盘上读回
    s2 = ProjectStore(path)
    assert set(s2.all()) == {"foo"}
    assert s2.get("foo").path == Path("C:/work/foo")
    assert s2.get("foo").default_agent == "opencode"


def test_project_store_corrupt_file_tolerated(tmp_path: Path):
    path = tmp_path / "projects.json"
    path.write_text("{ not json", encoding="utf-8")
    s = ProjectStore(path)  # 不抛
    assert s.all() == {}


def test_project_store_recovers_from_backup(tmp_path: Path):
    """共用的落盘鲁棒性（#83）对 ProjectStore 同样生效：损坏主文件回退 .bak。"""
    path = tmp_path / "projects.json"
    s1 = ProjectStore(path)
    s1.add(_proj("foo"))
    s1.add(_proj("bar"))  # .bak = {foo}
    path.write_text("broken{", encoding="utf-8")
    s2 = ProjectStore(path)
    assert set(s2.all()) == {"foo"}  # 从 .bak 恢复，而非清空


def test_project_store_memory_mode_writes_nothing(tmp_path: Path):
    s = ProjectStore(None)  # path=None
    s.add(_proj("foo"))
    assert list(tmp_path.iterdir()) == []  # 没落盘


# ModelStore：按 backend 的模型缓存（models.json，#65）


def test_task_create_records_model():
    s = SessionStore(None)
    t = s.create(
        project_name="p",
        agent_label="opencode",
        description="x",
        channel_key="feishu",
        conversation_payload={"conversation_id": "om_1"},
        workspace="C:/x",
        model="glm-5",
    )
    assert t.model == "glm-5"


def test_model_store_update_get():
    s = ModelStore(None)
    assert s.get("opencode") == []  # 未知 backend → 空
    s.update("opencode", ["a", "b"])
    assert s.get("opencode") == ["a", "b"]
    assert "refreshed_at" in s.all()["opencode"]


def test_model_store_empty_list_ok_for_copilot():
    s = ModelStore(None)
    s.update("copilot", [])  # copilot 不暴露模型 → 存空、仍带时间戳
    assert s.get("copilot") == []
    assert "copilot" in s.all()


def test_model_store_persists_and_reloads(tmp_path: Path):
    path = tmp_path / "models.json"
    s1 = ModelStore(path)
    s1.update("opencode", ["m1", "m2"])
    s2 = ModelStore(path)
    assert s2.get("opencode") == ["m1", "m2"]


def test_model_store_corrupt_file_tolerated(tmp_path: Path):
    path = tmp_path / "models.json"
    path.write_text("{ not json", encoding="utf-8")
    s = ModelStore(path)  # 不抛
    assert s.all() == {}


def test_model_store_memory_mode_writes_nothing(tmp_path: Path):
    s = ModelStore(None)
    s.update("opencode", ["a"])
    assert list(tmp_path.iterdir()) == []  # 没落盘


# JobStore：daemon 拥有的后台任务台账（jobs.json，#68）


def test_job_store_create_assigns_incrementing_ids():
    s = JobStore(None)
    j1 = s.create(task_id="t1", command=["python", "train.py"], cwd="C:/x")
    j2 = s.create(task_id="t1", command=["gradlew", "build"], cwd="C:/x")
    assert j1.job_id == "j1"
    assert j2.job_id == "j2"
    assert j1.status == "running"
    assert j1.exit_code is None
    assert not j1.is_terminal


def test_job_store_update_and_terminal():
    s = JobStore(None)
    j = s.create(task_id="t1", command=["x"], cwd="c")
    s.update(j.job_id, status="exited", exit_code=0, finished_at=123.0)
    got = s.get(j.job_id)
    assert got.status == "exited" and got.exit_code == 0 and got.finished_at == 123.0
    assert got.is_terminal


def test_job_store_by_task():
    s = JobStore(None)
    s.create(task_id="t1", command=["a"], cwd="c")
    s.create(task_id="t2", command=["b"], cwd="c")
    s.create(task_id="t1", command=["c"], cwd="c")
    assert [j.job_id for j in s.by_task("t1")] == ["j1", "j3"]
    assert [j.job_id for j in s.by_task("t2")] == ["j2"]


def test_job_store_persists_and_counter_monotonic(tmp_path: Path):
    p = tmp_path / "jobs.json"
    s1 = JobStore(p)
    s1.create(task_id="t1", command=["python", "train.py"], cwd="C:/x")
    s1.update("j1", status="exited", exit_code=0)
    s2 = JobStore(p)  # reload
    assert s2.get("j1").status == "exited"
    assert s2.get("j1").command == ["python", "train.py"]
    # 计数器随之持久化 → 下一个是 j2，不复用
    assert s2.create(task_id="t2", command=["x"], cwd="c").job_id == "j2"


def test_job_store_corrupt_file_tolerated(tmp_path: Path):
    p = tmp_path / "jobs.json"
    p.write_text("{ not json", encoding="utf-8")
    s = JobStore(p)
    assert s.all() == []
    assert s.create(task_id="t1", command=["x"], cwd="c").job_id == "j1"


def test_job_store_memory_mode_writes_nothing(tmp_path: Path):
    s = JobStore(None)
    s.create(task_id="t1", command=["x"], cwd="c")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------- #
# Session.origin（spawn/attach）与 (agent, session_id) 查重（#99 前置）
# ---------------------------------------------------------------------- #


def test_task_origin_defaults_to_spawn():
    s = SessionStore(None)
    t = make(s, thread="om_1")
    assert t.origin == "spawn"


def test_task_create_with_origin_attach():
    s = SessionStore(None)
    t = s.create(
        project_name="p",
        agent_label="opencode",
        description="附着",
        channel_key="feishu",
        conversation_payload={"conversation_id": "om_1"},
        workspace="C:/x",
        agent_session_id="ext_sid_1",
        origin="attach",
    )
    assert t.origin == "attach"


def test_task_origin_persists_and_reloads(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s1 = SessionStore(p)
    make(s1, thread="om_1")
    s1.create(
        project_name="p",
        agent_label="opencode",
        description="附着",
        channel_key="feishu",
        conversation_payload={"conversation_id": "om_2"},
        workspace="C:/x",
        agent_session_id="ext_sid_1",
        origin="attach",
    )
    s2 = SessionStore(p)
    assert s2.by_conversation("feishu", {"conversation_id": "om_1"}).origin == "spawn"
    assert s2.by_conversation("feishu", {"conversation_id": "om_2"}).origin == "attach"


def test_old_tasks_json_with_thread_root_is_rejected(tmp_path: Path):
    import json

    p = tmp_path / "tasks.json"
    payload = {
        "seq": 1,
        "tasks": {
            "t1": {
                "session_id": "t1",
                "project_name": "demo",
                "agent_label": "copilot",
                "description": "旧任务",
                "status": "suspended",
                "agent_session_id": "old_sid",
                "channel_key": "feishu",
                "conversation_id": "oc_1",
                "thread_root_id": "om_1",
                "workspace": "C:/x",
                "turns": 3,
                "created_at": 0.0,
                "updated_at": 0.0,
                "actions": [],
                "last_output": "",
                "model": "",
                "error_message": "",
                "issue_url": "",
            }
        },
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert SessionStore(p).all() == []


def test_old_conversation_id_schema_is_rejected(tmp_path: Path):
    import json

    p = tmp_path / "tasks.json"
    payload = {
        "seq": 1,
        "tasks": {
            "t1": {
                "session_id": "t1",
                "project_name": "demo",
                "agent_label": "copilot",
                "description": "旧任务",
                "status": "suspended",
                "agent_session_id": "old_sid",
                "channel_key": "feishu",
                "conversation_id": "om_1",
                "workspace": "C:/x",
                "turns": 3,
                "created_at": 0.0,
                "updated_at": 0.0,
                "actions": [],
                "last_output": "",
                "model": "",
                "error_message": "",
                "issue_url": "",
                "origin": "spawn",
            }
        },
    }
    p.write_text(json.dumps(payload), encoding="utf-8")

    assert SessionStore(p).all() == []


def test_old_session_identity_schema_is_rejected(tmp_path: Path):
    import json

    p = tmp_path / "tasks.json"
    payload = {
        "seq": 1,
        "tasks": {
            "t1": {
                "task_id": "t1",
                "project_name": "demo",
                "agent_label": "copilot",
                "description": "旧任务",
                "status": "suspended",
                "session_id": "old_sid",
            }
        },
    }
    p.write_text(json.dumps(payload), encoding="utf-8")

    assert SessionStore(p).all() == []


def test_by_agent_session_matches_agent_and_session():
    s = SessionStore(None)
    make(s, thread="om_1")
    s.update("t1", agent_session_id="sid_a", agent_label="copilot")
    make(s, thread="om_2", project="other")
    s.update("t2", agent_session_id="sid_b", agent_label="opencode")
    assert s.by_agent_session("copilot", "sid_a").session_id == "t1"
    assert s.by_agent_session("opencode", "sid_b").session_id == "t2"
    assert s.by_agent_session("copilot", "sid_b") is None  # agent 不匹配
    assert s.by_agent_session("opencode", "sid_a") is None  # session 不匹配


def test_by_agent_session_cross_agent_same_session_id_no_conflict():
    # 跨 agent 同名 session_id 不冲突：不同 backend 的 session 命名空间独立。
    s = SessionStore(None)
    s.create(
        project_name="p",
        agent_label="copilot",
        description="a",
        channel_key="feishu",
        conversation_payload={"conversation_id": "om_1"},
        workspace="C:/x",
        agent_session_id="shared_sid",
    )
    s.create(
        project_name="p",
        agent_label="opencode",
        description="b",
        channel_key="feishu",
        conversation_payload={"conversation_id": "om_2"},
        workspace="C:/x",
        agent_session_id="shared_sid",
    )
    assert s.by_agent_session("copilot", "shared_sid").conversation_payload == {
        "conversation_id": "om_1"
    }
    assert s.by_agent_session("opencode", "shared_sid").conversation_payload == {
        "conversation_id": "om_2"
    }


def test_by_agent_session_empty_keys_never_match():
    s = SessionStore(None)
    make(s, thread="om_1")
    s.update("t1", agent_session_id="sid_a", agent_label="copilot")
    assert s.by_agent_session("", "sid_a") is None
    assert s.by_agent_session("copilot", "") is None
    assert s.by_agent_session("", "") is None
