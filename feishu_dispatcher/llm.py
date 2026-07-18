"""OpenAI 兼容的 LLM client（调度器 LLM 的真实后端，P2）。

对接任何 OpenAI 兼容的 chat-completions + function-calling 端点
（deepseek / GLM回归 / openai 等），配置见 config 的 ``[llm]`` 段。
实现 :class:`feishu_dispatcher.scheduler.LLMClient` 协议。
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import LLMSettings
from .scheduler import LLMResponse, ToolCall

logger = logging.getLogger(__name__)


class OpenAICompatClient:
    """POST {base_url}/chat/completions，解析 message.content + tool_calls。"""

    def __init__(self, settings: LLMSettings, *, timeout: float = 90.0) -> None:
        self._url = settings.base_url.rstrip("/") + "/chat/completions"
        self._key = settings.api_key
        self._model = settings.model
        self._timeout = timeout

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        msg = data["choices"][0]["message"]
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                logger.warning("工具参数非合法 JSON: %r", fn.get("arguments"))
                args = {}
            tool_calls.append(
                ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args)
            )
        return LLMResponse(content=msg.get("content"), tool_calls=tool_calls)


def build_llm_client(settings: LLMSettings | None) -> OpenAICompatClient | None:
    """按配置构造 LLM client；未配置返回 None（P2 关闭）。"""
    if settings is None:
        return None
    return OpenAICompatClient(settings)
