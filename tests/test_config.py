import tomllib
from pathlib import Path

import pytest

from feishu_dispatcher.config import Config, Project, ViewerConfig

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


def test_llm_api_defaults_to_chat_and_parses_responses(tmp_path: Path):
    base = 'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n'
    # 省略 api → 默认 chat
    f1 = tmp_path / "c1.toml"
    f1.write_text(
        base + '[llm]\nbase_url = "u"\napi_key = "k"\nmodel = "m"\n', encoding="utf-8"
    )
    assert Config.load(f1).llm.api == "chat"
    # 显式 responses（大小写不敏感）
    f2 = tmp_path / "c2.toml"
    f2.write_text(
        base + '[llm]\nbase_url = "u"\napi_key = "k"\nmodel = "m"\napi = "Responses"\n',
        encoding="utf-8",
    )
    assert Config.load(f2).llm.api == "responses"


def test_llm_api_validation(tmp_path: Path):
    f = tmp_path / "c.toml"
    f.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n'
        '[llm]\nbase_url = "u"\napi_key = "k"\nmodel = "m"\napi = "grpc"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="llm.api"):
        Config.load(f)


_BASE = 'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n'


def test_llm_multi_profile_and_active(tmp_path: Path):
    f = tmp_path / "c.toml"
    f.write_text(
        _BASE
        + """
[llm]
active = "gpt5"
memory_rounds = 8

[llm.profiles.deepseek]
base_url = "u1"
api_key = "k1"
model = "deepseek-chat"

[llm.profiles.gpt5]
base_url = "u2"
api_key = "k2"
model = "gpt-5.4"
api = "responses"
""",
        encoding="utf-8",
    )
    cfg = Config.load(f)
    assert set(cfg.llm_profiles) == {"deepseek", "gpt5"}
    assert cfg.llm_active == "gpt5"
    # cfg.llm = 激活的 profile
    assert cfg.llm.model == "gpt-5.4" and cfg.llm.api == "responses"
    assert cfg.llm_profiles["deepseek"].api == "chat"  # 未写 → 默认 chat
    # memory_rounds 是调度器级共享值
    assert cfg.llm.memory_rounds == 8
    assert cfg.llm_profiles["deepseek"].memory_rounds == 8


def test_llm_active_defaults_to_first_profile(tmp_path: Path):
    f = tmp_path / "c.toml"
    f.write_text(
        _BASE
        + '[llm.profiles.deepseek]\nbase_url="u1"\napi_key="k"\nmodel="m1"\n'
        + '[llm.profiles.gpt5]\nbase_url="u2"\napi_key="k"\nmodel="m2"\n',
        encoding="utf-8",
    )
    cfg = Config.load(f)
    assert cfg.llm_active == "deepseek"  # 省略 active → 取第一个（TOML 顺序）


def test_llm_active_unknown_raises(tmp_path: Path):
    f = tmp_path / "c.toml"
    f.write_text(
        _BASE
        + '[llm]\nactive = "nope"\n'
        + '[llm.profiles.deepseek]\nbase_url="u"\napi_key="k"\nmodel="m"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="llm.active"):
        Config.load(f)


def test_llm_flat_mode_becomes_default_profile(tmp_path: Path):
    # flat [llm]（无 profiles）向后兼容 → 单 profile "default"
    f = tmp_path / "c.toml"
    f.write_text(
        _BASE + '[llm]\nbase_url = "u"\napi_key = "k"\nmodel = "m"\n', encoding="utf-8"
    )
    cfg = Config.load(f)
    assert set(cfg.llm_profiles) == {"default"}
    assert cfg.llm_active == "default"
    assert cfg.llm.model == "m"


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
    assert cfg.agent_start_timeout == 120.0  # #94


def test_feishu_qps_parsed(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\nfeishu_qps = 3.5\n',
        encoding="utf-8",
    )
    assert Config.load(cfg_file).feishu_qps == 3.5


def test_agent_start_timeout_parsed(tmp_path: Path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\nagent_start_timeout = 45\n',
        encoding="utf-8",
    )
    assert Config.load(cfg_file).agent_start_timeout == 45.0


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


def _agents_cfg(tmp_path: Path, agents_block: str) -> Config:
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        'app_id = "a"\napp_secret = "b"\nchat_id = "oc_1"\n' + agents_block,
        encoding="utf-8",
    )
    return Config.load(cfg_file)


def test_agent_table_form_parses_command_and_env(tmp_path: Path):
    # 表形式 [agents.<名>]：command=argv + env=追加环境变量（如 codex 的 CODEX_PATH）。
    cfg = _agents_cfg(
        tmp_path,
        '[agents]\ncopilot = ["copilot", "--acp"]\n'
        '[agents.codex]\ncommand = ["codex-acp"]\nenv = { CODEX_PATH = "codex" }\n',
    )
    assert cfg.agents == {"copilot": ["copilot", "--acp"], "codex": ["codex-acp"]}
    assert cfg.agent_env == {"codex": {"CODEX_PATH": "codex"}}


def test_config_example_has_explicit_codex_full_access_profile():
    config_path = Path(__file__).resolve().parents[1] / "config.example.toml"
    with config_path.open("rb") as config_file:
        agents = tomllib.load(config_file)["agents"]

    assert agents["codex"]["env"] == {"CODEX_PATH": "codex"}
    assert agents["codex-full-access"] == {
        "command": ["codex-acp"],
        "env": {
            "CODEX_PATH": "codex",
            "INITIAL_AGENT_MODE": "agent-full-access",
        },
    }


def test_config_example_has_pi():
    config_path = Path(__file__).resolve().parents[1] / "config.example.toml"
    with config_path.open("rb") as config_file:
        agents = tomllib.load(config_file)["agents"]

    assert agents["pi"]["command"] == ["pi-acpinator"]
    env = agents["pi"]["env"]
    assert "PI_ACPINATOR_PI_BIN" in env
    assert "PI_ACPINATOR_APPROVAL" in env


def test_config_example_has_workbuddy():
    # WorkBuddy（CodeBuddy）简写数组形式：`codebuddy --acp`（原生 ACP）；凭据/provider
    # 配在 ~/.codebuddy/models.json + settings.json，daemon 无需注入 env。
    config_path = Path(__file__).resolve().parents[1] / "config.example.toml"
    with config_path.open("rb") as config_file:
        agents = tomllib.load(config_file)["agents"]

    assert agents["workbuddy"] == ["codebuddy", "--acp"]


def test_agent_table_form_without_env_has_no_agent_env(tmp_path: Path):
    # 表形式但没写 env：argv 正常解析，agent_env 不留空条目。
    cfg = _agents_cfg(
        tmp_path,
        '[agents]\n[agents.codex]\ncommand = ["codex-acp"]\n',
    )
    assert cfg.agents == {"codex": ["codex-acp"]}
    assert cfg.agent_env == {}


def test_simple_agent_form_has_empty_agent_env(tmp_path: Path):
    # 简写数组形式向后兼容：无 env、agent_env 为空。
    cfg = _agents_cfg(tmp_path, '[agents]\ncopilot = ["copilot", "--acp"]\n')
    assert cfg.agents == {"copilot": ["copilot", "--acp"]}
    assert cfg.agent_env == {}


def test_agent_table_form_missing_command_raises(tmp_path: Path):
    with pytest.raises(ValueError, match="command"):
        _agents_cfg(
            tmp_path,
            '[agents]\n[agents.codex]\nenv = { CODEX_PATH = "codex" }\n',
        )


def test_viewer_section_parses_full_values(tmp_path: Path):
    # 配置含 [viewer] enabled/bind/port → 正确解析成 ViewerConfig（#116）。
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        _BASE + '[viewer]\nenabled = true\nbind = "127.0.0.1"\nport = 8000\n',
        encoding="utf-8",
    )
    cfg = Config.load(cfg_file)
    assert cfg.viewer == ViewerConfig(enabled=True, bind="127.0.0.1", port=8000)


def test_viewer_absent_means_none(tmp_path: Path):
    # 配置无 [viewer] 段 → Config.viewer is None（viewer 不起，默认安全）。
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(_BASE, encoding="utf-8")
    assert Config.load(cfg_file).viewer is None


def test_viewer_section_defaults_when_fields_omitted(tmp_path: Path):
    # [viewer] 段存在但只写部分字段 → 缺省项用默认值（bind=0.0.0.0 / port=7321）。
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(_BASE + "[viewer]\nenabled = true\n", encoding="utf-8")
    assert Config.load(cfg_file).viewer == ViewerConfig(enabled=True)


def test_viewer_section_empty_table_uses_defaults(tmp_path: Path):
    # 空表 [viewer]（无任何字段）→ 构造默认 ViewerConfig（enabled=False）。
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(_BASE + "[viewer]\n", encoding="utf-8")
    assert Config.load(cfg_file).viewer == ViewerConfig()
