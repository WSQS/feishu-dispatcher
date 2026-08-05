"""命令行入口：feishu-dispatcher start."""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from acp.exceptions import RequestError

logger = logging.getLogger(__name__)


class _AcpMethodNotFoundFilter(logging.Filter):
    """丢掉 ACP task supervisor 对「已回过 -32601 的 client 请求」记的裸
    ``Background task failed`` ERROR（#80）：方法名已由 acp_client 的观察器以
    WARNING 记下，这条 ERROR+traceback 纯属噪声。其它后台任务错误照常保留。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != "Background task failed" or not record.exc_info:
            return True
        exc = record.exc_info[1]
        return not (
            isinstance(exc, RequestError) and getattr(exc, "code", None) == -32601
        )


def _setup_logging(verbose: bool, log_dir: Path) -> None:
    """控制台 + 轮转文件双通道日志；文件落在 config 同目录 ``daemon.log``。

    默认 INFO（调度器工具调用、每轮起止、send 入队等诊断日志都在 INFO，无需 -v，
    也不含密钥）；``-v`` 才到 DEBUG。文件 2MB × 4 份轮转。幂等：重复调用不叠加。
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()  # 幂等：重复调用（如测试）不重复挂 handler
    root.filters.clear()  # 幂等：同上，不重复挂过滤器
    root.setLevel(level)
    root.addFilter(_AcpMethodNotFoundFilter())

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
        from feishu_dispatcher.singleinstance import SingleInstanceLock

        cfg_path = args.config or DEFAULT_CONFIG_PATH
        # 日志文件与会话/任务台账同放 config 目录
        _setup_logging(args.verbose, cfg_path.parent)

        # 单实例锁（#81）：一个状态目录同一时刻只允许一个 daemon，杜绝多实例
        # 共用 tasks.json / WS 互相踩坏台账。持锁进程退出/被杀由 OS 自动释放。
        lock = SingleInstanceLock(cfg_path.parent / "daemon.lock")
        holder = lock.acquire()
        if holder is not None:
            logger.error(
                "已有 daemon 实例在运行（pid %s，锁文件 %s），本次启动中止。"
                "若确认那是僵尸进程，先结束它再重试。",
                holder,
                lock.path,
            )
            raise SystemExit(1)

        try:
            cfg = Config.load(cfg_path, allow_empty_chat_id=args.discover)
            store_path = cfg_path.parent / "sessions.json"
            reboot = asyncio.run(
                run(cfg, discover=args.discover, store_path=store_path)
            )
        finally:
            # re-exec 前显式放锁，让重启起来的新进程能立刻重新获取。
            lock.release()
        if reboot:
            _reexec()


def _reexec() -> None:
    """/reboot：清理已在 run() 的 finally 里跑完，这里用同一 venv python + 同参数
    re-exec 一个全新进程替换自己（PID 不变，无需外部看护）。

    经 ``python -m feishu_dispatcher.cli <原参数>`` 起，绕过 uv 包装但继承其 env
    （VIRTUAL_ENV/PATH 都在），等价于原来的 `uv run feishu-dispatcher start ...`。
    """
    import os
    import sys

    from feishu_dispatcher.daemon import _REBOOTED_ENV

    os.environ[_REBOOTED_ENV] = "1"  # 新进程据此发「已重启」回执
    logger.info(
        "re-exec 重启 daemon：%s -m feishu_dispatcher.cli %s",
        sys.executable,
        sys.argv[1:],
    )
    os.execv(
        sys.executable, [sys.executable, "-m", "feishu_dispatcher.cli", *sys.argv[1:]]
    )


if __name__ == "__main__":
    main()
