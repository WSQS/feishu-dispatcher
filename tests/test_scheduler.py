"""调度器 LLM 工具循环的单元测试（用假 LLM，不碰网络）。"""

from __future__ import annotations

from pathlib import Path

from feishu_dispatcher.scheduler import (
    LLMResponse,
    SchedulerMemory,
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


def _tools(
    spawn=None,
    projects=None,
    tasks=None,
    get_task=None,
    send=None,
    resume=None,
    done=None,
    register=None,
    unregister=None,
):
    async def _spawn(p, t):
        return f"已派发 {p}"

    async def _send(tid, m):
        return f"已发给 {tid}"

    async def _resume(tid):
        return f"已恢复 {tid}"

    async def _done(tid):
        return f"已完成 {tid}"

    async def _register(name, agent, path):
        return f"已注册 {name}"

    async def _unregister(name):
        return f"已删除 {name}"

    return build_scheduler_tools(
        list_projects=lambda: projects or [{"name": "demo"}],
        spawn_agent=spawn or _spawn,
        list_tasks=lambda: tasks or [],
        get_task=get_task or (lambda tid: None),
        send_to_task=send or _send,
        resume_task=resume or _resume,
        mark_done=done or _done,
        register_project=register or _register,
        unregister_project=unregister or _unregister,
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


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


async def test_send_to_task_validates_and_dispatches():
    sent: list[tuple] = []

    async def send(tid, m):
        sent.append((tid, m))
        return f"已发给 {tid}"

    tools = _tools(send=send)
    st = _tool(tools, "send_to_task")
    assert "参数不足" in await st.handler({"task_id": "t1"})  # 缺 message
    assert "参数不足" in await st.handler({"message": "hi"})  # 缺 task_id
    out = await st.handler({"task_id": "t3", "message": "跑测试"})
    assert out == "已发给 t3"
    assert sent == [("t3", "跑测试")]


async def test_get_task_not_found_and_found():
    detail = {"t3": {"task_id": "t3", "status": "idle"}}
    tools = _tools(get_task=lambda tid: detail.get(tid))
    gt = _tool(tools, "get_task")
    assert "参数不足" in await gt.handler({})
    assert "未找到任务 t9" in await gt.handler({"task_id": "t9"})
    out = await gt.handler({"task_id": "t3"})
    assert '"t3"' in out and "idle" in out


async def test_resume_and_mark_done_validate_and_dispatch():
    tools = _tools()
    resume, done = _tool(tools, "resume_task"), _tool(tools, "mark_done")
    assert "参数不足" in await resume.handler({})
    assert "参数不足" in await done.handler({})
    assert await resume.handler({"task_id": "t2"}) == "已恢复 t2"
    assert await done.handler({"task_id": "t2"}) == "已完成 t2"


async def test_tool_calls_are_logged_for_diagnostics(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="feishu_dispatcher.scheduler")
    sent = []

    async def send(tid, m):
        sent.append((tid, m))
        return f"已发给 {tid}"

    llm = FakeLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        "1", "send_to_task", {"task_id": "t3", "message": "跑测试"}
                    )
                ]
            ),
            LLMResponse(content="已让 t3 跑测试"),
        ]
    )
    await run_tool_loop(llm, "让 t3 跑测试", _tools(send=send))
    # 诊断日志能看出：确实调了 send_to_task、目标 t3、返回什么
    assert "send_to_task" in caplog.text
    assert "t3" in caplog.text


async def test_finish_without_tool_call_is_logged(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="feishu_dispatcher.scheduler")
    llm = FakeLLM([LLMResponse(content="好的，我已经发送了")])  # 只说不做
    await run_tool_loop(llm, "让 t3 跑测试", _tools())
    # 关键诊断信号：LLM 没调任何工具就收尾（「说了没做」）
    assert "无工具调用" in caplog.text


def test_new_tools_are_exposed():
    names = {t.name for t in _tools()}
    assert names == {
        "list_projects",
        "spawn_agent",
        "list_tasks",
        "get_task",
        "send_to_task",
        "resume_task",
        "mark_done",
        "register_project",
        "unregister_project",
    }


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


async def test_history_is_included_in_messages():
    hist = [
        {"role": "user", "content": "上一句"},
        {"role": "assistant", "content": "上一答"},
    ]
    llm = FakeLLM([LLMResponse(content="ok")])
    await run_tool_loop(llm, "这一句", [], history=hist)
    msgs = llm.calls[0][0]
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "上一句"}
    assert msgs[2] == {"role": "assistant", "content": "上一答"}
    assert msgs[-1] == {"role": "user", "content": "这一句"}


def test_memory_in_memory_roundtrip():
    m = SchedulerMemory(None)
    m.add_exchange("q", "a")
    assert m.history() == [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]


def test_memory_persists_and_caps(tmp_path: Path):
    p = tmp_path / "mem.json"
    m = SchedulerMemory(p, max_messages=4)
    m.add_exchange("q1", "a1")
    m.add_exchange("q2", "a2")
    m.add_exchange("q3", "a3")  # 超过 4 条 → 只留最近两对
    reloaded = SchedulerMemory(p, max_messages=4)
    assert reloaded.history() == [
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ]


def test_memory_corrupt_file_tolerated(tmp_path: Path):
    p = tmp_path / "mem.json"
    p.write_text("not json{", encoding="utf-8")
    m = SchedulerMemory(p)  # 不抛
    assert m.history() == []
