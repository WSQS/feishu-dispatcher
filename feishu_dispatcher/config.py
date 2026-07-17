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
class Config:
    app_id: str
    app_secret: str
    chat_id: str
    agents: dict[str, list[str]] = field(default_factory=dict)
    projects: dict[str, Project] = field(default_factory=dict)
    throttle_window: float = 0.5

    @staticmethod
    def load(path: Path | None = None) -> Config:
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
        return Config(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            chat_id=data.get("chat_id", ""),
            agents={name: list(argv) for name, argv in data.get("agents", {}).items()},
            projects=projects,
            throttle_window=float(data.get("throttle_window", 0.5)),
        )