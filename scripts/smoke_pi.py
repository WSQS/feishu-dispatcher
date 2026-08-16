"""端到端验证：启动 pi（经社区适配器 pi-acpinator），走 ACP 全链路。

不经过飞书，只验证 daemon ↔ ACP ↔ pi 的核心链路（与 smoke_codex.py / smoke_opencode.py 同，
换 agent）。pi（earendil-works/pi）无原生 ACP，走 Rust 社区适配器
`pi-acpinator`（把 ACP 桥到 `pi --mode rpc`）。

前置：
  - `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`（提供 pi 命令）
  - `cargo install pi-acpinator`（提供 pi-acpinator 命令；本机有 cargo）
  - 模型凭据：DEEPSEEK_API_KEY（环境变量，或从 ~/.feishu-dispatcher/config.toml 的
    [llm.profiles.deepseek] 读 api_key）
用法：uv run python scripts/smoke_pi.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

from feishu_dispatcher.acp_client import AcpAgent
from feishu_dispatcher.pi_backend import build_pi_agent_spawn

REPO_ROOT = str(Path(__file__).resolve().parent.parent)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


def _read_deepseek_key() -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    cfg = Path.home() / ".feishu-dispatcher" / "config.toml"
    if not cfg.exists():
        return None
    m = re.search(
        r"\[llm\.profiles\.deepseek\][\s\S]*?api_key\s*=\s*\"([^\"]+)\"",
        cfg.read_text(encoding="utf-8"),
    )
    return m.group(1) if m else None


async def main() -> int:
    key = _read_deepseek_key()
    if not key:
        print(
            "未找到 DEEPSEEK_API_KEY（环境变量或 config.toml），无法跑真实 prompt。",
            flush=True,
        )
        return 2
    do_resume = "--resume" in sys.argv[1:]

    outputs: list[str] = []
    actions: list[dict] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    async def on_action(action: dict) -> None:
        print(f"[ACTION] {action!r}", flush=True)
        actions.append(action)

    spawn = build_pi_agent_spawn(REPO_ROOT, api_key=key)
    agent = AcpAgent(spawn, on_output, on_action=on_action)
    session_id = None
    try:
        print("=== starting pi-acpinator ===", flush=True)
        await agent.start()
        session_id = agent.session_id
        print(
            f"=== session={session_id}, model={agent.model!r}, "
            f"available_models={agent.available_models!r} ===",
            flush=True,
        )

        print("\n=== round 1: 流式文本 ===", flush=True)
        stop1 = await asyncio.wait_for(
            agent.prompt("What is 2+2? Reply with just the number."), timeout=180
        )
        print(f"=== round 1 stop_reason={stop1!r} ===", flush=True)
        print(f"=== round 1 last_message: {agent.last_message!r} ===", flush=True)

        print("\n=== round 2: 触发工具调用（read 工具，不依赖 bash） ===", flush=True)
        stop2 = await asyncio.wait_for(
            agent.prompt(
                "Use the read tool to read README.md and report its first line only."
            ),
            timeout=180,
        )
        print(f"=== round 2 stop_reason={stop2!r} ===", flush=True)
        print(f"=== round 2 last_message: {agent.last_message!r} ===", flush=True)
    except Exception:
        logging.exception("smoke failed")
        return 1
    finally:
        await agent.aclose()

    resume_ok = True
    if do_resume and session_id:
        print(
            f"\n=== resume phase: load_session({session_id}) 跨进程恢复 ===", flush=True
        )
        resumed: list[str] = []

        async def on_resumed(text: str) -> None:
            print(f"[RESUME] {text!r}", flush=True)
            resumed.append(text)

        agent2 = AcpAgent(
            build_pi_agent_spawn(REPO_ROOT, api_key=key),
            on_resumed,
            resume_session_id=session_id,
        )
        try:
            await agent2.start()
            await asyncio.wait_for(
                agent2.prompt(
                    "What number did I ask you to add in the first round? "
                    "Reply with just the number."
                ),
                timeout=180,
            )
        except Exception:
            logging.exception("resume phase failed")
            resume_ok = False
        finally:
            await agent2.aclose()
        recalled = "".join(resumed)
        print(f"=== resume recalled: {recalled!r} ===", flush=True)

    joined = "".join(outputs)
    print(
        f"\n=== captured {len(outputs)} chunks, total {len(joined)} chars ===",
        flush=True,
    )
    print(f"=== tool actions seen: {actions!r} ===", flush=True)
    print(f"=== full output:\n{joined}", flush=True)
    if do_resume:
        print(
            f"=== RESULT: resume {'OK ✅' if resume_ok else 'FAILED ❌'} ===",
            flush=True,
        )
        return 0 if resume_ok else 3
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
