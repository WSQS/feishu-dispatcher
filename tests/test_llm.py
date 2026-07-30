"""调度器 LLM client 单测：Chat Completions 与 Responses API 两种形态。

用 httpx.MockTransport 拦截请求（不碰网络）：既断言**请求翻译**（CC messages/tools →
各 API 的 payload），也断言**响应解析**（→ 统一的 LLMResponse）。
"""

from __future__ import annotations

import json

import httpx
import pytest

from feishu_dispatcher import llm
from feishu_dispatcher.config import LLMSettings
from feishu_dispatcher.llm import (
    OpenAICompatClient,
    ResponsesAPIClient,
    build_llm_client,
)


def _mock_httpx(monkeypatch, handler):
    """把 llm 模块里的 httpx.AsyncClient 换成走 MockTransport 的版本。"""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(llm.httpx, "AsyncClient", factory)


def _settings(api: str) -> LLMSettings:
    return LLMSettings(base_url="https://x/v1", api_key="k", model="m", api=api)


# ---- build_llm_client 选择 ---- #


def test_build_llm_client_selects_by_api():
    assert build_llm_client(None) is None
    assert isinstance(build_llm_client(_settings("chat")), OpenAICompatClient)
    assert isinstance(build_llm_client(_settings("responses")), ResponsesAPIClient)


# ---- Chat Completions ---- #


async def test_chat_client_request_and_parse(monkeypatch):
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "hi",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "list_projects",
                                        "arguments": '{"x": 1}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    _mock_httpx(monkeypatch, handler)
    client = OpenAICompatClient(_settings("chat"))
    defs = [{"type": "function", "function": {"name": "list_projects"}}]
    resp = await client.chat([{"role": "user", "content": "hi"}], defs)
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert resp.content == "hi"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].id == "call_1"
    assert resp.tool_calls[0].name == "list_projects"
    assert resp.tool_calls[0].arguments == {"x": 1}


# ---- Responses API：请求翻译 ---- #


async def test_responses_translates_request(monkeypatch):
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, json={"output": []})

    _mock_httpx(monkeypatch, handler)
    client = ResponsesAPIClient(_settings("responses"))
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "列项目"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {"name": "list_projects", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_9", "content": "[]"},
    ]
    defs = [
        {
            "type": "function",
            "function": {
                "name": "list_projects",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    await client.chat(messages, defs)
    b = captured["body"]
    assert captured["url"].endswith("/responses")
    assert b["model"] == "m"
    assert b["instructions"] == "SYS"  # system → instructions
    # user 文本 → input_text parts（bare 字符串会 500）
    assert b["input"][0] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "列项目"}],
    }
    # assistant.tool_calls → function_call item（call_id 原样）
    assert b["input"][1] == {
        "type": "function_call",
        "call_id": "call_9",
        "name": "list_projects",
        "arguments": "{}",
    }
    # tool 结果 → function_call_output（同 call_id）
    assert b["input"][2] == {
        "type": "function_call_output",
        "call_id": "call_9",
        "output": "[]",
    }
    # 工具扁平化：{type:function, name, description, parameters}
    assert b["tools"][0]["type"] == "function"
    assert b["tools"][0]["name"] == "list_projects"
    assert "function" not in b["tools"][0]  # 不再嵌套


# ---- Responses API：响应解析 ---- #


async def test_responses_parses_message(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "你好"}],
                    }
                ]
            },
        )

    _mock_httpx(monkeypatch, handler)
    resp = await ResponsesAPIClient(_settings("responses")).chat(
        [{"role": "user", "content": "hi"}], []
    )
    assert resp.content == "你好"
    assert resp.tool_calls == []


async def test_responses_parses_function_call(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "function_call",
                        "name": "list_projects",
                        "arguments": '{"a": 2}',
                        "call_id": "call_7",
                        "status": "completed",
                    }
                ]
            },
        )

    _mock_httpx(monkeypatch, handler)
    resp = await ResponsesAPIClient(_settings("responses")).chat(
        [{"role": "user", "content": "hi"}], []
    )
    assert resp.content is None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_7" and tc.name == "list_projects" and tc.arguments == {"a": 2}


async def test_responses_falls_back_to_output_text(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": [], "output_text": "兜底文本"})

    _mock_httpx(monkeypatch, handler)
    resp = await ResponsesAPIClient(_settings("responses")).chat(
        [{"role": "user", "content": "hi"}], []
    )
    assert resp.content == "兜底文本"


async def test_responses_raises_on_http_error(monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        await ResponsesAPIClient(_settings("responses")).chat(
            [{"role": "user", "content": "hi"}], []
        )
