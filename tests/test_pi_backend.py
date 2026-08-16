"""pi 后端接入（第一阶段 ACP 适配试点）适配层胶水的单元测试。

覆盖：pi 可执行文件解析（Windows .cmd shim / POSIX / 兜底）、pi-acpinator 环境变量
构建（权限门、pi bin 透传、api key）、AgentSpawn 组装，以及 pi-acpinator ↔ 仓库
``_extract_model`` 的模型 config option 契约（方法映射）。
"""

from __future__ import annotations

import shutil
import sys

from acp.schema import (
    NewSessionResponse,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
)

from feishu_dispatcher.acp_client import (
    AgentSpawn,
    _extract_model,
    _extract_model_options,
)
from feishu_dispatcher.pi_backend import (
    APPROVAL_ENV,
    DEFAULT_APPROVAL,
    PI_BIN_ENV,
    build_pi_agent_spawn,
    pi_acpinator_env,
    resolve_pi_bin,
)


def _fake_which(mapping: dict[str, str | None]):
    return lambda name: mapping.get(name)


# --- resolve_pi_bin ------------------------------------------------------ #


def test_resolve_pi_bin_windows_prefers_cmd(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _fake_which({"pi.cmd": r"C:\npm\pi.cmd"}))
    assert resolve_pi_bin() == r"C:\npm\pi.cmd"


def test_resolve_pi_bin_windows_falls_back_to_bare_pi(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _fake_which({"pi": r"C:\npm\pi"}))
    assert resolve_pi_bin() == r"C:\npm\pi"


def test_resolve_pi_bin_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", _fake_which({"pi": "/usr/local/bin/pi"}))
    assert resolve_pi_bin() == "/usr/local/bin/pi"


def test_resolve_pi_bin_not_found_returns_bare_name(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    assert resolve_pi_bin() == "pi"


# --- pi_acpinator_env ---------------------------------------------------- #


def test_env_always_disables_permission_gate(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    env = pi_acpinator_env()
    assert env[APPROVAL_ENV] == DEFAULT_APPROVAL == "off"


def test_env_sets_pi_bin_when_resolved(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", _fake_which({"pi.cmd": r"C:\npm\pi.cmd"}))
    env = pi_acpinator_env()
    assert env[PI_BIN_ENV] == r"C:\npm\pi.cmd"


def test_env_omits_pi_bin_when_bare(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    env = pi_acpinator_env()
    assert PI_BIN_ENV not in env


def test_env_omits_api_key_when_none(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    env = pi_acpinator_env(api_key=None)
    assert "DEEPSEEK_API_KEY" not in env


def test_env_passes_api_key(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    env = pi_acpinator_env(api_key="sk-test")
    assert env["DEEPSEEK_API_KEY"] == "sk-test"


# --- build_pi_agent_spawn ------------------------------------------------ #


def test_build_pi_agent_spawn(monkeypatch):
    monkeypatch.setattr(shutil, "which", _fake_which({}))
    spawn = build_pi_agent_spawn("/repo", api_key="sk-test")
    assert isinstance(spawn, AgentSpawn)
    assert spawn.command == ["pi-acpinator"]
    assert spawn.cwd == "/repo"
    assert spawn.env["DEEPSEEK_API_KEY"] == "sk-test"
    assert spawn.env[APPROVAL_ENV] == "off"


# --- 模型 config option 契约（pi-acpinator ↔ _extract_model） --------------- #


def test_pi_acpinator_model_option_maps_to_extract_model():
    """钉住契约：pi-acpinator 用 id="model" + ``provider/model_id`` 值暴露模型，
    仓库的 _extract_model/_extract_model_options 据此取当前模型与可切换列表。"""
    resp = NewSessionResponse(
        session_id="sess-1",
        config_options=[
            SessionConfigOptionSelect(
                id="model",
                name="Model",
                type="select",
                current_value="deepseek/deepseek-v4-pro",
                options=[
                    SessionConfigSelectOption(
                        value="deepseek/deepseek-v4-flash", name="DeepSeek V4 Flash"
                    ),
                    SessionConfigSelectOption(
                        value="deepseek/deepseek-v4-pro", name="DeepSeek V4 Pro"
                    ),
                ],
            )
        ],
    )
    assert _extract_model(resp) == "deepseek/deepseek-v4-pro"
    assert _extract_model_options(resp) == [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
    ]
