"""cli 日志装配测试（诊断日志落盘）。"""

from __future__ import annotations

import logging
from pathlib import Path

from feishu_dispatcher.cli import _setup_logging


def _with_restored_root(fn):
    """跑 fn，事后还原 root logger 的 handler/level，避免污染 caplog 等其他测试。"""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        return fn()
    finally:
        for h in root.handlers:
            if h not in saved_handlers:
                h.close()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_setup_logging_writes_to_daemon_log(tmp_path: Path):
    def body():
        _setup_logging(verbose=False, log_dir=tmp_path)
        logging.getLogger("feishu_dispatcher.diag").info("hello-diag-123")
        for h in logging.getLogger().handlers:
            h.flush()
        log = tmp_path / "daemon.log"
        assert log.exists()
        assert "hello-diag-123" in log.read_text(encoding="utf-8")

    _with_restored_root(body)


def test_setup_logging_is_idempotent(tmp_path: Path):
    def body():
        _setup_logging(verbose=False, log_dir=tmp_path)
        n1 = len(logging.getLogger().handlers)
        _setup_logging(verbose=False, log_dir=tmp_path)
        n2 = len(logging.getLogger().handlers)
        assert n1 == n2  # 重复调用不叠加 handler

    _with_restored_root(body)
