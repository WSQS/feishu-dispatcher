"""活卡片：累积一个回合的 agent 输出，节流地 patch 刷新，超限滚动到新卡片。

debounce 结构照 :class:`StreamThrottler`：pending Event + asyncio.wait_for(force, window)
+ flush/aclose。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .card import build_card

if TYPE_CHECKING:
    from .feishu import FeishuBridge

logger = logging.getLogger(__name__)


class LiveCard:
    """一张「活卡片」：累积一个回合的输出，节流地 patch 刷新，超限滚动到新卡片。"""

    _MAX_BODY_BYTES = 25000

    def __init__(
        self,
        bridge: FeishuBridge,
        root_message_id: str,
        title: str,
        *,
        footer: str = "",
        window: float = 0.5,
    ) -> None:
        self._bridge = bridge
        self._root_message_id = root_message_id
        self._title_base = title
        self._title = title
        #: 固定显示在卡片最下方的 note（如「模型：X」）；每次 emit 都带上
        self._footer = footer
        self._window = window
        self._body = ""
        self._status = "running"
        self._card_msg_id: str | None = None
        self._seq = 1
        self._dirty = False
        self._closed = False
        self._pending = asyncio.Event()
        self._force = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    def feed(self, text: str) -> None:
        """同步方法（供 on_output 直接调用，禁止 async）。"""
        if self._closed or not text:
            return
        if self._card_msg_id is not None:
            new_body = self._body + text
            if len(new_body.encode("utf-8")) > self._MAX_BODY_BYTES:
                self._roll()
        self._body += text
        self._dirty = True
        self._pending.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def flush(self) -> None:
        """把当前 body 立即 patch/发出（回合结束、状态变更时用）。"""
        self._force.set()
        await self._drain()

    async def set_status(self, status: str) -> None:
        """改状态并强制 flush（让 ✅/❌/🛑 立即显现）。"""
        self._status = status
        self._dirty = True
        self._force.set()
        await self._drain()

    async def aclose(self) -> None:
        """停 loop + 最后 flush。之后 feed 忽略。"""
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
            if not self._dirty:
                return
            self._dirty = False
            await self._emit()

    async def _emit(self) -> None:
        """dirty 时构造 card 并发送：card_msg_id 为 None 则 reply_card 建卡并存 id；
        否则 patch_card。任何异常只 logger.warning，不得抛出（绝不能拖垮 worker）。"""
        try:
            card = build_card(self._title, self._status, self._body, self._footer)
            if self._card_msg_id is None:
                self._card_msg_id = await asyncio.to_thread(
                    self._bridge.reply_card, self._root_message_id, card
                )
            else:
                await asyncio.to_thread(
                    self._bridge.patch_card, self._card_msg_id, card
                )
        except Exception:
            logger.warning("LiveCard emit 失败", exc_info=True)

    def _roll(self) -> None:
        """滚动到新卡片：旧卡打 footer 并 best-effort patch，重置状态。"""
        old_card_msg_id = self._card_msg_id
        old_title = self._title
        old_body = self._body
        self._body = ""
        self._card_msg_id = None
        self._seq += 1
        if self._seq > 1:
            self._title = f"{self._title_base} ({self._seq})"
        if old_card_msg_id is not None:
            loop = asyncio.get_running_loop()
            loop.create_task(
                self._patch_roll_footer(old_card_msg_id, old_title, old_body)
            )

    async def _patch_roll_footer(self, card_msg_id: str, title: str, body: str) -> None:
        try:
            note = "⤵ 输出接下一条卡片"
            footer = f"{self._footer} · {note}" if self._footer else note
            card = build_card(title, self._status, body, footer)
            await asyncio.to_thread(self._bridge.patch_card, card_msg_id, card)
        except Exception:
            logger.warning("roll footer patch 失败", exc_info=True)
