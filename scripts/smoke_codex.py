"""端到端验证：启动 Codex（经 @agentclientprotocol/codex-acp 适配器），发一条 prompt，捕获流式输出。

不经过飞书，只验证 daemon ↔ ACP 的核心链路（与 smoke_claude.py / smoke_cline.py 同，换 agent）。
Codex CLI 无原生 ACP，走社区适配器 @agentclientprotocol/codex-acp（把 ACP 桥到 codex 内核）。
前置：
  - `npm i -g @agentclientprotocol/codex-acp`（提供 codex-acp 命令）
  - `codex login`（ChatGPT 订阅）或设 OPENAI_API_KEY / CODEX_API_KEY
用法：uv run python scripts/smoke_codex.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

# codex-acp 默认跑它 bundle 的那份 @openai/codex；该 bundle 在 Windows 上常缺
# 平台原生二进制（@openai/codex-win32-x64 可选依赖装不上）。用 CODEX_PATH 让适配器
# 改跑本机全局安装的 codex（`npm i -g @openai/codex`）——适配器对 CODEX_PATH 走
# shell spawn，PATH 上的 `codex` 名即可解析。可用环境变量覆盖。
CODEX_PATH = os.environ.get("CODEX_PATH", "codex")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


async def main() -> int:
    outputs: list[str] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    spawn = AgentSpawn(
        command=["codex-acp"],
        cwd=REPO_ROOT,
        env={"CODEX_PATH": CODEX_PATH},
    )
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting codex-acp ===", flush=True)
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
