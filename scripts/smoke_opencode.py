"""端到端验证：启动 opencode（ACP），发一条 prompt，捕获流式输出。

不经过飞书，只验证 daemon ↔ ACP 的核心链路（与 smoke_acp.py 同，换 agent）。
前置：opencode 侧已配好 provider（`opencode providers`）。
用法：uv run python scripts/smoke_opencode.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


async def main() -> int:
    outputs: list[str] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    spawn = AgentSpawn(
        command=["opencode", "acp"],
        cwd=r"C:\Users\wsqsy\Documents\ai\feishu-dispatcher",
    )
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting opencode acp ===", flush=True)
        await agent.start()
        print(f"=== session={agent.session_id}, sending prompt ===", flush=True)
        await asyncio.wait_for(
            agent.prompt("What is 2+2? Reply with just the number."), timeout=120
        )
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
