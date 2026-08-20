"""有界扫描执行服务：把同步的目录扫描放到受控线程池跑，不占 daemon 主 loop。

旧 tree 接口递归扫描是同步 I/O，经 dispatch marshal 回主 loop 会卡住整个 daemon。
本模块提供一个受控执行器，把扫描放到专用线程池，并带上并发/排队上限、超时与关闭
生命周期。超时或取消只**丢弃结果**（Python 线程不可强杀，线程仍会跑完，结果不投递）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._scanner import scan_children

#: 扫描函数签名（dir_path, rel_path）→ entries；可注入以便测试
ScanFn = Callable[[Path, str], list[dict]]


class ScanExecutor:
    """把同步扫描放到专用线程池跑，带并发/排队上限、超时与关闭生命周期。"""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_pending: int = 16,
        default_timeout: float | None = None,
        scan: ScanFn = scan_children,
    ) -> None:
        self._scan = scan
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="workspace-scan"
        )
        #: 在途（含排队）扫描数上限 = 并发 + 排队；超出则 run() 阻塞等待
        self._slots = asyncio.Semaphore(max_workers + max_pending)
        self._default_timeout = default_timeout

    async def run(
        self, dir_path: Path, rel_path: str, *, timeout: float | None = None
    ) -> list[dict]:
        """在 worker 线程跑一次扫描并返回条目；超时抛 TimeoutError 并丢弃结果。"""
        executor = self._executor
        if executor is None:
            raise RuntimeError("ScanExecutor 已关闭")
        effective = timeout if timeout is not None else self._default_timeout
        await self._slots.acquire()
        try:
            fut = asyncio.get_running_loop().run_in_executor(
                executor, self._scan, dir_path, rel_path
            )
            if effective and effective > 0:
                return await asyncio.wait_for(fut, effective)
            return await fut
        except TimeoutError:
            # 超时只丢结果：线程仍跑完、结果不投递（Python 线程不可强杀）。
            raise TimeoutError(
                f"扫描超时（>{effective:g}s）: {rel_path or '.'}"
            ) from None
        finally:
            self._slots.release()

    async def aclose(self) -> None:
        """关闭线程池，等已提交的扫描跑完；之后 run() 拒绝新任务。"""
        executor = self._executor
        if executor is not None:
            self._executor = None
            executor.shutdown(wait=True)
