"""cli 日志装配测试（诊断日志落盘）。"""

from __future__ import annotations

import logging
from pathlib import Path

from acp.exceptions import RequestError

from feishu_dispatcher.cli import _AcpMethodNotFoundFilter, _setup_logging


def _with_restored_root(fn):
    """跑 fn，事后还原 root logger 的 handler/filter/level，避免污染其他测试。"""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_filters = root.filters[:]
    try:
        return fn()
    finally:
        for h in root.handlers:
            if h not in saved_handlers:
                h.close()
        root.handlers[:] = saved_handlers
        root.filters[:] = saved_filters
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
        f1 = len(logging.getLogger().filters)
        _setup_logging(verbose=False, log_dir=tmp_path)
        n2 = len(logging.getLogger().handlers)
        f2 = len(logging.getLogger().filters)
        assert n1 == n2  # 重复调用不叠加 handler
        assert f1 == f2 == 1  # 也不叠加过滤器

    _with_restored_root(body)


def _record(msg, exc):
    record = logging.LogRecord("root", logging.ERROR, __file__, 0, msg, (), None)
    if exc is not None:
        record.exc_info = (type(exc), exc, None)
    return record


def test_acp_filter_drops_handled_method_not_found():
    f = _AcpMethodNotFoundFilter()
    exc = RequestError(-32601, "Method not found", {"method": "cursor/x"})
    assert f.filter(_record("Background task failed", exc)) is False


def test_acp_filter_keeps_other_background_task_errors():
    f = _AcpMethodNotFoundFilter()
    # 非 -32601 的 RequestError 保留（真故障别被吞）
    assert (
        f.filter(
            _record("Background task failed", RequestError(-32603, "Internal error"))
        )
        is True
    )
    # 无 exc_info 的同名日志保留
    assert f.filter(_record("Background task failed", None)) is True
    # 其它消息即便带 -32601 也保留
    assert (
        f.filter(_record("其它错误", RequestError(-32601, "Method not found"))) is True
    )
