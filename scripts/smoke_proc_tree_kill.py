"""探针：Windows 上关掉 agent 时能否把整棵 cmd→powershell→node shim 进程树杀干净（#92）。

不经 ACP/飞书，直接复刻泄漏机理：spawn 一条 `cmd.exe → powershell → powershell`
的三层链（模拟 cursor 的 cmd.cmd→ps1→node），先只 terminate 直接子进程 cmd.exe
（=SDK 现状）验证 powershell 后代确实泄漏，再用 `_win_snapshot_ppids` + `_win_reap_pids`
按 pid 直杀整棵树，验证全部退出。

用法：uv run python scripts/smoke_proc_tree_kill.py
"""

from __future__ import annotations

import asyncio
import sys

from feishu_dispatcher.acp_client import (
    _proc_tree_pids,
    _win_reap_pids,
    _win_snapshot_ppids,
)

# powershell(外, root) → powershell(内, 睡 600s)：两层，terminate 外层不杀内层，
# 复刻 cursor 的 shim 链泄漏（避开 cmd 嵌套引号被 list2cmdline 搅乱的坑）
_INNER_CMD = "& powershell -NoProfile -Command 'Start-Sleep -Seconds 600'"


def _tree(root: int) -> list[int]:
    return _proc_tree_pids(root, _win_snapshot_ppids())


def _alive(pid: int) -> bool:
    return pid in _win_snapshot_ppids()


async def _wait_tree(root: int, want: int, tries: int = 40) -> list[int]:
    """轮询直到进程树至少有 want 个成员（等 shim 链逐层起来）。"""
    tree: list[int] = []
    for _ in range(tries):
        tree = _tree(root)
        if len(tree) >= want:
            return tree
        await asyncio.sleep(0.1)
    return tree


async def main() -> int:
    if sys.platform != "win32":
        print("SKIP: 本探针仅 Windows 有意义（shim 链泄漏是 Windows 特有）")
        return 0

    proc = await asyncio.create_subprocess_exec(
        "powershell",
        "-NoProfile",
        "-Command",
        _INNER_CMD,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    root = proc.pid
    print(f"spawned powershell root pid={root}")

    tree = await _wait_tree(root, want=2)
    print(f"进程树 pid: {tree}")
    if len(tree) < 2:
        print("FAIL: 没等到两层进程树起来（powershell→powershell）")
        _win_reap_pids(tree or [root])
        return 1
    descendants = [p for p in tree if p != root]

    # 1) 复刻 SDK 现状：只 terminate 直接子进程 cmd.exe
    proc.terminate()
    await asyncio.sleep(1.5)
    leaked = [p for p in descendants if _alive(p)]
    print(f"仅 terminate cmd 后仍存活的后代（泄漏）: {leaked}")
    if not leaked:
        print("WARN: 未复现泄漏（本机 shim 链可能自行退出）；仍验证 reaper 幂等")

    # 2) 用兜底 reaper 按 pid 直杀整棵树
    _win_reap_pids(tree)
    for _ in range(40):
        still = [p for p in tree if _alive(p)]
        if not still:
            break
        await asyncio.sleep(0.1)
    still = [p for p in tree if _alive(p)]
    if still:
        print(f"FAIL: reaper 后仍有存活: {still}")
        _win_reap_pids(still)
        return 1

    print("PASS: 整棵进程树已清干净")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
