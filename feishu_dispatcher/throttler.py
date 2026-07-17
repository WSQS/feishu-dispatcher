"""agent 流式输出的批量节流器。

设计决策 #8：agent 输出全量转发到飞书话题，按 ~500ms 窗口合并，
避免 token 级碎片消息打爆飞书发送 API。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Sink = Callable[[str], Awaitable[None]]


class StreamThrottler:
    """把流式文本片段合并成批量发送。

    第一个未发送片段到达后等待 ``window`` 秒收集后续片段，合并后调用
    ``sink``；单批超过 ``max_chars`` 时按上限切成多次调用。``sink``
    抛异常时该批被丢弃并记录日志，节流循环本身不中断。

    ``feed()`` 是同步方法，可直接在 ACP 通知回调里调用；其余方法需在
    同一个事件循环内 await。
    """

    def __init__(
        self, sink: Sink, *, window: float = 0.5, max_chars: int = 4000
    ) -> None:
        self._sink = sink
        self._window = window
        self._max_chars = max_chars
        self._chunks: list[str] = []
        self._pending = asyncio.Event()
        self._force = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    def feed(self, text: str) -> None:
        """追加一个流式片段。"""
        if self._closed or not text:
            return
        self._chunks.append(text)
        self._pending.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def flush(self) -> None:
        """立即发出已积累的片段（如 agent 回合结束时）。"""
        await self._drain()

    async def aclose(self) -> None:
        """发完剩余片段并停止节流循环。之后的 feed() 会被忽略。"""
        if self._closed:
            return
        self._closed = True
        self._pending.set()
        self._force.set()
        if self._task is not None:
            await self._task
        await self._drain()

    async def _run(self) -> None:
        while not self._closed:
            await self._pending.wait()
            if self._closed:
                break
            try:
                await asyncio.wait_for(self._force.wait(), timeout=self._window)
            except asyncio.TimeoutError:
                pass
            self._force.clear()
            self._pending.clear()
            await self._drain()

    async def _drain(self) -> None:
        async with self._flush_lock:
            if not self._chunks:
                return
            text = "".join(self._chunks)
            self._chunks.clear()
            for start in range(0, len(text), self._max_chars):
                piece = text[start : start + self._max_chars]
                try:
                    await self._sink(piece)
                except Exception:
                    logger.exception("节流器 sink 失败，丢弃 %d 字符", len(piece))
