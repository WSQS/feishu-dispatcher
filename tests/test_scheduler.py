"""调度器 LLM 工具循环的单元测试（用假 LLM，不碰网络）。"""

from __future__ import annotations

from feishu_dispatcher.scheduler import (
    LLMResponse,
    ToolCall,
    build_scheduler_tools,
    run_tool_loop,
)


class FakeLLM:
    """按脚本依次返回预设应答，并记录每次调用。"""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.calls: list[tuple] = []

    async def chat(self, messages, tools) -> LLMResponse:
        self.calls.append((messages, tools))
        return self.script.pop(0)


def _tools(spawn=None, projects=None, agents=None):
    async def _spawn(p, t):
        return f"已派发 {p}"

    return build_scheduler_tools(
        list_projects=lambda: projects or [{"name": "demo"}],
        spawn_agent=spawn or _spawn,
        list_agents=lambda: agents or [],
    )


async def test_returns_text_when_no_tool_calls():
    llm = FakeLLM([LLMResponse(content="你好")])
    assert await run_tool_loop(llm, "hi", []) == "你好"


async def test_executes_tool_then_returns_final():
    spawned = []

    async def spawn(p, t):
        spawned.append((p, t))
        return f"已派发 {p}"

    llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall("1", "list_projects", {})]),
            LLMResponse(
                tool_calls=[
                    ToolCall("2", "spawn_agent", {"project": "demo", "task": "做 X"})
                ]
            ),
            LLMResponse(content="已给 demo 派发：做 X"),
        ]
    )
    out = await run_tool_loop(llm, "帮 demo 做 X", _tools(spawn=spawn))
    assert spawned == [("demo", "做 X")]
    assert out == "已给 demo 派发：做 X"
    assert len(llm.calls) == 3  # list_projects → spawn → 收尾


async def test_unknown_tool_reported_not_crash():
    llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall("1", "nope", {})]),
            LLMResponse(content="ok"),
        ]
    )
    assert await run_tool_loop(llm, "x", []) == "ok"


async def test_tool_error_fed_back_not_raised():
    async def boom(p, t):
        raise RuntimeError("kaboom")

    llm = FakeLLM(
        [
            LLMResponse(
                tool_calls=[ToolCall("1", "spawn_agent", {"project": "p", "task": "t"})]
            ),
            LLMResponse(content="已处理错误"),
        ]
    )
    out = await run_tool_loop(llm, "x", _tools(spawn=boom))
    assert out == "已处理错误"
    # 第二轮 LLM 应看到工具错误结果
    tool_msgs = [m for m in llm.calls[1][0] if m.get("role") == "tool"]
    assert any("kaboom" in m["content"] for m in tool_msgs)


async def test_spawn_agent_validates_missing_args():
    tools = _tools()
    spawn_tool = next(t for t in tools if t.name == "spawn_agent")
    assert "参数不足" in await spawn_tool.handler({"project": "demo"})


async def test_max_iters_cap():
    async def noop(p, t):
        return "ok"

    llm = FakeLLM(
        [
            LLMResponse(tool_calls=[ToolCall(str(i), "list_projects", {})])
            for i in range(10)
        ]
    )
    out = await run_tool_loop(llm, "x", _tools(spawn=noop), max_iters=3)
    assert "步数超限" in out
    assert len(llm.calls) == 3
