"""调度器 LLM client 单测：Chat Completions 与 Responses API 两种形态。

用 httpx.MockTransport 拦截请求（不碰网络）：既断言**请求翻译**（CC messages/tools →
各 API 的 payload），也断言**响应解析**（→ 统一的 LLMResponse）。
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from feishu_dispatcher import llm
from feishu_dispatcher.config import LLMSettings
from feishu_dispatcher.llm import (
    OpenAICompatClient,
    ResponsesAPIClient,
    build_llm_client,
    llm_log_context,
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
        captured["headers"] = req.headers
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
    assert captured["headers"]["X-Client-Request-Id"].startswith("llm-")
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


async def test_responses_logs_structured_request(monkeypatch, caplog):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"llm_provider-x-cpa-trace-id": "upstream-123"},
            json={"output": []},
        )

    _mock_httpx(monkeypatch, handler)
    caplog.set_level(logging.INFO, logger="feishu_dispatcher.llm")

    with llm_log_context("项目 Manager[demo]"):
        await ResponsesAPIClient(_settings("responses")).chat(
            [{"role": "user", "content": "不要出现在日志中"}], []
        )

    records = [
        json.loads(record.getMessage().removeprefix("llm_request "))
        for record in caplog.records
        if record.name == "feishu_dispatcher.llm"
        and record.getMessage().startswith("llm_request ")
    ]
    assert len(records) == 2
    start, finish = records
    assert start["event"] == finish["event"] == "llm_request"
    assert start["phase"] == "start"
    assert start["outcome"] == "started"
    assert finish["phase"] == "finish"
    assert start["request_id"] == finish["request_id"]
    assert start["request_id"].startswith("llm-")
    assert finish["daemon_context"] == "项目 Manager[demo]"
    assert finish["model"] == "m"
    assert finish["attempt"] == 1
    assert finish["status"] == 200
    assert finish["outcome"] == "success"
    assert isinstance(finish["elapsed_ms"], int)
    assert finish["elapsed_ms"] >= 0
    assert finish["error_summary"] == ""
    assert finish["upstream_request_id"] == "upstream-123"
    assert "不要出现在日志中" not in caplog.text
    assert "api_key" not in caplog.text


async def test_responses_logs_http_error_without_response_body(monkeypatch, caplog):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"x-request-id": "upstream-error-456"},
            json={"error": "sensitive response body"},
        )

    _mock_httpx(monkeypatch, handler)
    caplog.set_level(logging.INFO, logger="feishu_dispatcher.llm")
    with pytest.raises(httpx.HTTPStatusError):
        with llm_log_context("项目 Manager[demo]"):
            await ResponsesAPIClient(_settings("responses")).chat(
                [{"role": "user", "content": "hi"}], []
            )

    records = [
        json.loads(record.getMessage().removeprefix("llm_request "))
        for record in caplog.records
        if record.name == "feishu_dispatcher.llm"
        and record.getMessage().startswith("llm_request ")
    ]
    assert len(records) == 2
    finish = records[-1]
    assert finish["phase"] == "finish"
    assert finish["request_id"] == records[0]["request_id"]
    assert finish["daemon_context"] == "项目 Manager[demo]"
    assert finish["model"] == "m"
    assert finish["attempt"] == 1
    assert finish["status"] == 500
    assert finish["outcome"] == "failure"
    assert finish["elapsed_ms"] >= 0
    assert finish["error_summary"] == "HTTP 500 Internal Server Error"
    assert finish["upstream_request_id"] == "upstream-error-456"
    assert "sensitive response body" not in caplog.text


async def test_responses_logs_transport_error_type(monkeypatch, caplog):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive timeout detail", request=req)

    _mock_httpx(monkeypatch, handler)
    caplog.set_level(logging.INFO, logger="feishu_dispatcher.llm")
    with pytest.raises(httpx.ReadTimeout):
        await ResponsesAPIClient(_settings("responses")).chat(
            [{"role": "user", "content": "hi"}], []
        )

    records = [
        json.loads(record.getMessage().removeprefix("llm_request "))
        for record in caplog.records
        if record.name == "feishu_dispatcher.llm"
        and record.getMessage().startswith("llm_request ")
    ]
    finish = records[-1]
    assert finish["status"] == "ReadTimeout"
    assert finish["outcome"] == "failure"
    assert finish["error_summary"] == "ReadTimeout"
    assert finish["upstream_request_id"] == ""
    assert "sensitive timeout detail" not in caplog.text


async def test_responses_logs_cancelled_request(monkeypatch, caplog):
    request_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def handler(_req: httpx.Request) -> httpx.Response:
        request_started.set()
        await never_finish.wait()
        return httpx.Response(200, json={"output": []})

    _mock_httpx(monkeypatch, handler)
    caplog.set_level(logging.INFO, logger="feishu_dispatcher.llm")
    task = asyncio.create_task(
        ResponsesAPIClient(_settings("responses")).chat(
            [{"role": "user", "content": "不要出现在取消日志中"}], []
        )
    )
    await request_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    records = [
        json.loads(record.getMessage().removeprefix("llm_request "))
        for record in caplog.records
        if record.name == "feishu_dispatcher.llm"
        and record.getMessage().startswith("llm_request ")
    ]
    assert len(records) == 2
    start, finish = records
    assert finish["phase"] == "finish"
    assert finish["request_id"] == start["request_id"]
    assert finish["status"] == "CancelledError"
    assert finish["outcome"] == "failure"
    assert finish["error_summary"] == "CancelledError"
    assert finish["upstream_request_id"] == ""
    assert "不要出现在取消日志中" not in caplog.text
