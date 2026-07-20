"""任务台账 TaskStore 的单元测试。"""

from __future__ import annotations

from pathlib import Path

from feishu_dispatcher.store import _MAX_ACTIONS, TaskStore


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
    # 持久化：重载后动作还在
    assert len(TaskStore(p).get("t1").actions) == 2


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
