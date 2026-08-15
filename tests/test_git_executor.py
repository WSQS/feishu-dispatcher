"""有界 Git diff-tree 执行服务测试。"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from feishu_dispatcher._git_executor import (
    GitTreeBusyError,
    GitTreeExecutor,
    GitTreeRequestTimeout,
)


async def test_run_delegates_and_returns_result():
    calls: list[tuple[Path, str]] = []

    def collect(workspace: Path, rel_path: str) -> list[dict]:
        calls.append((workspace, rel_path))
        return [{"name": "x", "path": "x", "type": "file"}]

    ex = GitTreeExecutor(collect=collect)
    try:
        result = await ex.run(Path("ws"), "")
        assert result == [{"name": "x", "path": "x", "type": "file"}]
        assert calls == [(Path("ws"), "")]
    finally:
        await ex.aclose()


async def test_capacity_full_fails_fast():
    started = threading.Event()
    release = threading.Event()

    def collect(_workspace: Path, _rel_path: str) -> list[dict]:
        started.set()
        release.wait(timeout=5)
        return []

    ex = GitTreeExecutor(max_workers=1, max_pending=0, collect=collect)
    first = asyncio.create_task(ex.run(Path("ws"), ""))
    try:
        await asyncio.to_thread(started.wait, 5)
        with pytest.raises(GitTreeBusyError, match="容量已满"):
            await ex.run(Path("ws"), "src")
    finally:
        release.set()
        await first
        await ex.aclose()


async def test_request_timeout_keeps_capacity_until_worker_exits():
    finished = threading.Event()

    def collect(_workspace: Path, _rel_path: str) -> list[dict]:
        time.sleep(0.2)
        finished.set()
        return []

    ex = GitTreeExecutor(max_workers=1, max_pending=0, collect=collect)
    try:
        with pytest.raises(GitTreeRequestTimeout, match="请求超时"):
            await ex.run(Path("ws"), "", timeout=0.01)
        with pytest.raises(GitTreeBusyError, match="容量已满"):
            await ex.run(Path("ws"), "src")
        await asyncio.to_thread(finished.wait, 5)
        await asyncio.sleep(0)  # 让 Future done callback 释放容量
        assert await ex.run(Path("ws"), "src") == []
    finally:
        await ex.aclose()


async def test_aclose_wait_does_not_block_event_loop():
    started = threading.Event()
    release = threading.Event()

    def collect(_workspace: Path, _rel_path: str) -> list[dict]:
        started.set()
        release.wait(timeout=5)
        return []

    ex = GitTreeExecutor(collect=collect)
    run_task = asyncio.create_task(ex.run(Path("ws"), ""))
    await asyncio.to_thread(started.wait, 5)
    close_task = asyncio.create_task(ex.aclose())
    await asyncio.sleep(0.01)
    assert not close_task.done()  # 正在等 worker，但 event loop 仍可继续调度本协程
    release.set()
    assert await run_task == []
    await close_task


async def test_aclose_rejects_later_runs():
    ex = GitTreeExecutor(collect=lambda _w, _p: [])
    await ex.aclose()
    with pytest.raises(RuntimeError, match="已关闭"):
        await ex.run(Path("ws"), "")
