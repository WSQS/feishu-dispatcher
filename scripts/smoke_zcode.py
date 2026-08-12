"""端到端验证：启动 ZCode（经 zcode-open-bridge），发一条 prompt，捕获流式输出。

不经过飞书，只验证 daemon ↔ ACP 的核心链路。
前置：
  - 已安装并登录 ZCode，`zcode` 命令可用
  - 已把 zcode-open-bridge 的 `zcode-acp-bridge` 放到 PATH；或设置
    ZCODE_ACP_BRIDGE 指向该源码脚本
用法：uv run python scripts/smoke_zcode.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
_BRIDGE = os.environ.get("ZCODE_ACP_BRIDGE", "zcode-acp-bridge")
_COMMAND = [sys.executable, _BRIDGE] if Path(_BRIDGE).is_file() else [_BRIDGE]
_ENV_KEYS = (
    "ZCODE_BIN",
    "ZCODE_MODEL",
    "ZCODE_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ZCODE_ACP_DEFAULT_MODE",
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


async def main() -> int:
    outputs: list[str] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    agent = AcpAgent(
        AgentSpawn(command=_COMMAND, cwd=REPO_ROOT, env=env),
        on_output,
    )
    try:
        print(f"=== starting {' '.join(_COMMAND)} ===", flush=True)
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
