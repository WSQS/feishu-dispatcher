"""调度器 LLM「大脑」：自然语言 → 理解 → 调用工具派发 agent（P2）。

设计边界（design.md 决策 #10）：轻量 router，只做理解/识别项目/派发/状态查询，
**不写代码、不改文件、不跑命令**——那是底层 agent 的工作。

本模块只含 provider 无关的**工具循环引擎**与**工具定义**：
- :class:`LLMClient` 是最小抽象（OpenAI 兼容的 chat + function calling 形状），
  真实实现（deepseek / GLM / openai 等）在别处注入，测试注入假 client。
- :func:`run_tool_loop` 驱动「LLM 调工具 → 执行 → 结果喂回 → 循环」直到出最终回复。
- :func:`build_scheduler_tools` 把 daemon 能力（列项目 / 派 agent / 查状态）
  包装成工具，daemon 注入实际实现。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个任务调度器。你的职责：
1. 理解用户的任务描述，识别涉及哪些已注册项目（先用 list_projects 查看有哪些项目）。
2. 为每个相关项目调用 spawn_agent 派发任务（task 用清晰的自然语言描述要做什么）。
3. 回答用户关于 agent 状态的问题（list_agents）。

你不写代码、不改文件、不跑命令——那些是 agent 的工作，你只负责理解与派发。
派发后用一两句话简要告诉用户你做了什么。若用户的请求与任何已注册项目都无关，
或信息不足以确定项目/任务，不要 spawn，直接回复澄清或说明。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """一次 LLM 应答：要么给最终文本，要么要求调用若干工具。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    """最小 LLM 抽象（OpenAI 兼容 chat + tools 形状）。"""

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[[dict[str, Any]], Awaitable[str]]  # 参数 -> 结果字符串


def _tool_defs(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


async def run_tool_loop(
    client: LLMClient,
    user_message: str,
    tools: list[ToolSpec],
    *,
    system_prompt: str = SYSTEM_PROMPT,
    max_iters: int = 6,
) -> str:
    """驱动 LLM 工具循环，返回给用户的最终文本。

    每轮：LLM 应答 → 若无工具调用则结束返回其文本；否则执行每个工具、把结果
    作为 ``role=tool`` 消息喂回，继续下一轮。达到 ``max_iters`` 仍未收敛则兜底。
    工具 handler 抛异常不会中断循环——异常文本作为工具结果喂回，让 LLM 自处理。
    """
    by_name = {t.name: t for t in tools}
    defs = _tool_defs(tools)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    for _ in range(max_iters):
        resp = await client.chat(messages, defs)
        if not resp.tool_calls:
            return resp.content or ""
        messages.append(
            {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ],
            }
        )
        for tc in resp.tool_calls:
            spec = by_name.get(tc.name)
            if spec is None:
                result = f"未知工具: {tc.name}"
            else:
                try:
                    result = await spec.handler(tc.arguments)
                except Exception as exc:  # 喂回 LLM，让它决定怎么办
                    logger.exception("调度器工具 %s 执行失败", tc.name)
                    result = f"工具 {tc.name} 执行出错: {exc}"
            messages.append(
                {"role": "tool", "tool_call_id": tc.id, "content": result}
            )
    return "（调度器思考步数超限，请把需求说得更具体，或用 `/run <项目> <任务>` 直接派发。）"


def build_scheduler_tools(
    *,
    list_projects: Callable[[], list[dict[str, Any]]],
    spawn_agent: Callable[[str, str], Awaitable[str]],
    list_agents: Callable[[], list[dict[str, Any]]],
) -> list[ToolSpec]:
    """把 daemon 能力包装成调度器工具。list_* 同步取状态，spawn 异步执行。"""

    async def _list_projects(_args: dict[str, Any]) -> str:
        return json.dumps(list_projects(), ensure_ascii=False)

    async def _spawn_agent(args: dict[str, Any]) -> str:
        project = str(args.get("project", "")).strip()
        task = str(args.get("task", "")).strip()
        if not project or not task:
            return "参数不足：project 和 task 都必填。"
        return await spawn_agent(project, task)

    async def _list_agents(_args: dict[str, Any]) -> str:
        return json.dumps(list_agents(), ensure_ascii=False)

    return [
        ToolSpec(
            name="list_projects",
            description="列出所有已注册项目（含项目名与默认 agent），派发前先了解有哪些项目。",
            parameters={"type": "object", "properties": {}},
            handler=_list_projects,
        ),
        ToolSpec(
            name="spawn_agent",
            description="给指定项目派发一个 coding agent 执行任务，会新建一个飞书话题跟踪其输出。",
            parameters={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "已注册的项目名"},
                    "task": {
                        "type": "string",
                        "description": "要 agent 做什么，清晰的自然语言描述",
                    },
                },
                "required": ["project", "task"],
            },
            handler=_spawn_agent,
        ),
        ToolSpec(
            name="list_agents",
            description="列出当前活跃与可恢复的 agent 及其项目，用于回答状态类问题。",
            parameters={"type": "object", "properties": {}},
            handler=_list_agents,
        ),
    ]
