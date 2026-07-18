"""配置加载。

P0 原型范围：项目列表硬编码在 TOML 配置文件里（设计文档 P2 才做自注册），
飞书凭据与 agent 启动命令同样来自配置。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".feishu-dispatcher" / "config.toml"


@dataclass(frozen=True)
class Project:
    """已注册项目（P0 硬编码于配置文件）。"""

    name: str
    path: Path
    default_agent: str = "copilot"


@dataclass(frozen=True)
class LLMSettings:
    """调度器 LLM（P2）的 OpenAI 兼容端点配置。未配置则 P2 关闭。"""

    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    chat_id: str
    agents: dict[str, list[str]] = field(default_factory=dict)
    projects: dict[str, Project] = field(default_factory=dict)
    throttle_window: float = 0.5
    #: 发送者 open_id 白名单；空 = 不限制（R10）
    sender_whitelist: list[str] = field(default_factory=list)
    #: 活跃 agent 并发上限（R11）
    max_agents: int = 3
    #: 空闲多少秒后自动挂起 agent（关进程腾名额，记录保留、回复即恢复）；
    #: <=0 = 不自动挂起。默认 30 分钟，只回收真正被搁置的 agent。
    idle_timeout: float = 1800.0
    #: 流式输出模式：card=原地更新卡片（默认），text=每批发新消息（兜底）
    stream_mode: str = "card"
    #: 调度器 LLM（P2）；None = 不启用（自然语言消息回退到「用法」提示）
    llm: LLMSettings | None = None

    @staticmethod
    def load(path: Path | None = None, *, allow_empty_chat_id: bool = False) -> Config:
        cfg_path = path or DEFAULT_CONFIG_PATH
        if not cfg_path.exists():
            raise FileNotFoundError(
                f"配置文件不存在: {cfg_path}。请复制仓库根目录的 config.example.toml 并填写。"
            )
        data = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        projects = {
            p["name"]: Project(
                name=p["name"],
                path=Path(p["path"]),
                default_agent=p.get("default_agent", "copilot"),
            )
            for p in data.get("projects", [])
        }
        chat_id = data.get("chat_id", "")
        # R10：chat_id 必填（空则拒绝启动）；只有 discover 模式允许空
        if not chat_id and not allow_empty_chat_id:
            raise ValueError(
                "配置 chat_id 不能为空。用 `feishu-dispatcher start --discover` "
                "可在日志里看到收到消息的 chat_id 来发现群 id。"
            )
        stream_mode = data.get("stream_mode", "card")
        if stream_mode not in ("card", "text"):
            raise ValueError(f"stream_mode 必须为 card 或 text，当前为 {stream_mode}")
        llm_data = data.get("llm")
        llm = None
        if llm_data:
            llm = LLMSettings(
                base_url=llm_data["base_url"],
                api_key=llm_data["api_key"],
                model=llm_data["model"],
            )
        return Config(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            chat_id=chat_id,
            agents={name: list(argv) for name, argv in data.get("agents", {}).items()},
            projects=projects,
            throttle_window=float(data.get("throttle_window", 0.5)),
            sender_whitelist=list(data.get("sender_whitelist", [])),
            max_agents=int(data.get("max_agents", 3)),
            idle_timeout=float(data.get("idle_timeout", 1800.0)),
            stream_mode=stream_mode,
            llm=llm,
        )
