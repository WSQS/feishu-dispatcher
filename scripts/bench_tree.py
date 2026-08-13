"""测量旧 ``GET /api/projects/{name}/tree`` 的基线。

直接调用 ``viewer.tree`` handler（不经过 daemon / HTTP），量四项：
- 扫描量：逐文件 ``stat`` 次数 + 访问目录数（monkeypatch 统计）
- 端到端耗时
- 响应体大小
- 主循环阻塞：扫描期间同 loop 上的 heartbeat 最大间隙

handler 的函数体是**同步**的（``os.walk`` + 逐文件 ``stat``），而它在 daemon 里经
``dispatch`` marshal 回**主事件循环**执行——所以扫描耗时即主循环被阻塞的时长，
heartbeat 最大间隙 ≈ 扫描耗时，直观反映「慢扫描期间 health/heartbeat 被拖住」。

用法：
  uv run python scripts/gen_tree_dataset.py --files 100000   # 先造数据
  uv run python scripts/bench_tree.py <数据集目录>
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from feishu_dispatcher import viewer


def _run_scan(root: Path):
    """构造 viewer.tree 需要的 ctx/request 并调用它。"""
    ctx = {"all_projects": lambda: {"bench": SimpleNamespace(path=root)}}
    request = {
        "segments": {"name": "bench"},
        "query": {},
        "path": "/api/projects/bench/tree",
    }
    return viewer.tree(ctx, request)


async def _bench(root: Path) -> tuple[int, float, int, int, int, float]:
    """跑一次扫描，返回 (status, 耗时秒, stat 次数, 目录数, 响应字节, heartbeat 最大间隙秒)。"""
    stat_count = {"n": 0}
    walk_dirs = {"n": 0}
    orig_stat = Path.stat
    orig_walk = Path.walk

    def counting_stat(self: Path, *a, **k):
        stat_count["n"] += 1
        return orig_stat(self, *a, **k)

    def counting_walk(self: Path, *a, **k):
        for d, dirs, files in orig_walk(self, *a, **k):
            walk_dirs["n"] += 1
            yield d, dirs, files

    Path.stat = counting_stat  # type: ignore[method-assign]
    Path.walk = counting_walk  # type: ignore[method-assign]

    gaps: list[float] = []

    async def heartbeat() -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(0.001)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    try:
        hb = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)  # 让 heartbeat 先稳定跑起来
        t0 = time.perf_counter()
        status, body = await _run_scan(root)
        dt = time.perf_counter() - t0
        # 先让 heartbeat 醒来一次、把「被扫描阻塞的间隙」记进 gaps，再取消——
        # 否则 cancel 会中断它 suspend 中的 sleep，丢掉最大的那个 gap。
        await asyncio.sleep(0.02)
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        payload = json.dumps(body, ensure_ascii=False)
        return (
            status,
            dt,
            stat_count["n"],
            walk_dirs["n"],
            len(payload.encode("utf-8")),
            max(gaps) if gaps else 0.0,
        )
    finally:
        Path.stat = orig_stat  # type: ignore[method-assign]
        Path.walk = orig_walk  # type: ignore[method-assign]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"数据集目录不存在: {root}", file=sys.stderr)
        return 2
    status, dt, stats, dirs, resp_bytes, max_gap = asyncio.run(_bench(root))
    print(f"数据集: {root}")
    print(f"  HTTP status         = {status}")
    print(f"  扫描耗时            = {dt * 1000:.1f} ms")
    print(f"  访问目录数          = {dirs}")
    print(f"  逐文件 stat 次数    = {stats}")
    print(f"  响应体大小          = {resp_bytes} bytes")
    print(f"  heartbeat 最大间隙  = {max_gap * 1000:.1f} ms（主循环阻塞 ≈ 扫描耗时）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
