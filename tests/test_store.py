"""任务台账 TaskStore 的单元测试。"""

from __future__ import annotations

from pathlib import Path

from feishu_dispatcher.config import Project
from feishu_dispatcher.store import (
    _MAX_ACTIONS,
    JobStore,
    ModelStore,
    ProjectStore,
    TaskStore,
)


def make(
    store: TaskStore, *, thread: str = "om_1", project: str = "demo", desc: str = "做 X"
):
    return store.create(
        project_name=project,
        agent_label="copilot",
        description=desc,
        thread_root_id=thread,
        workspace="C:/x",
    )


def test_create_assigns_incrementing_ids():
    s = TaskStore(None)
    t1 = make(s, thread="om_1")
    t2 = make(s, thread="om_2")
    assert t1.task_id == "t1"
    assert t2.task_id == "t2"
    assert t1.status == "starting"


def test_get_and_by_thread():
    s = TaskStore(None)
    t = make(s, thread="om_1")
    assert s.get("t1") is t
    assert s.by_thread("om_1") is t
    assert s.by_thread("nope") is None


def test_update_mutates_and_bumps():
    s = TaskStore(None)
    make(s)
    s.update("t1", status="idle", turns=2, session_id="ses_x")
    t = s.get("t1")
    assert t.status == "idle"
    assert t.turns == 2
    assert t.session_id == "ses_x"


def test_persists_and_counter_never_reuses(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s1 = TaskStore(p)
    make(s1, thread="om_1")
    make(s1, thread="om_2")
    s1.update("t1", status="idle")
    s2 = TaskStore(p)  # reload
    assert s2.get("t1").status == "idle"
    assert s2.by_thread("om_2").task_id == "t2"
    # 计数器随之持久化 → 下一个是 t3，不复用
    assert make(s2, thread="om_3").task_id == "t3"


def test_prune_keeps_recent_terminal_but_counter_monotonic():
    s = TaskStore(None, keep_terminal=1)
    make(s, thread="om_1")
    make(s, thread="om_2")
    s.update("t1", status="done")
    s.update("t2", status="done")  # keep_terminal=1 → t1 被修剪
    assert s.get("t1") is None
    assert s.get("t2") is not None
    assert make(s, thread="om_3").task_id == "t3"  # 永不复用 t1


def test_create_self_heals_reverted_seq_never_reuses_id():
    """#81：即使 seq 被回退到已存在 id 之下，create() 也从现有 id 推下界、不复用。"""
    s = TaskStore(None)
    make(s, thread="om_1")  # t1
    make(s, thread="om_2")  # t2
    make(s, thread="om_3")  # t3
    # 模拟多实例踩踏 / 台账被回退：计数器倒退到 1
    s._seq = 1
    t = make(s, thread="om_4")
    assert t.task_id == "t4"  # 不是 t2/t3——现有最大 id 是 3，跳到 4
    assert s.by_thread("om_1").task_id == "t1"  # 老任务映射未被覆盖
    assert s.by_thread("om_4").task_id == "t4"


def test_create_reload_with_tampered_seq_does_not_clobber(tmp_path: Path):
    """#81 现实路径：tasks.json 里 seq 被回退后重载，新建仍不覆盖已有任务。"""
    import json

    p = tmp_path / "tasks.json"
    s1 = TaskStore(p)
    make(s1, thread="om_1")  # t1
    make(s1, thread="om_2")  # t2
    # 把落盘的 seq 篡改回退（模拟另一进程写了个旧 seq）
    data = json.loads(p.read_text(encoding="utf-8"))
    data["seq"] = 0
    p.write_text(json.dumps(data), encoding="utf-8")
    s2 = TaskStore(p)  # 重载：seq=0，但仍有 t1/t2
    t = make(s2, thread="om_3")
    assert t.task_id == "t3"  # 不复用 t1
    assert s2.by_thread("om_1").task_id == "t1"
    assert s2.by_thread("om_2").task_id == "t2"


def test_failed_is_resumable_not_terminal_and_error_persists(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s1 = TaskStore(p)
    make(s1, thread="om_1")
    s1.update("t1", status="failed", error_message="RuntimeError: boom")
    t = s1.get("t1")
    assert t.is_resumable and not t.is_terminal  # failed 可恢复、非终止
    # 持久化往返：error_message 落盘并读回
    s2 = TaskStore(p)
    assert s2.get("t1").status == "failed"
    assert s2.get("t1").error_message == "RuntimeError: boom"


def test_failed_not_pruned(tmp_path: Path):
    # failed 不进历史修剪（同 suspended，可恢复态不清）
    s = TaskStore(None, keep_terminal=1)
    make(s, thread="om_1")
    make(s, thread="om_2")
    s.update("t1", status="failed")
    s.update("t2", status="done")  # terminal，触发 _prune（只清终止态）
    assert s.get("t1") is not None  # failed 未被清


def test_active_split():
    s = TaskStore(None)
    make(s, thread="om_1")  # starting → active
    make(s, thread="om_2")
    s.update("t2", status="stopped")  # terminal
    assert [t.task_id for t in s.active()] == ["t1"]


def test_corrupt_file_tolerated(tmp_path: Path):
    p = tmp_path / "tasks.json"
    p.write_text("not json{", encoding="utf-8")
    s = TaskStore(p)
    assert s.all() == []
    make(s, thread="om_1")
    assert TaskStore(p).get("t1") is not None


def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s = TaskStore(p)
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
    make(TaskStore(tmp_path / "tasks.json"))
    assert calls  # 至少 fsync 了临时文件


def test_flush_keeps_backup_of_previous_version(tmp_path: Path):
    import json

    p = tmp_path / "tasks.json"
    s = TaskStore(p)
    make(s, thread="om_1")  # 首次落盘：此前无主文件 → 无 .bak
    assert not (tmp_path / "tasks.json.bak").exists()
    make(s, thread="om_2")  # 二次落盘：上一版本降级为 .bak
    bak = tmp_path / "tasks.json.bak"
    assert bak.exists()
    assert set(json.loads(bak.read_text(encoding="utf-8"))["tasks"]) == {"t1"}


def test_corrupt_primary_recovers_from_backup_not_wiped(tmp_path: Path):
    """主文件损坏时从 .bak 恢复历史（不再静默清空），seq 不归零 → 不复用已恢复的 id。"""
    p = tmp_path / "tasks.json"
    s1 = TaskStore(p)
    make(s1, thread="om_1")  # t1
    make(s1, thread="om_2")  # t2；.bak = {t1}
    make(s1, thread="om_3")  # t3；.bak = {t1, t2}
    p.write_text("truncated{", encoding="utf-8")  # 系统崩溃把主文件写花
    s2 = TaskStore(p)  # 主损坏 → 回退 .bak（含 t1、t2）
    assert s2.by_thread("om_1").task_id == "t1"  # 历史未被清空
    assert s2.by_thread("om_2").task_id == "t2"
    nxt = make(s2, thread="om_4")  # seq 从 .bak 恢复 → 不落回 t1
    assert nxt.task_id not in {"t1", "t2"}
    assert int(nxt.task_id[1:]) >= 3  # 旧行为会给 t1（清空+seq 归零）


def test_corrupt_primary_without_backup_archives_and_empties(tmp_path: Path):
    """无 .bak 可退时空起，但损坏主文件被改名存档（.corrupt-*），不再挡下次启动。"""
    p = tmp_path / "tasks.json"
    p.write_text("not json{", encoding="utf-8")
    s = TaskStore(p)
    assert s.all() == []
    assert len(list(tmp_path.glob("tasks.json.corrupt-*"))) == 1
    assert not p.exists()  # 损坏主文件已移走


def test_missing_primary_recovers_from_backup(tmp_path: Path):
    """主文件丢失（.bak 尚在）时也从备份恢复，而非当作全新空台账。"""
    p = tmp_path / "tasks.json"
    s1 = TaskStore(p)
    make(s1, thread="om_1")
    make(s1, thread="om_2")  # .bak = {t1}
    p.unlink()
    s2 = TaskStore(p)
    assert s2.by_thread("om_1").task_id == "t1"


def test_clear_terminal():
    s = TaskStore(None)
    make(s, thread="om_1")
    make(s, thread="om_2")
    s.update("t2", status="done")
    assert s.clear_terminal() == 1
    assert s.get("t2") is None
    assert s.get("t1") is not None


def test_add_action_appends_and_persists(tmp_path: Path):
    p = tmp_path / "tasks.json"
    s = TaskStore(p)
    make(s, thread="om_1")
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "Editing a.py"})
    s.add_action("t1", {"turn": 1, "kind": "execute", "title": "pytest"})
    assert [a["title"] for a in s.get("t1").actions] == ["Editing a.py", "pytest"]
    # 批量刷盘：add_action 只置脏不落盘，flush_dirty 才真正写盘
    s.flush_dirty()
    assert len(TaskStore(p).get("t1").actions) == 2


def test_add_action_does_not_flush_until_flush_dirty(tmp_path: Path):
    # 批量刷盘：多条 add_action 之间不写盘，flush_dirty 只写一次
    p = tmp_path / "tasks.json"
    s = TaskStore(p)
    make(s, thread="om_1")
    count = _spy_writes(s)
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "a"})
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "b"})
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "c"})
    assert count[0] == 0  # 期间零写盘
    s.flush_dirty()
    assert count[0] == 1  # 一次 flush_dirty 只写一次（而非每条写一次）
    # 再次 flush_dirty 无变更时 no-op
    s.flush_dirty()
    assert count[0] == 1


def test_update_action_status_backfills_by_tool_call_id():
    # tool_call_update 完成状态：按 tool_call_id 回填到已记录的起始动作
    s = TaskStore(None)
    make(s, thread="om_1")
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "Editing a.py", "tool_call_id": "tc1"})
    s.add_action("t1", {"turn": 1, "kind": "execute", "title": "pytest", "tool_call_id": "tc2"})
    s.update_action_status("t1", "tc2", "failed")
    actions = s.get("t1").actions
    assert actions[0].get("status") is None  # tc1 未结算
    assert actions[1]["status"] == "failed"  # tc2 按命中的 id 回填


def test_update_action_status_does_not_overwrite_already_set():
    # 已结算的动作不被后续同名 update 覆盖；回填到更早的、尚无 status 的同名条目
    s = TaskStore(None)
    make(s, thread="om_1")
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "e1", "tool_call_id": "tc1"})
    s.add_action("t1", {"turn": 2, "kind": "edit", "title": "e2", "tool_call_id": "tc1"})
    s.update_action_status("t1", "tc1", "completed")  # 命中最近的 tc1（第2轮）
    s.update_action_status("t1", "tc1", "completed")  # 命中更早的 tc1（第1轮）
    assert s.get("t1").actions[1]["status"] == "completed"
    assert s.get("t1").actions[0]["status"] == "completed"


def test_update_action_status_unknown_id_is_noop():
    s = TaskStore(None)
    make(s, thread="om_1")
    s.add_action("t1", {"turn": 1, "kind": "edit", "title": "x", "tool_call_id": "tc1"})
    s.update_action_status("t1", "tc404", "completed")  # 找不到，忽略
    assert s.get("t1").actions[0].get("status") is None


def test_update_action_status_unknown_task_is_noop():
    s = TaskStore(None)
    s.update_action_status("t404", "tc1", "completed")  # 不抛


def _spy_writes(store: TaskStore) -> list[int]:
    """计数 store 落盘次数：把 _flush 包一层计数器（add_action/flush_dirty 共用）。
    返回单元素列表（``count[0]``），随 store 写盘自增。"""
    count = [0]
    orig = store._flush

    def counting() -> None:
        orig()
        count[0] += 1

    store._flush = counting  # type: ignore[method-assign]
    return count


def test_add_action_caps_at_max_dropping_oldest():
    s = TaskStore(None)
    make(s, thread="om_1")
    for i in range(_MAX_ACTIONS + 5):
        s.add_action("t1", {"turn": 1, "kind": "edit", "title": f"edit {i}"})
    actions = s.get("t1").actions
    assert len(actions) == _MAX_ACTIONS
    assert actions[0]["title"] == "edit 5"  # 最旧 5 条被丢
    assert actions[-1]["title"] == f"edit {_MAX_ACTIONS + 4}"


def test_add_action_unknown_task_is_noop():
    s = TaskStore(None)
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
    s = TaskStore(None)
    t = s.create(
        project_name="p",
        agent_label="opencode",
        description="x",
        thread_root_id="om_1",
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
