"""验证 ACP 会话能否跨进程恢复对话上下文（会话恢复方案的核心假设）。

  1. 起 agent，new_session，让它记住一个数字，拿 session_id，关闭（模拟 daemon 重启）。
  2. 起全新 agent 进程，按能力通告 load/resume 同一 session，问它记住的数字。

用法：uv run python scripts/smoke_resume.py [copilot|opencode|claude|codex|zcode]（默认 opencode）
前置：对应 agent 已可用（opencode 需配好 provider；claude 需装 claude-agent-acp
适配器且已登录，见 docs/claude-code-backend.md；codex 需装 codex-acp 适配器且已
codex login；zcode 见 docs/zcode-backend.md）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
CWD = str(Path(__file__).resolve().parent.parent)
SECRET = "4287"
_ZCODE_BRIDGE = os.environ.get("ZCODE_ACP_BRIDGE", "zcode-acp-bridge")
_AGENTS = {
    "copilot": ["copilot", "--acp"],
    "opencode": ["opencode", "acp"],
    "claude": ["claude-agent-acp"],
    "codex": ["codex-acp"],
    "zcode": (
        [sys.executable, _ZCODE_BRIDGE]
        if Path(_ZCODE_BRIDGE).is_file()
        else [_ZCODE_BRIDGE]
    ),
}
# codex-acp 的 bundled codex 在 Windows 常缺原生二进制；用 CODEX_PATH 指向本机全局
# codex（见 scripts/smoke_codex.py 注释）。其它 agent 无需额外 env。
_ZCODE_ENV_KEYS = (
    "ZCODE_BIN",
    "ZCODE_MODEL",
    "ZCODE_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ZCODE_ACP_DEFAULT_MODE",
)
_ENV = {
    "codex": {"CODEX_PATH": os.environ.get("CODEX_PATH", "codex")},
    "zcode": {key: os.environ[key] for key in _ZCODE_ENV_KEYS if key in os.environ},
}


class Collector:
    def __init__(self) -> None:
        self.buf: list[str] = []

    async def __call__(self, text: str) -> None:
        self.buf.append(text)

    def take(self) -> str:
        out = "".join(self.buf)
        self.buf.clear()
        return out


async def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "opencode"
    argv = _AGENTS[name]
    env = _ENV.get(name, {})
    col = Collector()

    a1 = AcpAgent(AgentSpawn(command=argv, cwd=CWD, env=env), col)
    await a1.start()
    sid = a1.session_id
    print(f"=== phase1 session_id = {sid} ===", flush=True)
    await a1.prompt(
        f"Remember this secret number for later: {SECRET}. Acknowledge briefly."
    )
    print(f"[phase1 store] {col.take()!r}", flush=True)
    await a1.aclose()
    print("=== phase1 closed (simulating daemon restart) ===", flush=True)

    a2 = AcpAgent(
        AgentSpawn(command=argv, cwd=CWD, env=env), col, resume_session_id=sid
    )
    await a2.start()
    print(f"=== phase2 resumed session_id = {a2.session_id} ===", flush=True)
    await a2.prompt(
        "What is the secret number I asked you to remember? Reply with just the number."
    )
    recall = col.take()
    print(f"[phase2 recall] {recall!r}", flush=True)
    await a2.aclose()

    ok = SECRET in recall
    print(
        f"\n=== RESULT: context {'SURVIVED ✅' if ok else 'LOST ❌'} across restart ===",
        flush=True,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
