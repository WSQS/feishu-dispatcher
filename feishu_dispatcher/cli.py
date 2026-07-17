"""命令行入口：feishu-dispatcher start."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="feishu-dispatcher",
        description="飞书驱动的个人 coding agent 调度器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="启动 daemon（前台运行）")
    start.add_argument(
        "--config",
        type=Path,
        default=None,
        help="配置文件路径（默认 ~/.feishu-dispatcher/config.toml）",
    )
    start.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "start":
        from feishu_dispatcher.config import Config
        from feishu_dispatcher.daemon import run

        run(Config.load(args.config))