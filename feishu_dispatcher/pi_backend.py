"""pi 后端接入（第一阶段 ACP 适配试点）：经社区适配器 pi-acpinator 接入 pi。

pi（earendil-works/pi）无原生 ACP，走 Rust 社区适配器 `pi-acpinator`（把 ACP 桥到
`pi --mode rpc`，见 https://github.com/ahmadaccino/pi-acpinator）。本模块只封装
daemon 侧需要的薄胶水：

- 解析 pi 可执行文件路径（Windows 上 npm 全局的 pi 是 ``pi.cmd`` shim，须显式解析）；
- 构建 pi-acpinator 子进程所需的环境变量追加项。

pi 的 JSONL 事件 ↔ ACP ``session_update`` 的映射（framing/方法映射/会话映射/异常路径）
由 pi-acpinator 负责（其自带 ``cargo test`` 覆盖 framing/翻译/coalescing/correlation）。
"""

from __future__ import annotations

import shutil
import sys

from .acp_client import AgentSpawn

#: pi-acpinator 用它覆盖 pi 可执行文件路径（默认 "pi"）。
#: Windows 上 daemon 只透传 SDK 白名单 env，适配器 spawn 的裸 ``pi`` 常解析不到
#: npm shim，故显式给出绝对路径（``resolve_pi_bin``）。
PI_BIN_ENV = "PI_ACPINATOR_PI_BIN"

#: 工具权限门：off / mutating / all。P0 用 off——与 daemon 的 request_permission
#: 自动放行一致，不引入交互确认。
APPROVAL_ENV = "PI_ACPINATOR_APPROVAL"

#: 权限门默认值（不开，P0 自动放行）。
DEFAULT_APPROVAL = "off"


def resolve_pi_bin() -> str:
    """解析 pi 可执行文件路径；找不到返回裸名 ``"pi"``（交给适配器兜底）。

    Windows 上 npm 全局装的 pi 是 ``pi.cmd`` shim。真正原因是 pi-acpinator（Rust
    std spawn）spawn ``pi`` 时不查 PATHEXT（只补 ``.exe``）、解析不到 npm shim；
    daemon 自己的 ``_resolve_executable`` 会给 ACP argv 首词补 ``.cmd``，但管不到
    适配器内部再 spawn 的 ``pi``。故这里显式解析出 ``pi.cmd`` 交给它
    （``PI_ACPINATOR_PI_BIN``）。POSIX 用 ``pi``。
    """
    if sys.platform == "win32":
        for name in ("pi.cmd", "pi"):
            found = shutil.which(name)
            if found:
                return found
    else:
        found = shutil.which("pi")
        if found:
            return found
    return "pi"


def pi_acpinator_env(api_key: str | None = None) -> dict[str, str]:
    """构建 pi-acpinator 子进程的环境变量追加项（``AgentSpawn.env``）。

    - 总是带 ``PI_ACPINATOR_APPROVAL=off``（不开权限门）；
    - pi 可执行文件解析到非裸名时带上 ``PI_ACPINATOR_PI_BIN``；
    - ``api_key`` 非空时作为 ``DEEPSEEK_API_KEY`` 透传（第一阶段试点用 deepseek）。
    """
    env: dict[str, str] = {APPROVAL_ENV: DEFAULT_APPROVAL}
    pi_bin = resolve_pi_bin()
    if pi_bin != "pi":
        env[PI_BIN_ENV] = pi_bin
    if api_key:
        env["DEEPSEEK_API_KEY"] = api_key
    return env


def build_pi_agent_spawn(cwd: str, *, api_key: str | None = None) -> AgentSpawn:
    """构建启动 pi（经 pi-acpinator）的 :class:`AgentSpawn`。

    前置：``npm install -g --ignore-scripts @earendil-works/pi-coding-agent``（pi 命令）
    与 ``cargo install pi-acpinator``（pi-acpinator 命令）。
    """
    return AgentSpawn(
        command=["pi-acpinator"],
        cwd=cwd,
        env=pi_acpinator_env(api_key),
    )
