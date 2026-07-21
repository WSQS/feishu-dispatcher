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


def test_empty_chat_id_raises(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('app_id = "a"\napp_secret = "b"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="discover"):
        Config.load(cfg_file)


def test_empty_chat_id_allowed_in_discover_mode(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('app_id = "a"\napp_secret = "b"\n', encoding="utf-8")
    cfg = Config.load(cfg_file, allow_empty_chat_id=True)
    assert cfg.chat_id == ""


def test_sender_whitelist_and_max_agents_parsed(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n'
        'sender_whitelist = ["ou_a", "ou_b"]\nmax_agents = 5\n',
        encoding="utf-8",
    )
    cfg = Config.load(cfg_file)
    assert cfg.sender_whitelist == ["ou_a", "ou_b"]
    assert cfg.max_agents == 5


def test_llm_memory_rounds_parsed_and_defaults(tmp_path: Path):
    base = 'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n'
    # 显式配置 → 采用
    f1 = tmp_path / "c1.toml"
    f1.write_text(
        base + '[llm]\nbase_url = "u"\napi_key = "k"\nmodel = "m"\nmemory_rounds = 6\n',
        encoding="utf-8",
    )
    cfg = Config.load(f1)
    assert cfg.llm is not None and cfg.llm.memory_rounds == 6
    # 省略 → 默认 12
    f2 = tmp_path / "c2.toml"
    f2.write_text(
        base + '[llm]\nbase_url = "u"\napi_key = "k"\nmodel = "m"\n', encoding="utf-8"
    )
    assert Config.load(f2).llm.memory_rounds == 12


def test_llm_memory_rounds_must_be_positive(tmp_path: Path):
    f = tmp_path / "c.toml"
    f.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n'
        '[llm]\nbase_url = "u"\napi_key = "k"\nmodel = "m"\nmemory_rounds = 0\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="memory_rounds"):
        Config.load(f)


def test_minimal_config(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n', encoding="utf-8"
    )
    cfg = Config.load(cfg_file)
    assert cfg.sender_whitelist == []
    assert cfg.max_agents == 7  # #36：令牌桶就位后默认从 3 提到 7
    assert cfg.feishu_qps == 5.0
    assert cfg.projects == {}
    assert cfg.throttle_window == 0.5
    assert cfg.stream_mode == "card"


def test_feishu_qps_parsed(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\nfeishu_qps = 3.5\n',
        encoding="utf-8",
    )
    assert Config.load(cfg_file).feishu_qps == 3.5


def test_stream_mode_validation(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\nstream_mode = "invalid"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="stream_mode"):
        Config.load(cfg_file)


def test_seed_project_agent_not_configured_warns(tmp_path: Path, caplog):
    # 种子项目仍允许省略/兜底 default_agent（向后兼容），但兜底的 copilot 不在
    # [agents] 里时加载应打 warning（否则 /run 才会失败）。
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n'
        '[agents]\nopencode = ["opencode", "acp"]\n'
        '[[projects]]\nname = "demo"\npath = "C:/work/demo"\n',  # 无 default_agent → 兜底 copilot
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        cfg = Config.load(cfg_file)
    assert cfg.projects["demo"].default_agent == "copilot"  # 兜底仍生效
    assert "copilot" in caplog.text and "demo" in caplog.text
