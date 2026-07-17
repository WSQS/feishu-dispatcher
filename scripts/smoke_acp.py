"""端到端验证：启动 copilot（ACP），发一条 prompt，捕获流式输出。

不经过飞书，只验证 daemon ↔ ACP 的核心链路（设计文档 P0 第 1 条）。
用法：uv run python scripts/smoke_acp.py
"""
from __future__ import annotations

import asyncio
import logging
import sys

from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main() -> int:
    outputs: list[str] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    spawn = AgentSpawn(
        command=["copilot", "--acp"],
        cwd=r"C:\Users\wsqsy\Documents\ai\feishu-dispatcher",
    )
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting agent ===", flush=True)
        await agent.start()
        print(f"=== session={agent.session_id}, sending prompt ===", flush=True)
        await agent.prompt("What is 2+2? Reply with just the number.")
        print("=== prompt round done ===", flush=True)
    except Exception:
        logging.exception("smoke failed")
        return 1
    finally:
        await agent.aclose()

    joined = "".join(outputs)
    print(f"\n=== captured {len(outputs)} chunks, total {len(joined)} chars ===", flush=True)
    print(f"=== full output:\n{joined}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
