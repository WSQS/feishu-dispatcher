"""端到端验证：启动 Cline（`cline --acp`，原生 ACP 模式），发一条 prompt，捕获流式输出。

不经过飞书，只验证 daemon ↔ ACP 的核心链路（与 smoke_opencode.py / smoke_claude.py 同，换 agent）。
Cline CLI 原生带 `--acp`（`cline --help` 里 "Run in Agent Client Protocol (ACP) mode"），
无需社区适配器。
前置：
  - `npm i -g cline`（提供 cline 命令；Windows 上是 cline.cmd）
  - `cline auth` 已登录某 provider（本机默认 provider=cline、model=cline-free/glm-5.2）
用法：uv run python scripts/smoke_cline.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from feishu_dispatcher.acp_client import AcpAgent, AgentOutputChunk, AgentSpawn

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


async def main() -> int:
    outputs: list[str] = []

    async def on_output(output: AgentOutputChunk) -> None:
        print(f"[OUT] {output.display_text!r}", flush=True)
        outputs.append(output.display_text)

    spawn = AgentSpawn(
        command=["cline", "--acp"],
        cwd=REPO_ROOT,
    )
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting cline --acp ===", flush=True)
        await agent.start()
        print(f"=== session={agent.session_id}, model={agent.model!r} ===", flush=True)
        await asyncio.wait_for(
            agent.prompt("What is 2+2? Reply with just the number."), timeout=180
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
