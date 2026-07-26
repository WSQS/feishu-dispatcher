"""探针：daemon 注入的环境变量能否透传到 agent 执行的 shell 命令。

这是「CLI + daemon 拥有进程」方案里 task 身份传递的命门：daemon 在启动 agent 时
用 AgentSpawn.env 塞入身份（task_id / token / 控制面 URL），若 opencode 跑 bash
命令时把自己的进程环境透传下去，则我们的 CLI 在那条命令里能直接从 os.environ 读到
身份，agent 完全无需感知自己的 task_id。本探针验证这个透传成立。

用法：uv run python scripts/smoke_env_propagation.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

WORKDIR = r"C:\Users\wsqsy\AppData\Local\Temp\pty-smoke"  # 有现成 opencode.json（flash 模型）
MARKER = "t_probe_9F3X"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

PROMPT = (
    "Using your shell/bash tool, run a command that prints the value of the environment "
    "variable named FEISHU_DISPATCHER_TASK_ID, and report the exact output. "
    "Try `printenv FEISHU_DISPATCHER_TASK_ID`, and if that prints nothing try "
    "`echo $FEISHU_DISPATCHER_TASK_ID` and `echo %FEISHU_DISPATCHER_TASK_ID%`. "
    "Report whatever the command actually printed."
)


async def main() -> int:
    outputs: list[str] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    spawn = AgentSpawn(
        command=["opencode", "acp"],
        cwd=WORKDIR,
        env={
            "FEISHU_DISPATCHER_TASK_ID": MARKER,
            "FEISHU_DISPATCHER_URL": "http://127.0.0.1:54321",
        },
    )
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting opencode acp (with injected env) ===", flush=True)
        await agent.start()
        print(f"=== session={agent.session_id}, sending prompt ===", flush=True)
        await asyncio.wait_for(agent.prompt(PROMPT), timeout=120)
        print("=== prompt round done ===", flush=True)
    except Exception:
        logging.exception("smoke failed")
        return 1
    finally:
        await agent.aclose()

    joined = "".join(outputs)
    hit = MARKER in joined
    print("\n" + "=" * 60, flush=True)
    print(f"=== env 透传到 agent 的 shell 命令? {'YES ✅' if hit else 'NO ❌'} ===", flush=True)
    print(f"    (marker {MARKER!r} {'出现' if hit else '未出现'}在输出里)", flush=True)
    print("=" * 60, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
