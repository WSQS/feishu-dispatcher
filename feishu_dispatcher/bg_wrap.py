"""后台 job 包装进程：跑真实 argv，并把 pid / 退出码落到 state 目录（#89）。

daemon 以脱离方式启动本模块（``python -m feishu_dispatcher.bg_wrap``）。即便 daemon
退出/重启，本进程仍继续；退出时原子写 exit-file，供新 daemon 重新发现并唤回 agent。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ._atomic import atomic_write


def _write_pid(path: str) -> None:
    atomic_write(Path(path), f"{os.getpid()}\n")


def _write_exit(path: str, rc: int) -> None:
    atomic_write(Path(path), f"{rc}\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="feishu_dispatcher.bg_wrap")
    p.add_argument("--pid-file", required=True)
    p.add_argument("--exit-file", required=True)
    p.add_argument("command", nargs=argparse.REMAINDER, help="`--` 后的真实命令 argv")
    args = p.parse_args(argv)
    cmd = list(args.command)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("bg_wrap: 缺少 `-- <command...>`", file=sys.stderr)
        return 2

    _write_pid(args.pid_file)
    # 继承本进程的 stdout/stderr（daemon 已把它们重定向到 job 日志文件）
    try:
        proc = subprocess.Popen(cmd)  # noqa: S603 — argv 来自 daemon，不经 shell
    except OSError as exc:
        print(f"bg_wrap: 启动失败: {exc}", file=sys.stderr)
        _write_exit(args.exit_file, 127)
        return 127
    rc = proc.wait()
    _write_exit(args.exit_file, rc)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
