"""单实例锁 SingleInstanceLock 的单元测试（#81）。"""

from __future__ import annotations

import os
from pathlib import Path

from feishu_dispatcher.singleinstance import SingleInstanceLock


def test_acquire_creates_lock_and_writes_pid(tmp_path: Path):
    lock = SingleInstanceLock(tmp_path / "daemon.lock")
    assert lock.acquire() is None
    try:
        assert lock.path.exists()
        assert lock.path.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        lock.release()


def test_second_acquire_fails_and_reports_holder_pid(tmp_path: Path):
    p = tmp_path / "daemon.lock"
    first = SingleInstanceLock(p)
    assert first.acquire() is None
    try:
        second = SingleInstanceLock(p)
        holder = second.acquire()
        assert holder == str(os.getpid())  # 拿不到锁，报出持锁 pid
    finally:
        first.release()


def test_release_allows_reacquire(tmp_path: Path):
    p = tmp_path / "daemon.lock"
    first = SingleInstanceLock(p)
    assert first.acquire() is None
    first.release()
    # 释放后第二个实例能拿到（模拟旧 daemon 干净退出、新 daemon 起来）
    second = SingleInstanceLock(p)
    assert second.acquire() is None
    second.release()


def test_release_is_idempotent(tmp_path: Path):
    lock = SingleInstanceLock(tmp_path / "daemon.lock")
    lock.acquire()
    lock.release()
    lock.release()  # 再次释放不应报错


def test_acquire_creates_parent_dir(tmp_path: Path):
    nested = tmp_path / "does" / "not" / "exist" / "daemon.lock"
    lock = SingleInstanceLock(nested)
    assert lock.acquire() is None
    try:
        assert nested.exists()
    finally:
        lock.release()
