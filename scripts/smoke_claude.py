"""端到端验证：启动 Claude Code（经 claude-agent-acp 适配器），发一条 prompt，捕获流式输出。

不经过飞书，只验证 daemon ↔ ACP 的核心链路（与 smoke_opencode.py 同，换 agent）。
Claude Code 无原生 ACP，走社区适配器 @agentclientprotocol/claude-agent-acp
（见 docs/claude-code-backend.md）。
前置：
  - `npm i -g @agentclientprotocol/claude-agent-acp`（提供 claude-agent-acp 命令）
  - `claude` 已登录（claude.ai OAuth `claude auth login` 或 ANTHROPIC_API_KEY）
用法：uv run python scripts/smoke_claude.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


async def main() -> int:
    outputs: list[str] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    spawn = AgentSpawn(
        command=["claude-agent-acp"],
        cwd=REPO_ROOT,
    )
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting claude-agent-acp ===", flush=True)
        await agent.start()
        print(f"=== session={agent.session_id}, sending prompt ===", flush=True)
        await asyncio.wait_for(
            agent.prompt("What is 2+2? Reply with just the number."), timeout=120
        )
        print("=== prompt round done ===", flush=True)
        print(f"=== last_message: {agent.last_message!r} ===", flush=True)
    except Exception:
        logging.exception("smoke failed")
        return 1
    finally:
        await agent.aclose()

    joined = "".join(outputs)
    print(
        f"\n=== captured {len(outputs)} chunks, total {len(joined)} chars ===",
        flush=True,
    )
    print(f"=== full output:\n{joined}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
