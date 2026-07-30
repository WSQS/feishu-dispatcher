"""调度器 LLM 的真实后端（P2）：两种 OpenAI 形态的 client。

- :class:`OpenAICompatClient` —— Chat Completions（``/chat/completions`` + function calling），
  对接 deepseek / GLM / openai 等（``[llm] api = "chat"``，默认）。
- :class:`ResponsesAPIClient` —— OpenAI Responses API（``/responses``），对接只走该接口的
  端点/模型（如公司网关上的 gpt-5.4；``[llm] api = "responses"``）。

两者都实现 :class:`feishu_dispatcher.scheduler.LLMClient` 协议、返回统一的 ``LLMResponse``，
故 ``scheduler.py`` 的工具循环/记忆不感知具体后端。配置见 config 的 ``[llm]`` 段。
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


# --------------------------------------------------------------------------- #
# Responses API：与 Chat Completions 的双向翻译
# --------------------------------------------------------------------------- #


def _cc_tools_to_responses(defs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CC 工具 [{type:function, function:{name,description,parameters}}] → Responses 扁平形态。"""
    out: list[dict[str, Any]] = []
    for d in defs:
        fn = d.get("function") or {}
        out.append(
            {
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters")
                or {"type": "object", "properties": {}},
            }
        )
    return out


def _cc_messages_to_responses(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """CC 风格 messages → (instructions, Responses input items)。

    system → instructions；user/assistant 文本 → role 消息 item；assistant.tool_calls →
    function_call item（带 call_id）；role=tool 的结果 → function_call_output item（同 call_id）。
    call_id 原样往返，保证多轮里 function_call 与其 output 配对（scheduler 用 tc.id 串起）。
    """
    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if content:
                instructions.append(content)
        elif role == "user":
            # 数组 input 里的 role 消息 content 必须是**类型化 parts**（bare 字符串会 500）；
            # 用户/工具文本用 input_text
            items.append(
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": content or ""}],
                }
            )
        elif role == "assistant":
            if content:
                items.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    }
                )
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
        elif role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": m.get("tool_call_id", ""),
                    "output": content or "",
                }
            )
    return "\n\n".join(instructions), items


def _parse_responses(data: dict[str, Any]) -> LLMResponse:
    """Responses 响应 output[] → LLMResponse（message 文本 → content；function_call → ToolCall）。"""
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for item in data.get("output") or []:
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text") and c.get("text"):
                    content_parts.append(c["text"])
        elif itype == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                logger.warning(
                    "Responses 工具参数非合法 JSON: %r", item.get("arguments")
                )
                args = {}
            tool_calls.append(
                ToolCall(
                    id=item.get("call_id", ""),
                    name=item.get("name", ""),
                    arguments=args,
                )
            )
    content = "".join(content_parts) or None
    if content is None and not tool_calls:
        # 回退到便捷字段（部分实现给 output_text 汇总）
        content = data.get("output_text") or None
    return LLMResponse(content=content, tool_calls=tool_calls)


class ResponsesAPIClient:
    """POST {base_url}/responses（OpenAI Responses API）。

    把调度器的 Chat-Completions 形状 messages/tools 翻译成 Responses 的
    instructions/input/tools，再把 output 翻回统一的 ``LLMResponse``——对 scheduler 透明。
    """

    def __init__(self, settings: LLMSettings, *, timeout: float = 90.0) -> None:
        self._url = settings.base_url.rstrip("/") + "/responses"
        self._key = settings.api_key
        self._model = settings.model
        self._timeout = timeout

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        instructions, input_items = _cc_messages_to_responses(messages)
        payload: dict[str, Any] = {"model": self._model, "input": input_items}
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = _cc_tools_to_responses(tools)
            payload["tool_choice"] = "auto"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._key}"},
            )
            resp.raise_for_status()
            data = resp.json()
        return _parse_responses(data)


def build_llm_client(
    settings: LLMSettings | None,
) -> "OpenAICompatClient | ResponsesAPIClient | None":
    """按配置构造 LLM client；未配置返回 None（P2 关闭）。``api`` 决定用哪种形态。"""
    if settings is None:
        return None
    if settings.api == "responses":
        return ResponsesAPIClient(settings)
    return OpenAICompatClient(settings)
