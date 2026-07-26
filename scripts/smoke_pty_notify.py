"""探针 v2：opencode-pty 带外回合是否经 ACP 冒出来 + **末尾有没有可靠的回合尾标**。

v1 已确认：PTY 退出触发的 <pty_exited> 带外回合，其输出经 session/update 冒到
on_output（我们没再发 prompt()）。v2 要回答「收尾判定」的关键问题：这轮带外回合
**结束时**，有没有一个稳定、可识别的原始 session/update（比如收尾 usage_update /
current_mode_update / 某个状态变更），能当回合结束信号——还是只能靠静默 debounce。

做法同 v1，但额外在 agent._client_impl 上包一层 session_update，**逐条**打印每个
原始 update 的类型 + 时间戳 + 全量字段（on_output 只给格式化文本，看不到被
_StreamFormatter 有意忽略的 usage_update 等——而尾标八成就在那些里）。

用法：uv run python scripts/smoke_pty_notify.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from feishu_dispatcher import acp_client
from feishu_dispatcher.acp_client import AcpAgent, AgentSpawn

WORKDIR = r"C:\Users\wsqsy\AppData\Local\Temp\pty-smoke"
EXIT_MARKER = "DONE_MARKER_XYZ"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

_PHASE = "init"
_T0 = 0.0
_updates: list[tuple[float, str, str]] = []  # (elapsed, phase, kind)


PROMPT = (
    "You have a tool called pty_spawn from the opencode-pty plugin. "
    "Do exactly this and nothing else:\n"
    "1. Call pty_spawn with these exact arguments:\n"
    '   command="cmd", '
    'args=["/c", "ping -n 11 127.0.0.1 > nul & echo ' + EXIT_MARKER + '"], '
    'description="pty exit notification smoke test", notifyOnExit=true\n'
    "2. After the tool returns, reply with ONLY the returned pty session id and then STOP.\n"
    "Do NOT call pty_read. Do NOT poll. Do NOT sleep-and-read. Just spawn and report the id. "
    "You will later receive a <pty_exited> message automatically; that is expected and you "
    "do not need to act now."
)


def _dump(update: object) -> str:
    """把一个原始 update 压成紧凑单行：优先 pydantic model_dump，回退到属性列举。

    chunk 类只留 type + 文本长度（避免刷屏）；其余类型全量 dump（尾标就藏这里）。
    """
    kind = getattr(update, "session_update", None) or "?"
    if kind in {"agent_message_chunk", "agent_thought_chunk", "user_message_chunk"}:
        content = getattr(update, "content", None)
        text = getattr(content, "text", "") or ""
        return f"{kind} (text_len={len(text)})"
    data: object
    try:
        data = update.model_dump(exclude_none=True)  # type: ignore[attr-defined]
    except Exception:
        data = {
            k: getattr(update, k)
            for k in dir(update)
            if not k.startswith("_") and not callable(getattr(update, k, None))
        }
    s = repr(data)
    if len(s) > 600:
        s = s[:599] + "…"
    return f"{kind} {s}"


def _install_class_patch() -> None:
    """在 _ClientImpl **类**上包一层 session_update（必须在 start() 之前）。

    SDK 在 connect_to_agent 时就绑定了 client 的方法引用，之后改实例属性无效
    （v1→v2 踩过：实例级 patch 被绕过、抓到 0 条）。改类方法、且在 connect 之前
    改，绑定时捕获的就是包装版。
    """
    orig = acp_client._ClientImpl.session_update

    async def logged(self, session_id: str, update: object, **kwargs: object):
        el = round(time.monotonic() - _T0, 2)
        kind = getattr(update, "session_update", None) or "?"
        _updates.append((el, _PHASE, str(kind)))
        print(f"[UPD {el:6.2f}s | {_PHASE:9}] {_dump(update)}", flush=True)
        return await orig(self, session_id, update, **kwargs)

    acp_client._ClientImpl.session_update = logged


async def on_output(text: str) -> None:
    return None  # v2 只关心原始 update；格式化文本不再打印，免得刷屏


async def main() -> int:
    global _PHASE, _T0

    _install_class_patch()  # 必须在 start() 之前
    spawn = AgentSpawn(command=["opencode", "acp"], cwd=WORKDIR)
    agent = AcpAgent(spawn, on_output)
    try:
        print("=== starting opencode acp ===", flush=True)
        _T0 = time.monotonic()
        _PHASE = "startup"
        await agent.start()
        print(f"=== session={agent.session_id}, model={agent.model!r} ===", flush=True)

        _T0 = time.monotonic()  # 重置基准，turn 相对计时更干净
        _PHASE = "turn1"
        print("=== TURN 1: spawn pty w/ notifyOnExit, no polling ===", flush=True)
        stop = await asyncio.wait_for(agent.prompt(PROMPT), timeout=120)
        n_turn1 = len(_updates)
        print(
            f"=== TURN 1 done (stop_reason={stop!r}), {n_turn1} raw updates ===",
            flush=True,
        )

        _PHASE = "idle-wait"
        print("=== IDLE-WAIT 40s: watching raw updates of out-of-band turn ===", flush=True)
        last_idle_seen = 0
        quiet_since: float | None = None
        for i in range(80):
            await asyncio.sleep(0.5)
            n_idle = len(_updates) - n_turn1
            if n_idle != last_idle_seen:
                last_idle_seen = n_idle
                quiet_since = time.monotonic()
            elif quiet_since is not None:
                # 记录「最后一条 update 之后静默了多久」——debounce 参数的参考
                pass
    except Exception:
        logging.exception("smoke failed")
        return 1
    finally:
        await agent.aclose()

    # ---- 分析：原始 update 类型序列 ----
    idle = [(el, k) for (el, ph, k) in _updates if ph == "idle-wait"]
    print("\n" + "=" * 66, flush=True)
    print("=== RESULT: out-of-band turn raw update sequence (idle-wait) ===", flush=True)
    if not idle:
        print("  (none — 带外回合没冒出来)", flush=True)
    else:
        for el, k in idle:
            print(f"  {el:6.2f}s  {k}", flush=True)
        first_el = idle[0][0]
        last_el = idle[-1][0]
        print(f"\n  首条 {first_el}s → 末条 {last_el}s（跨度 {round(last_el - first_el, 2)}s）",
              flush=True)
        print(f"  末条 update 类型 = {idle[-1][1]}", flush=True)
        # 末条之后我们又等了到 40s；若末条远早于 40s，说明其后确实静默
        types = [k for _, k in idle]
        print(f"  出现过的类型: {sorted(set(types))}", flush=True)
    print("=" * 66, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
