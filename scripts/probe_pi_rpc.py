"""一次性探针：直连 ``pi --mode rpc``，记录命令/响应/事件的**真实 JSON 字段名**。

不是冒烟、不经 ACP，只用于核实 pi RPC 协议（适配层映射依据，已据此选择 pi-acpinator）。
- 用 stdin 发命令（JSONL），stdout 收响应/事件（JSONL），LF 为唯一记录分隔。
- 默认用 deepseek 跑（从 daemon config.toml 的 [llm.profiles.deepseek] 读 api_key，
  也可用环境变量 DEEPSEEK_API_KEY 覆盖）；``--no-session`` 避免污染会话目录。

用法（cwd=worktree）：
  C:\\...\\.venv\\Scripts\\python.exe scripts/probe_pi_rpc.py

核实记录（pi v0.84.2，实测字段名）：
- 协议是**自定义 JSONL**（每条一个 ``type`` 字段），**不是** JSON-RPC 2.0。命令带可选
  ``id`` 用于关联；响应 ``{"type":"response","command":...,"success":bool,"data":...}``。
- 命令：``get_state``（data.sessionId / data.model / data.messageCount）、
  ``get_available_models``（data.models[]）、``prompt``（message）、
  ``set_model``（provider + modelId）、``get_last_assistant_text``、``abort``、
  ``new_session``。
- 事件：``agent_start`` / ``agent_end``（messages + willRetry）/ ``agent_settled``（整轮
  结束信号）/ ``turn_start`` / ``turn_end``（message + toolResults）/ ``message_start`` /
  ``message_end`` / ``message_update``（assistantMessageEvent: thinking_start/thinking_delta/
  thinking_end、text_start/text_delta/text_end、toolcall_start/toolcall_delta/toolcall_end）。
- 工具执行：``tool_execution_start`` / ``tool_execution_end``（toolCallId、toolName、args、
  result、isError）——与 ACP 的 tool_call/tool_call_update 同构。
- 会话持久化：默认落盘 ``~/.pi/agent/sessions/``，``--session-id <uuid>`` 可跨进程恢复
  （get_state.messageCount > 0、get_last_assistant_text 返回历史）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


def _resolve_pi() -> str:
    """Windows 上 npm 全局装的 ``pi`` 是 ``pi.cmd`` shim，subprocess 不会自动查 PATHEXT。"""
    for name in ("pi.cmd", "pi"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "找不到 pi 可执行文件（npm i -g @earendil-works/pi-coding-agent）"
    )


def _read_deepseek_key() -> str | None:
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    cfg = Path.home() / ".feishu-dispatcher" / "config.toml"
    if not cfg.exists():
        return None
    text = cfg.read_text(encoding="utf-8")
    # 只取 [llm.profiles.deepseek] 段内的 api_key（段截断到下一个 [ 段头）
    import re

    m = re.search(
        r"\[llm\.profiles\.deepseek\][\s\S]*?api_key\s*=\s*\"([^\"]+)\"", text
    )
    return m.group(1) if m else None


def main() -> int:
    key = _read_deepseek_key()
    if not key:
        print(
            "未找到 DEEPSEEK_API_KEY（config.toml 或环境变量），无法跑真实 prompt。",
            file=sys.stderr,
        )
        return 2

    pi = _resolve_pi()
    argv = [
        pi,
        "--mode",
        "rpc",
        "--provider",
        "deepseek",
        "--model",
        "deepseek-v4-flash",
        "--no-session",
    ]
    env = dict(os.environ)
    env["DEEPSEEK_API_KEY"] = key
    print(f"=== spawn: {argv} ===", flush=True)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )

    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_lines.append(line.rstrip("\n"))

    threading.Thread(target=drain_stderr, daemon=True).start()

    def send(obj: dict) -> None:
        line = json.dumps(obj)
        print(f">>> {line}", flush=True)
        proc.stdin.write(line + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    # 逐行读 stdout，打印每条 JSON 的紧凑形式；遇 agent_settled 停止本轮
    def read_until(types: set[str], timeout: float = 180.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        out: list[dict] = []
        while time.monotonic() < deadline:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                break
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                print(f"<<< (non-json) {line!r}", flush=True)
                continue
            print(f"<<< {json.dumps(evt, ensure_ascii=False)}", flush=True)
            out.append(evt)
            if evt.get("type") in types:
                break
        return out

    try:
        # 1) 初始状态
        print("\n=== [1] get_state ===", flush=True)
        send({"type": "get_state", "id": "p-state"})
        read_until({"response"}, timeout=30)

        # 2) 可用模型
        print("\n=== [2] get_available_models ===", flush=True)
        send({"type": "get_available_models", "id": "p-models"})
        read_until({"response"}, timeout=30)

        # 3) 真实 prompt（观察流式 + 结束信号）
        print("\n=== [3] prompt ===", flush=True)
        send(
            {
                "type": "prompt",
                "id": "p-prompt",
                "message": "Reply with exactly the word OK.",
            }
        )
        read_until({"agent_settled", "turn_end"}, timeout=300)

        # 4) 最后一轮助理文本
        print("\n=== [4] get_last_assistant_text ===", flush=True)
        send({"type": "get_last_assistant_text", "id": "p-last"})
        read_until({"response"}, timeout=30)

        # 5) 切模型 + 再看状态
        print("\n=== [5] set_model ===", flush=True)
        send(
            {
                "type": "set_model",
                "provider": "deepseek",
                "modelId": "deepseek-v4-pro",
                "id": "p-setmodel",
            }
        )
        read_until({"response"}, timeout=30)
        print("\n=== [5b] get_state after set_model ===", flush=True)
        send({"type": "get_state", "id": "p-state2"})
        read_until({"response"}, timeout=30)
    finally:
        # 收尾：abort + 关 stdin
        try:
            send({"type": "abort"})
            time.sleep(0.2)
        except Exception:
            pass
        try:
            proc.stdin.close()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\n=== pi exit code:", proc.returncode, "===", flush=True)
    if stderr_lines:
        print("=== stderr (first 40): ===", flush=True)
        for ln in stderr_lines[:40]:
            print(f"  [stderr] {ln}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
