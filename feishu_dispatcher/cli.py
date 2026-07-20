"""命令行入口：feishu-dispatcher start."""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)


def _setup_logging(verbose: bool, log_dir: Path) -> None:
    """控制台 + 轮转文件双通道日志；文件落在 config 同目录 ``daemon.log``。

    默认 INFO（调度器工具调用、每轮起止、send 入队等诊断日志都在 INFO，无需 -v，
    也不含密钥）；``-v`` 才到 DEBUG。文件 2MB × 4 份轮转。幂等：重复调用不叠加。
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()  # 幂等：重复调用（如测试）不重复挂 handler
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "daemon.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        logger.info("日志写入 %s", log_dir / "daemon.log")
    except Exception:
        logger.warning("无法创建日志文件，仅输出到控制台", exc_info=True)


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

    if args.command == "start":
        import asyncio

        from feishu_dispatcher.config import DEFAULT_CONFIG_PATH, Config
        from feishu_dispatcher.daemon import run

        cfg_path = args.config or DEFAULT_CONFIG_PATH
        # 日志文件与会话/任务台账同放 config 目录
        _setup_logging(args.verbose, cfg_path.parent)
        cfg = Config.load(cfg_path, allow_empty_chat_id=args.discover)
        store_path = cfg_path.parent / "sessions.json"
        asyncio.run(run(cfg, discover=args.discover, store_path=store_path))
