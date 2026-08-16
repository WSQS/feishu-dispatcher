"""端到端验证：启动 WorkBuddy（CodeBuddy）`codebuddy --acp`（原生 ACP 模式），发一条 prompt，
捕获流式输出，并验证 load_session/close 支持与模型暴露情况。

不经过飞书，只验证 daemon ↔ ACP 的核心链路（与 smoke_codex.py / smoke_cline.py 同，换 agent）。
CodeBuddy Code（国际版，即 WorkBuddy）CLI 原生带 `--acp`（`codebuddy --help` 里
"Start in ACP (Agent Client Protocol) mode"），无需社区适配器。
前置（已配置，免云端登录，codebuddy 自己读文件、无需父进程透传凭据 env）：
  - `npm install -g @tencent-ai/codebuddy-code`（提供 codebuddy 命令；Windows 上是 codebuddy.cmd）
  - `~/.codebuddy/models.json` 配好 DeepSeek（OpenAI 兼容格式，
    url=https://api.deepseek.com/v1/chat/completions、apiKey=${CODEBUDDY_API_KEY}）；
    key 放 `~/.codebuddy/settings.json` 的 `env.CODEBUDDY_API_KEY`（该 env 名同时是
    ACP 免登录的「BYO key」信号，不能叫 DEEPSEEK_API_KEY）。
用法：uv run python scripts/smoke_workbuddy.py
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

_SECRET = "4287"


async def main() -> int:
    outputs: list[str] = []

    async def on_output(text: str) -> None:
        print(f"[OUT] {text!r}", flush=True)
        outputs.append(text)

    # 模型来自 ~/.codebuddy/models.json + settings.json（DeepSeek），codebuddy 自读、免云端登录。
    spawn = AgentSpawn(command=["codebuddy", "--acp"], cwd=REPO_ROOT)

    # --- phase 1：握手 + 流式 + 模型暴露 + close_session ---
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting codebuddy --acp ===", flush=True)
        await agent.start()
        print(f"=== session={agent.session_id}, model={agent.model!r} ===", flush=True)
        print(f"=== available_models={agent.available_models!r} ===", flush=True)
        await asyncio.wait_for(
            agent.prompt("What is 2+2? Reply with just the number."), timeout=180
        )
        print("=== prompt round done ===", flush=True)
        print(f"=== last_message: {agent.last_message!r} ===", flush=True)
        # 存一个数字供 phase 2 验证 load_session 是否真的恢复了上下文
        await asyncio.wait_for(
            agent.prompt(
                f"Remember this secret number for later: {_SECRET}. Acknowledge briefly."
            ),
            timeout=180,
        )
        print("=== secret stored ===", flush=True)
    except Exception:
        logging.exception("smoke failed (phase 1)")
        return 1
    finally:
        sid = agent.session_id
        await agent.aclose()
        print("=== phase 1 closed (close_session 已走) ===", flush=True)

    joined = "".join(outputs)
    print(
        f"\n=== captured {len(outputs)} chunks, total {len(joined)} chars ===",
        flush=True,
    )

    # --- phase 2：load_session 跨进程恢复（session 持久化支持与否） ---
    if not sid:
        print("=== 无 session_id，跳过 load_session 验证 ===", flush=True)
        return 0
    outputs2: list[str] = []

    async def on_output2(text: str) -> None:
        outputs2.append(text)

    a2 = AcpAgent(spawn, on_output2, resume_session_id=sid)
    try:
        print(f"=== resuming session {sid} (load_session) ===", flush=True)
        await a2.start()
        print(f"=== resumed model={a2.model!r} ===", flush=True)
        await asyncio.wait_for(
            a2.prompt(
                "What is the secret number I asked you to remember? Reply with just the number."
            ),
            timeout=180,
        )
        print("=== resume prompt round done ===", flush=True)
    except Exception:
        logging.exception("load_session 验证失败")
        return 1
    finally:
        await a2.aclose()

    recall = "".join(outputs2)
    ok = _SECRET in recall
    print(
        f"\n=== RESULT: load_session {'支持 ✅' if ok else '不支持 ❌'}（recall={recall.strip()!r}） ===",
        flush=True,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
