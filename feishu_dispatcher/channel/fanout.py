"""把一个流式输出投影到多个 Conversation。"""

from __future__ import annotations

import asyncio
import logging

from ..conversation import ConversationRef
from . import OutputStatus, StreamingOutput

logger = logging.getLogger(__name__)


class FanoutStreamingOutput:
    """把一个 Session 回合的输出投影到多个 Conversation。"""

    def __init__(self, outputs: list[tuple[ConversationRef, StreamingOutput]]) -> None:
        self._outputs = outputs

    def feed(self, text: str) -> None:
        for conversation, output in self._outputs:
            try:
                output.feed(text)
            except Exception:
                logger.exception(
                    "Session 流式输出 feed 失败 conversation=%s",
                    conversation.to_log_string(),
                )

    def set_footer(self, footer: str) -> None:
        for conversation, output in self._outputs:
            try:
                output.set_footer(footer)
            except Exception:
                logger.exception(
                    "Session 流式输出 footer 失败 conversation=%s",
                    conversation.to_log_string(),
                )

    async def flush(self) -> None:
        await self._call_all("flush")

    async def set_status(self, status: OutputStatus) -> None:
        await self._call_all("set_status", status)

    async def aclose(self) -> None:
        await self._call_all("aclose")

    async def _call_all(self, method: str, *args) -> None:
        async def call_one(
            conversation: ConversationRef, output: StreamingOutput
        ) -> None:
            try:
                await getattr(output, method)(*args)
            except Exception:
                logger.exception(
                    "Session 流式输出 %s 失败 conversation=%s",
                    method,
                    conversation.to_log_string(),
                )

        await asyncio.gather(
            *(call_one(conversation, output) for conversation, output in self._outputs)
        )
