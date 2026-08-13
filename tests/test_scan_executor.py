"""有界扫描执行服务的单测。"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from feishu_dispatcher._scan_executor import ScanExecutor


async def test_run_delegates_and_returns_result():
    calls: list[tuple[str, str]] = []

    def fake_scan(dir_path: Path, rel_path: str) -> list[dict]:
        calls.append((str(dir_path), rel_path))
        return [{"name": "x", "path": rel_path, "type": "file"}]

    ex = ScanExecutor(scan=fake_scan)
    try:
        out = await ex.run(Path("ws"), "sub")
        assert out == [{"name": "x", "path": "sub", "type": "file"}]
        assert calls == [("ws", "sub")]
    finally:
        await ex.aclose()


async def test_timeout_discards_result():
    started = threading.Event()

    def slow_scan(dir_path: Path, rel_path: str) -> list[dict]:
        started.set()
        time.sleep(0.3)
        return []

    ex = ScanExecutor(scan=slow_scan)
    try:
        with pytest.raises(TimeoutError, match="超时"):
            await ex.run(Path("ws"), "", timeout=0.01)
        assert started.is_set()  # 线程确实启动，只是结果被丢弃
    finally:
        await ex.aclose()  # 等慢线程跑完


async def test_concurrency_bound():
    running = 0
    peak = 0
    lock = threading.Lock()
    started = threading.Event()
    release = threading.Event()

    def gated_scan(dir_path: Path, rel_path: str) -> list[dict]:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        started.set()
        release.wait(timeout=5)
        with lock:
            running -= 1
        return []

    ex = ScanExecutor(max_workers=2, max_pending=0, scan=gated_scan)
    try:
        tasks = [asyncio.create_task(ex.run(Path("ws"), str(i))) for i in range(3)]
        await asyncio.to_thread(started.wait, 5)  # 等首个 scan 启动
        await asyncio.sleep(0.05)  # 给第 2 个也进入；第 3 个应阻塞在信号量
        with lock:
            assert running == 2  # 并发被压到 max_workers=2
        release.set()
        await asyncio.gather(*tasks)
        assert peak <= 2
    finally:
        await ex.aclose()


async def test_aclose_blocks_subsequent_runs():
    ex = ScanExecutor(scan=lambda d, r: [])
    await ex.aclose()
    with pytest.raises(RuntimeError, match="已关闭"):
        await ex.run(Path("ws"), "")
