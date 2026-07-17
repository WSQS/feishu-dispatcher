"""会话持久化 SessionStore 的单元测试。"""

from __future__ import annotations

from pathlib import Path

from feishu_dispatcher.store import SessionRecord, SessionStore


def rec(tid: str = "t1") -> SessionRecord:
    return SessionRecord(
        thread_root_id=tid,
        project_name="proj",
        agent_label="copilot",
        session_id=f"sid_{tid}",
        cwd="C:/x",
    )


def test_in_memory_put_get_remove():
    s = SessionStore(None)
    s.put(rec("t1"))
    assert s.get("t1") == rec("t1")
    s.remove("t1")
    assert s.get("t1") is None


def test_persists_across_instances(tmp_path: Path):
    p = tmp_path / "sessions.json"
    s1 = SessionStore(p)
    s1.put(rec("t1"))
    s1.put(rec("t2"))
    s2 = SessionStore(p)  # 重新从磁盘加载
    assert s2.get("t1") == rec("t1")
    assert set(s2.all()) == {"t1", "t2"}


def test_remove_persists(tmp_path: Path):
    p = tmp_path / "sessions.json"
    s1 = SessionStore(p)
    s1.put(rec("t1"))
    s1.remove("t1")
    assert SessionStore(p).get("t1") is None


def test_corrupt_file_is_tolerated(tmp_path: Path):
    p = tmp_path / "sessions.json"
    p.write_text("not json{", encoding="utf-8")
    s = SessionStore(p)  # 不应抛异常
    assert s.all() == {}
    s.put(rec("t1"))  # 仍可写入
    assert SessionStore(p).get("t1") == rec("t1")


def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    p = tmp_path / "sessions.json"
    s = SessionStore(p)
    s.put(rec("t1"))
    assert p.exists()
    assert not (tmp_path / "sessions.json.tmp").exists()


def test_get_missing_returns_none():
    assert SessionStore(None).get("nope") is None
