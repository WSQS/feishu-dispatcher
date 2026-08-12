# ZCode 后端接入

ZCode 本身不提供 ACP server。本项目通过非官方社区项目
[tizerluo/zcode-open-bridge](https://github.com/tizerluo/zcode-open-bridge) 的
`zcode-acp-bridge` 接入；该 bridge 当前标为 **experimental**，没有随
feishu-dispatcher 一起安装或固定版本。

## 能力与边界

bridge 把 ZCode 私有 stdio 协议翻译成 ACP，支持创建会话、流式文本、工具调用、
取消和会话恢复。它声明的是新版 ACP `session/resume`，而不是旧的
`session/load`；feishu-dispatcher 会按初始化时的能力通告自动选择恢复方法，
所以空闲挂起或 daemon 重启后仍可接回原会话。

当前已知限制：

- bridge 仍是 experimental，ZCode 升级可能破坏私有协议兼容。
- bridge 没有通过标准 ACP `configOptions` 暴露模型，因此 `/model` 不能列出或切换模型。
- bridge 的 diff 事件目前只有文件名，没有完整 diff 内容。
- 本仓环境没有安装 ZCode，自动测试覆盖了 ACP 能力选择；真实 CLI 的握手、流式和恢复需在
  安装 ZCode 的机器上运行下方冒烟脚本。

## 安装

1. 安装 Node.js 18+ 和 ZCode，并完成登录；确认 `zcode` 命令能启动其
   headless app-server。
2. 获取 `zcode-open-bridge`，把
   `packages/acp-bridge/zcode-acp-bridge` 放到 PATH，或在配置里写它的绝对路径。
   bridge 是一个只依赖 Python 标准库的脚本。
3. 配置后端：

```toml
[agents.zcode]
command = ["zcode-acp-bridge"]
env = { ZCODE_ACP_DEFAULT_MODE = "yolo" }
```

如果 Windows 没有可直接执行的同名 wrapper，可显式用 Python 启动源码脚本：

```toml
[agents.zcode]
command = [
  "C:/path/to/python.exe",
  "C:/tools/zcode-open-bridge/packages/acp-bridge/zcode-acp-bridge",
]
env = {
  ZCODE_BIN = "zcode",
  ZCODE_ACP_DEFAULT_MODE = "yolo",
}
```

`ZCODE_BIN` 可改成实际的 ZCode CLI 路径。若 bridge 脚本已包装成
`zcode-acp-bridge.cmd`，第一种写法即可，daemon 会在 Windows 自动解析 `.cmd`。

## 权限风险

上游 bridge 默认 `ZCODE_ACP_DEFAULT_MODE=yolo`，意味着 ZCode 的文件修改和命令执行
不会再请求确认。feishu-dispatcher 本就是远程派活工具，启用前至少应：

- 配置 `sender_whitelist`，只允许可信飞书账号派活。
- 只登记允许 agent 修改的项目目录。
- 先在测试仓库跑冒烟，再用于重要仓库。

可把模式改成 `build` 收紧权限，但旧版 ZCode 可能因 bridge 尚未转发权限请求而卡住；
改后应实际验证一次包含文件读取和命令执行的任务。

## 验证

bridge 已在 PATH 时：

```powershell
uv run python scripts/smoke_zcode.py
uv run python scripts/smoke_resume.py zcode
```

若直接使用源码脚本，先设置其路径：

```powershell
$env:ZCODE_ACP_BRIDGE = "C:\tools\zcode-open-bridge\packages\acp-bridge\zcode-acp-bridge"
uv run python scripts/smoke_zcode.py
uv run python scripts/smoke_resume.py zcode
```

第一个脚本验证握手和流式输出；第二个脚本关闭 bridge 进程后重新启动，验证同一
ZCode 会话能通过 `session/resume` 恢复上下文。
