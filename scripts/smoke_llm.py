"""验证调度器 LLM（P2）：自然语言 → 理解 → 调工具派发。

从 config.toml 的 [llm] 段读端点（不含密钥硬编码）。用假 spawn/list 回调，
只验证 LLM 的决策链（不真的起 agent / 不碰飞书）。
用法：uv run python scripts/smoke_llm.py ["自然语言需求"]
前置：config.toml 里配好 [llm]。
"""

from __future__ import annotations

import asyncio
import sys

from feishu_dispatcher.config import Config
from feishu_dispatcher.llm import build_llm_client
from feishu_dispatcher.scheduler import build_scheduler_tools, run_tool_loop

PROJECTS = [
    {"name": "feishu-dispatcher", "default_agent": "copilot"},
    {"name": "brick-blast", "default_agent": "opencode"},
]


async def main() -> int:
    cfg = Config.load()
    client = build_llm_client(cfg.llm)
    if client is None:
        print("config.toml 未配置 [llm]，无法测试调度器 LLM。", flush=True)
        return 1

    spawned: list[tuple[str, str]] = []

    async def spawn(project: str, task: str) -> str:
        spawned.append((project, task))
        return f"已在 {project} 启动 agent 处理：{task}"

    tools = build_scheduler_tools(
        list_projects=lambda: PROJECTS,
        spawn_agent=spawn,
        list_agents=lambda: [],
    )

    msg = (
        sys.argv[1]
        if len(sys.argv) > 1
        else "帮 feishu-dispatcher 这个项目加一个深色模式开关"
    )
    print(f"=== user: {msg} ===", flush=True)
    reply = await run_tool_loop(client, msg, tools)
    print(f"\n=== SPAWNED: {spawned} ===", flush=True)
    print(f"=== REPLY: {reply} ===", flush=True)
    return 0 if spawned else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
