"""cli 日志装配测试（诊断日志落盘）。"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from acp.exceptions import RequestError

import feishu_dispatcher.cli as cli_module
import feishu_dispatcher.config as config_module
import feishu_dispatcher.daemon as daemon_module
import feishu_dispatcher.singleinstance as singleinstance_module
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


def test_reexec_sets_reboot_handoff_and_preserves_arguments(monkeypatch):
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_execv(executable: str, argv: list[str]) -> None:
        exec_calls.append((executable, argv))

    monkeypatch.setattr(sys, "executable", "python-test")
    monkeypatch.setattr(sys, "argv", ["feishu-dispatcher", "start", "--discover"])
    monkeypatch.setattr(os, "execv", fake_execv)

    cli_module._reexec()

    assert os.environ[cli_module._REBOOTED_ENV] == "1"
    assert exec_calls == [
        (
            "python-test",
            ["python-test", "-m", "feishu_dispatcher.cli", "start", "--discover"],
        )
    ]


def test_main_consumes_reboot_handoff_and_passes_it_to_daemon(
    monkeypatch, tmp_path: Path
):
    calls: dict[str, object] = {}
    config_path = tmp_path / "config.toml"

    class FakeLock:
        def __init__(self, path: Path) -> None:
            self.path = path

        def acquire(self) -> None:
            return None

        def release(self) -> None:
            calls["released"] = True

    async def fake_run(cfg, **kwargs):
        calls["cfg"] = cfg
        calls["kwargs"] = kwargs
        return False

    monkeypatch.setattr(cli_module, "_setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        config_module.Config,
        "load",
        staticmethod(lambda *_args, **_kwargs: object()),
    )
    monkeypatch.setattr(daemon_module, "run", fake_run)
    monkeypatch.setattr(singleinstance_module, "SingleInstanceLock", FakeLock)
    monkeypatch.setattr(
        sys,
        "argv",
        ["feishu-dispatcher", "start", "--config", str(config_path)],
    )
    monkeypatch.setenv(cli_module._REBOOTED_ENV, "1")

    cli_module.main()

    assert calls["kwargs"] == {
        "discover": False,
        "store_path": config_path.parent / "sessions.json",
        "rebooted": True,
    }
    assert cli_module._REBOOTED_ENV not in os.environ
