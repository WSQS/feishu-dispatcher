from pathlib import Path

import pytest

from feishu_dispatcher.config import Config, Project

SAMPLE = """
app_id = "cli_abc"
app_secret = "sec"
chat_id = "oc_123"
throttle_window = 0.3

[agents]
copilot = ["copilot", "--acp"]

[[projects]]
name = "demo"
path = "C:/work/demo"
default_agent = "copilot"

[[projects]]
name = "other"
path = "C:/work/other"
"""


def test_load_full_config(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(SAMPLE, encoding="utf-8")
    cfg = Config.load(cfg_file)
    assert cfg.app_id == "cli_abc"
    assert cfg.app_secret == "sec"
    assert cfg.chat_id == "oc_123"
    assert cfg.throttle_window == 0.3
    assert cfg.agents == {"copilot": ["copilot", "--acp"]}
    assert cfg.projects["demo"] == Project(
        name="demo", path=Path("C:/work/demo"), default_agent="copilot"
    )
    assert cfg.projects["other"].default_agent == "copilot"


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError, match="config.example.toml"):
        Config.load(tmp_path / "nope.toml")


def test_minimal_config(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('app_id = "a"\napp_secret = "b"\n', encoding="utf-8")
    cfg = Config.load(cfg_file)
    assert cfg.chat_id == ""
    assert cfg.projects == {}
    assert cfg.throttle_window == 0.5