# Claude Code 作为第三种 ACP 后端

**结论**：可以接入，daemon 侧**零代码改动**——只是加一条 `[agents]` 配置。但 Claude Code
**没有原生 ACP**，必须经一个社区适配器把它包成 ACP agent server。本机（2026-07-20，
`claude` 2.1.215，claude.ai Max 已登录）已用本项目的冒烟脚本**实测握手 / 流式 / load_session /
权限+工具执行全部通过**。

## 一、原生还是适配器？—— 适配器

`claude --help` 没有任何 ACP 子命令/flag（只有 `mcp` / `agents` / `auth` / `gateway` 等；
`claude mcp` 是把 Claude Code 当 **MCP client**，不是 ACP server）。社区/官方也确认 Claude Code
本体不暴露 ACP（[anthropics/claude-code#6686](https://github.com/anthropics/claude-code/issues/6686)
仍是 open feature request）。

接入走 **Zed 维护的官方适配器**，它用 `@anthropic-ai/claude-agent-sdk`（TypeScript SDK，内部即
Claude Code 引擎）实现一个 ACP agent server：

- **当前包名**：`@agentclientprotocol/claude-agent-acp`（bin = `claude-agent-acp`，实测 v0.59.0，
  依赖 `@anthropic-ai/claude-agent-sdk` 0.3.207 + `@agentclientprotocol/sdk` 1.2.1）。
- **旧包名**：`@zed-industries/claude-code-acp`（bin = `claude-code-acp`，停更在 0.16.2）。GitHub
  仓库 `zed-industries/claude-code-acp` 已重定向到 `agentclientprotocol/claude-agent-acp`，npm
  上也标了「已改名，迁移到新包」。**用新包**。
- 另有已废弃的第三方 `acp-claude-code`（`Xuanwo/acp-claude-code`，作者自己标 deprecated → 指向 Zed 版），
  不用。

适配器**自带 Claude Agent SDK**（SDK 内含 Claude Code 引擎），并**不**去 spawn 系统里的 `claude`
CLI；但它复用 `claude` 的登录态（`~/.claude`），所以本机装没装 `claude` CLI 不是硬前置，**登录态**才是。

## 二、确切启动命令 + 前置条件

**启动命令（argv）**：

```toml
claude = ["claude-agent-acp"]
```

（Windows 上 `_resolve_executable` 会自动补 `.cmd`；实测走 `%APPDATA%\npm\claude-agent-acp.cmd`。）

**前置条件**：

1. 装适配器（提供 `claude-agent-acp` 命令）：
   ```
   npm i -g @agentclientprotocol/claude-agent-acp
   ```
   （或 `npx @agentclientprotocol/claude-agent-acp`，但每次拉包慢，daemon 场景建议全局装拿到
   `.cmd` shim。）本机实测：`added 104 packages`，shim 落在 `%APPDATA%\npm\claude-agent-acp{,.cmd,.ps1}`。
2. 鉴权二选一（SDK 读取顺序：环境变量 > `~/.claude` 登录态）：
   - **claude.ai 订阅登录**（本机用的这个）：`claude auth login`（`claude auth status` 应显示
     `loggedIn: true`）。Pro/Max 订阅额度即可跑，无需 API key。
   - **API key**：`export ANTHROPIC_API_KEY=sk-ant-...`，走 API 计费。要给 daemon 用就写进
     `AgentSpawn.env` / 配置（daemon 只透传白名单 env + `AgentSpawn.env`，不会自动带上你 shell 里的
     `ANTHROPIC_API_KEY`）。
3. `claude` CLI 本身**非硬前置**（适配器自带 SDK 引擎），但装了更方便管理登录态。

## 三、实测结果（本机，真实子进程）

用本项目脚本跑的（都起真 `claude-agent-acp` 子进程）：

| 项 | 结果 | 证据 |
|---|---|---|
| **initialize 握手** | ✅ | `agent=@agentclientprotocol/claude-agent-acp title='Claude Agent' v0.59.0`，protocol_version=1 |
| **流式输出** | ✅ | `smoke_claude.py`：问 2+2，`on_output` 收到 `'4'`，`last_message='4'`（Task.last_output 能填） |
| **load_session（跨进程恢复）** | ✅ | `smoke_resume.py claude`：phase1 记数 4287→关进程→phase2 `load_session` 同 id→答 `'4287'`，`context SURVIVED` |
| **权限自动放行 + 工具执行** | ✅ | 让它写 hello.txt：`request_permission` 自动放行、`🔧 Write` 流式、文件真被创建、内容正确 |
| **审计动作回调 `on_action`** | ✅ | 收到 `{'kind': 'edit', 'title': 'Write'}` |
| **session/close** | ✅ | capabilities 通告 `close`；`aclose()` 无异常（区别于 copilot 不支持） |

**`agent_capabilities`（实测）**：

```json
{
  "load_session": true,
  "prompt_capabilities": { "image": true, "audio": false, "embedded_context": true },
  "mcp_capabilities": { "http": true, "sse": true, "acp": false },
  "session_capabilities": { "list": {}, "delete": {}, "additional_directories": {},
                            "fork": {}, "resume": {}, "close": {} },
  "auth": { "logout": {} },
  "field_meta": { "claudeCode": { "promptQueueing": true } }
}
```

比 copilot/opencode 更全：原生支持 `load_session` / `fork` / `resume` / `close` / `list` / `delete`，
以及 image + embedded_context prompt。

**`new_session` 的 `config_options`（模型/模式都暴露）**：

- `id="mode"`（category `mode`）：会话权限模式 select——`auto` / `default`（Manual，默认，危险操作前问）/
  `acceptEdits` / `plan` / `dontAsk` / `bypassPermissions`。当前 `default`。
- `id="model"`（category `model`）：当前模型，实测 `current_value = "claude-fable-5[1m]"`——
  **`_extract_model` 能取到**（与 opencode 同套 `id/category=="model"` 逻辑；`_extract_model` 已并入 main，
  claude 的模型会自动显示在就绪消息/`get_task`/`/task`/卡片 footer）。
- `id="effort"`（category `thought_level`）：思考强度 select（default/low/medium/high/xhigh/max），当前 `xhigh`。

也带 `modes`（current_mode_id + available_modes，同上 6 个权限模式），可用于将来切模式。

**没跑通 / 未验证的**：无「跑不通」项。未单独验证：多轮排队（`promptQueueing`）、图片/embedded_context
prompt、通过 ACP 的 `session/set_mode` 切权限模式、MCP 透传。这些是增强，不影响基本派活。

## 四、`[agents]` 配置示例

```toml
[agents]
copilot = ["copilot", "--acp"]
opencode = ["opencode", "acp"]
# Claude Code：经适配器 @agentclientprotocol/claude-agent-acp（无原生 ACP）。
# 前置：npm i -g @agentclientprotocol/claude-agent-acp + claude 已登录（claude auth login）。
claude = ["claude-agent-acp"]

[[projects]]
name = "my-project"
path = "C:/path/to/project"
default_agent = "claude"
```

## 五、要「完全接入」还差什么

现有 daemon/`acp_client.py` **不需要改**就能跑（握手/流式/恢复/权限全部走通用 ACP 路径，已实测）。
剩下都是可选打磨：

1. **启动延迟**：`new_session` 实测 ~15–18s 才返回（适配器 + Claude Agent SDK 冷启动重）。比
   copilot/opencode 慢，`AcpAgent.start()` 无超时——建议给握手/new_session 加超时或调大用户预期。
   若命中 `max_agents` 满 + 空闲挂起后频繁 `load_session`，冷启动开销会更明显。
2. **权限模式默认值**：默认 `default`（Manual）模式下适配器对危险操作发 `request_permission`，本项目
   client 已自动放行（实测能写文件），**功能上够用**。若想少一轮 permission 往返/更贴近「个人本地全权」，
   可在 new_session 后经 ACP 把 mode 切到 `acceptEdits` 或 `bypassPermissions`（当前 daemon 未做切模式，
   属增强）。
3. **模型可见性**：`_extract_model` 已并入 main，claude 的当前模型（`claude-fable-5[1m]`）会自动出现在
   就绪消息、`get_task`、`/task` 与卡片底部 footer，无需额外改动。
4. **鉴权透传**：daemon 只透传 env 白名单 + `AgentSpawn.env`。claude.ai 登录态走 `~/.claude`（白名单里
   的 USERPROFILE/APPDATA 够定位），已验证能跑；若要用 API key 记得写进 `AgentSpawn.env`。
5. **卡片碎片**：Claude 的最终回复是整块 `agent_message_chunk`（实测一次一大段，不像 opencode 逐 token
   碎），`_StreamFormatter` 的 💭 连续态处理对它更友好，无需额外打磨。

## 六、复现命令

```bash
# 装适配器
npm i -g @agentclientprotocol/claude-agent-acp
# 确认 claude 已登录
claude auth status
# 抓握手 / capabilities / 模型 / config_options
uv run python scripts/capture_acp_meta.py claude
# 流式冒烟
uv run python scripts/smoke_claude.py
# load_session 跨进程恢复
uv run python scripts/smoke_resume.py claude
```
