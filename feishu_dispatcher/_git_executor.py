"""有界 Git diff-tree 执行服务：把同步 Git 收集放到专用线程池。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ._git_tree import diff_tree_children

GitTreeFn = Callable[[Path, str], list[dict]]


class GitTreeBusyError(RuntimeError):
    """执行器的在途 + 排队容量已满。"""


class GitTreeRequestTimeout(TimeoutError):
    """一次完整 diff-tree 请求超过执行器时限。"""


class GitTreeExecutor:
    """在专用线程池执行 Git diff-tree 收集，并限制并发与排队容量。"""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_pending: int = 8,
        default_timeout: float | None = 20.0,
        collect: GitTreeFn = diff_tree_children,
    ) -> None:
        self._collect = collect
        self._executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="viewer-git-tree",
        )
        self._capacity = max_workers + max_pending
        self._active = 0
        self._default_timeout = default_timeout

    async def run(
        self,
        workspace: Path,
        rel_path: str,
        *,
        timeout: float | None = None,
    ) -> list[dict]:
        """执行一次收集；容量满立即拒绝，超时丢弃迟到结果。"""
        executor = self._executor
        if executor is None:
            raise RuntimeError("GitTreeExecutor 已关闭")
        # run() 只从 daemon 主 loop 调用；check + increment 之间没有 await，故不会 TOCTOU。
        if self._active >= self._capacity:
            raise GitTreeBusyError("Git diff-tree 执行容量已满")
        self._active += 1
        try:
            fut = asyncio.get_running_loop().run_in_executor(
                executor,
                self._collect,
                workspace,
                rel_path,
            )
        except Exception:
            self._active -= 1
            raise
        # shield 防止请求超时/取消把 Future 提前标 done；容量要等真实 worker 退出才释放。
        fut.add_done_callback(lambda _done: setattr(self, "_active", self._active - 1))
        effective = timeout if timeout is not None else self._default_timeout
        if effective and effective > 0:
            try:
                return await asyncio.wait_for(asyncio.shield(fut), effective)
            except TimeoutError:
                raise GitTreeRequestTimeout(
                    f"Git diff-tree 请求超时（>{effective:g}s）: {rel_path or '.'}"
                ) from None
        return await fut

    async def aclose(self) -> None:
        """关闭线程池并等待已提交工作退出；等待放到线程，避免阻塞 daemon 主 loop。"""
        executor = self._executor
        if executor is not None:
            self._executor = None
            await asyncio.to_thread(executor.shutdown, wait=True)
