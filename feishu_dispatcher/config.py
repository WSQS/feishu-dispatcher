"""配置加载。

TOML 配置文件里的 ``[[projects]]`` 是**种子项目**（引导集）；运行时还可经
``/project`` 命令 / ``register_project`` 工具动态注册（落盘 projects.json，见
store.ProjectStore），两者由 daemon 合并成有效项目表。飞书凭据与 agent 启动命令
同样来自配置。
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".feishu-dispatcher" / "config.toml"


@dataclass(frozen=True)
class Project:
    """一个项目（config.toml 种子或运行时注册）。default_agent 种子可省略（兜底
    copilot）；运行时注册强制必填（见 daemon._register_project）。

    ``repo`` 是**可选**的远端仓库 URL（如 ``https://github.com/owner/name``），供调度器
    只读拉取 issue/PR（#56）。留空则 forge 层探测该 ``path`` 下 ``git remote origin``；
    forge 类型按 URL host 推断（github.com → gh，其余 → glab）。见 ``forge.py``。
    """

    name: str
    path: Path
    default_agent: str = "copilot"
    repo: str = ""


@dataclass(frozen=True)
class LLMSettings:
    """调度器 LLM（P2）的 OpenAI 兼容端点配置。未配置则 P2 关闭。"""

    base_url: str
    api_key: str
    model: str
    #: API 形态：``chat``=Chat Completions（``/chat/completions``，默认）；
    #: ``responses``=OpenAI Responses API（``/responses``，某些端点/模型如网关上的
    #: gpt-5.4 只走这个）。见 llm.py 的两个 client 实现。
    api: str = "chat"
    #: 主线对话记忆保留的轮数（透传给 SchedulerMemory.max_turns）；默认 12
    memory_rounds: int = 12


def _parse_llm_profile(pd: dict, *, memory_rounds: int, ctx: str) -> LLMSettings:
    """从一个 profile 表（或 flat ``[llm]``）解析出 :class:`LLMSettings`。

    ``memory_rounds`` 是调度器级共享值（不 per-profile），由调用方从 ``[llm]`` 顶层读入后传入。
    """
    api = str(pd.get("api", "chat")).strip().lower()
    if api not in ("chat", "responses"):
        raise ValueError(f"{ctx}.api 必须为 chat 或 responses，当前为 {api}")
    try:
        return LLMSettings(
            base_url=pd["base_url"],
            api_key=pd["api_key"],
            model=pd["model"],
            api=api,
            memory_rounds=memory_rounds,
        )
    except KeyError as e:
        raise ValueError(f"{ctx} 缺少必填项 {e}") from e


@dataclass(frozen=True)
class Config:
    app_id: str
    app_secret: str
    chat_id: str
    agents: dict[str, list[str]] = field(default_factory=dict)
    #: 后端名 → 追加给该 agent 子进程的环境变量（SDK 白名单之外的显式追加项）。
    #: 用表形式 `[agents.<名>]` 的 `env` 声明——如 codex-acp 需 `CODEX_PATH` 指向本机
    #: 全局 codex（其 bundled codex 在 Windows 常缺原生二进制）。
    agent_env: dict[str, dict[str, str]] = field(default_factory=dict)
    projects: dict[str, Project] = field(default_factory=dict)
    throttle_window: float = 0.5
    #: 发送者 open_id 白名单；空 = 不限制（R10）
    sender_whitelist: list[str] = field(default_factory=list)
    #: 活跃 agent 并发上限（R11）。默认 7——配 feishu_qps 令牌桶（#36）后，多 agent
    #: 高并发的卡片输出会被限流压在飞书同群 QPS 下，故可比早期保守的 3 更高。
    max_agents: int = 7
    #: 出站飞书调用的 QPS 上限（令牌桶，#36）；飞书同群共享 ~5 QPS。<=0 关闭限流。
    feishu_qps: float = 5.0
    #: 空闲多少秒后自动挂起 agent（关进程腾名额，记录保留、回复即恢复）；
    #: <=0 = 不自动挂起。默认 30 分钟，只回收真正被搁置的 agent。
    idle_timeout: float = 1800.0
    #: 后台任务（fdx bg run）默认超时秒数；<=0 = 不超时（默认，长训练/build 不该被砍）。
    #: 兜底防卡死进程堆积；agent 也可用 `fdx bg run --timeout N` 单次覆盖。#68
    bg_job_timeout: float = 0.0
    #: agent 启动/会话恢复（initialize + new/load_session）整体超时秒数（#94）。后端卡在
    #: 握手/load_session 时快速失败（标 failed 可恢复 + 通知）而非永久冻结、静默不回复。
    #: <=0 = 关闭（不建议）。默认 120s，足够冷启动/大会话恢复，又兜住无限卡死。
    agent_start_timeout: float = 120.0
    #: 流式输出模式：card=原地更新卡片（默认），text=每批发新消息（兜底）
    stream_mode: str = "card"
    #: 调度器 LLM（P2）；None = 不启用（自然语言消息回退到「用法」提示）。= 当前激活 profile
    llm: LLMSettings | None = None
    #: 全部 LLM profile（名 → 配置）；flat/单 profile 模式下只有一个（名为 default）。#74
    llm_profiles: dict[str, LLMSettings] = field(default_factory=dict)
    #: 启动时激活的 profile 名（运行时 /llm 可切，不持久化，重启回到此值）
    llm_active: str = ""

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
                repo=str(p.get("repo", "")).strip(),
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
        llm_profiles: dict[str, LLMSettings] = {}
        llm_active = ""
        if llm_data:
            memory_rounds = int(llm_data.get("memory_rounds", 12))
            if memory_rounds < 1:
                raise ValueError(
                    f"llm.memory_rounds 必须为正整数，当前为 {memory_rounds}"
                )
            prof_data = llm_data.get("profiles")
            if prof_data:
                # 多 profile 模式：[llm.profiles.<名>] + [llm].active（#74）
                for name, pd in prof_data.items():
                    llm_profiles[name] = _parse_llm_profile(
                        pd, memory_rounds=memory_rounds, ctx=f"llm.profiles.{name}"
                    )
                if not llm_profiles:
                    raise ValueError("llm.profiles 为空")
                llm_active = str(llm_data.get("active", "")).strip() or next(
                    iter(llm_profiles)
                )
                if llm_active not in llm_profiles:
                    raise ValueError(
                        f"llm.active '{llm_active}' 不在 profiles 里"
                        f"（可选: {', '.join(llm_profiles)}）"
                    )
            else:
                # flat 单 profile（向后兼容）：字段直接写在 [llm] 下
                llm_profiles["default"] = _parse_llm_profile(
                    llm_data, memory_rounds=memory_rounds, ctx="llm"
                )
                llm_active = "default"
            llm = llm_profiles[llm_active]
        # [agents] 每个后端有两种写法：
        #   简写   copilot = ["copilot", "--acp"]          （只有 argv）
        #   表形式 [agents.codex]                          （需要追加 env 的后端）
        #            command = ["codex-acp"]
        #            env = { CODEX_PATH = "codex" }
        agents: dict[str, list[str]] = {}
        agent_env: dict[str, dict[str, str]] = {}
        for name, spec in data.get("agents", {}).items():
            if isinstance(spec, dict):
                cmd = spec.get("command")
                if not isinstance(cmd, list) or not cmd:
                    raise ValueError(
                        f"[agents.{name}] 的 command 必须是非空数组，"
                        f'例如 command = ["codex-acp"]'
                    )
                agents[name] = [str(a) for a in cmd]
                env = spec.get("env")
                if env:
                    if not isinstance(env, dict):
                        raise ValueError(f"[agents.{name}] 的 env 必须是键值表")
                    agent_env[name] = {str(k): str(v) for k, v in env.items()}
            else:
                agents[name] = [str(a) for a in spec]
        # 种子项目的 default_agent 仍可省略（兜底 copilot，向后兼容）；但若兜底或
        # 显式指定的 agent 不在 [agents] 里，/run 时才会失败——加载时先提醒。
        for p in projects.values():
            if p.default_agent not in agents:
                logger.warning(
                    "项目 %s 的 default_agent '%s' 不在 [agents] 配置里，/run 会失败",
                    p.name,
                    p.default_agent,
                )
        return Config(
            app_id=data["app_id"],
            app_secret=data["app_secret"],
            chat_id=chat_id,
            agents=agents,
            agent_env=agent_env,
            projects=projects,
            throttle_window=float(data.get("throttle_window", 0.5)),
            sender_whitelist=list(data.get("sender_whitelist", [])),
            max_agents=int(data.get("max_agents", 7)),
            feishu_qps=float(data.get("feishu_qps", 5.0)),
            idle_timeout=float(data.get("idle_timeout", 1800.0)),
            bg_job_timeout=float(data.get("bg_job_timeout", 0.0)),
            agent_start_timeout=float(data.get("agent_start_timeout", 120.0)),
            stream_mode=stream_mode,
            llm=llm,
            llm_profiles=llm_profiles,
            llm_active=llm_active,
        )
