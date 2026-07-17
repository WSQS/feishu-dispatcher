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
    start.add_argument(
        "--discover",
        action="store_true",
        help="发现模式：允许 chat_id 为空，只打印收到消息的 chat_id，不执行命令",
    )
    start.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "start":
        import asyncio

        from feishu_dispatcher.config import Config
        from feishu_dispatcher.daemon import run

        cfg = Config.load(args.config, allow_empty_chat_id=args.discover)
        asyncio.run(run(cfg, discover=args.discover))
