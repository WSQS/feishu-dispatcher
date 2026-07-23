"""抓包工具：dump 一个 ACP agent 的 new_session 响应 + session/update 流。

用来排查「agent 通过 ACP 暴露了哪些元数据」——比如**当前模型**（opencode 放在
new_session 响应 config_options 里 id=="model" 的 select 的 current_value；copilot
不暴露）、session modes、可用命令等。不经飞书，只起一个短命 agent 子进程问一句就关。

用法：uv run python scripts/capture_acp_meta.py [opencode|copilot|claude] [cwd]
  cwd 默认仓库根；opencode 的模型/provider 取决于该目录的 opencode 配置。
  连发两轮并 dump 每轮 PromptResponse.usage + 流式 usage_update（token 用量，#53）。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import acp
from acp import text_block
from acp.transports import spawn_stdio_transport

from feishu_dispatcher.acp_client import (
    _PROTOCOL_VERSION,
    _Callbacks,
    _ClientImpl,
    _extract_model,
    _extract_usage_tokens,
    _resolve_executable,
)

_AGENTS = {
    "opencode": ["opencode", "acp"],
    "copilot": ["copilot", "--acp"],
    "claude": ["claude-agent-acp"],
}
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)


def _dump(obj):
    fn = getattr(obj, "model_dump", None)
    if callable(fn):
        try:
            return fn(mode="json", exclude_none=True)
        except TypeError:
            return fn()
    return repr(obj)


class _Capturing(_ClientImpl):
    def __init__(self, cb) -> None:
        super().__init__(cb)
        self.updates: list = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(_dump(update))
        await super().session_update(session_id, update, **kwargs)


async def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "opencode"
    cwd = sys.argv[2] if len(sys.argv) > 2 else _REPO_ROOT
    argv = _AGENTS[name]
    executable = _resolve_executable(argv[0])

    async def _noop(_t: str) -> None:
        pass

    cap = _Capturing(_Callbacks(on_output=_noop))
    ctx = spawn_stdio_transport(executable, *argv[1:], env={}, cwd=cwd)
    reader, writer, proc = await ctx.__aenter__()

    async def _drain():
        s = getattr(proc, "stderr", None)
        if s is None:
            return
        try:
            while await s.readline():
                pass
        except Exception:
            pass

    drain = asyncio.create_task(_drain())
    conn = acp.connect_to_agent(cap, writer, reader)
    try:
        init = await asyncio.wait_for(
            conn.initialize(
                protocol_version=_PROTOCOL_VERSION,
                client_info={"name": "capture", "version": "0"},
            ),
            timeout=60,
        )
        print(f"=== {name} agent_info ===")
        print(json.dumps(_dump(init.agent_info), ensure_ascii=False, indent=2))

        session = await asyncio.wait_for(conn.new_session(cwd=cwd), timeout=60)
        print("\n=== new_session 响应 ===")
        print(json.dumps(_dump(session), ensure_ascii=False, indent=2))
        print(f"\n>>> _extract_model 判定当前模型: {_extract_model(session)!r}")

        # 连发两轮同样的短 prompt：token 用量（#53）里 usage.total_tokens 究竟是
        # 「本轮」还是「跨会话累计」，单轮看不出——两轮对比即知（累计则第二轮更大）。
        for i in (1, 2):
            try:
                resp = await asyncio.wait_for(
                    conn.prompt(
                        session_id=session.session_id,
                        prompt=[text_block("Reply with a single word: hi")],
                    ),
                    timeout=90,
                )
                # PromptResponse.usage 是随响应返回的 UNSTABLE 字段
                print(f"\n=== 第 {i} 轮 prompt 响应（含 usage）===")
                print(json.dumps(_dump(resp), ensure_ascii=False, indent=2))
                print(
                    f">>> _extract_usage_tokens 判定: {_extract_usage_tokens(resp)!r}"
                )
            except Exception as exc:
                print(f"(第 {i} 轮 prompt 异常，忽略: {exc})")

        # 注意 _dump 用字段名（snake_case），不是 alias——按 session_update 计数
        kinds: dict[str, int] = {}
        for u in cap.updates:
            k = u.get("session_update") if isinstance(u, dict) else None
            kinds[str(k)] = kinds.get(str(k), 0) + 1
        print(f"\n=== session/update 变体计数 ===\n{json.dumps(kinds, indent=2)}")
        # 流式 usage_update（当前 context 占用 used/size），若后端上报则 dump 最后一条
        usage_updates = [
            u
            for u in cap.updates
            if isinstance(u, dict) and u.get("session_update") == "usage_update"
        ]
        if usage_updates:
            print("\n=== 最后一条 usage_update ===")
            print(json.dumps(usage_updates[-1], ensure_ascii=False, indent=2))
    finally:
        drain.cancel()
        for close in (conn.close, lambda: ctx.__aexit__(None, None, None)):
            try:
                await close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
