# ZCode 后端接入

ZCode 本身不提供 ACP server。本项目通过非官方社区项目
[tizerluo/zcode-open-bridge](https://github.com/tizerluo/zcode-open-bridge) 的
`zcode-acp-bridge` 接入；该 bridge 当前标为 **experimental**，没有随
feishu-dispatcher 一起安装或固定版本。

## 能力与边界

bridge 把 ZCode 私有 stdio 协议翻译成 ACP，支持创建会话、流式文本、工具调用、
取消和会话恢复。它声明的是新版 ACP `session/resume`，而不是旧的
`session/load`；feishu-dispatcher 会按初始化时的能力通告自动选择恢复方法。

2026-08-12 在 Windows 真实安装的 ZCode 3.7.3 / CLI 0.16.1 上验证结果：

| 链路 | 结果 |
|---|---|
| ACP `initialize` | ✅，bridge 声明 `loadSession=false` 和 `sessionCapabilities.resume` |
| `session/new` | ✅ |
| prompt 真流式输出 | ✅，`2+2` 分块返回 `4` |
| 新 bridge 进程执行 `session/resume` | ✅，dispatcher 正确选择新版恢复方法 |
| 恢复后直接 prompt | ❌，上游 bridge 未给冷恢复会话补回 runtime model |
| 恢复后注入正确 runtime model 再 prompt | ✅，能准确回忆前一进程保存的 `4287` |

因此，会话历史本身已经持久化且 `session/resume` 调用有效；当前断点在
`zcode-open-bridge` 与 ZCode 0.16.1 的运行时模型兼容。原版 bridge 恢复后的下一轮
通常报：

```text
历史任务使用的模型已不可用，请从当前模型列表中选择一个可用模型后继续。
```

实验中通过 ZCode 私有的 `session/updateRuntimeModelConfig` 补回 Anthropic provider
后，跨进程上下文恢复完整通过。这个修复应放在 bridge：它负责读取 ZCode 凭据并掌握
私有协议，dispatcher 核心只负责依据标准 ACP capability 选择 `load` 或 `resume`。

当前已知限制：

- bridge 仍是 experimental，ZCode 升级可能破坏私有协议兼容。
- 上游 bridge 当前不能完成 ZCode 0.16.1 的冷恢复后首轮；自动空闲挂起或 daemon 重启
  后继续对话会触发该问题。依赖 ZCode 工作时可临时把全局 `idle_timeout` 设为 `<=0`
  避免自动挂起，但 daemon 重启后仍需 `/run` 新开会话，直到 bridge 修复。
- bridge 没有通过标准 ACP `configOptions` 暴露模型，因此 `/model` 不能列出或切换模型。
- bridge 的 diff 事件目前只有文件名，没有完整 diff 内容。
- 自动测试覆盖 dispatcher 的 ACP 能力选择；真实 CLI 结果仍取决于本机 ZCode 与 bridge
  版本，升级后应重新运行下方冒烟脚本。

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

ZCode 的 Windows 安装可能没有把 `zcode` 放进 PATH。若安装目录只有
`C:\Program Files\ZCode\resources\glm\zcode.cjs`，可自行创建一个 wrapper：

```bat
@echo off
"C:\Program Files\nodejs\node.exe" "C:\Program Files\ZCode\resources\glm\zcode.cjs" %*
```

再把 `ZCODE_BIN` 指向该 `.cmd` 文件。先运行 `zcode.cmd --version`，确认能输出 CLI
版本后再启动 bridge。

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
ZCode 会话能否通过 `session/resume` 恢复上下文。使用 2026-08-12 的上游 bridge
和 ZCode CLI 0.16.1 时，第一个脚本应通过，第二个会暴露上述 runtime model 问题；
若第二个也输出 `context SURVIVED`，说明所安装 bridge 已包含兼容修复。
