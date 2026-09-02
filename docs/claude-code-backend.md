# Claude Code 后端接入（ACP 适配器）

Claude Code **无原生 ACP**，经 Zed 官方适配器接入；daemon 侧零代码改动，只需在 `[agents]` 加一条配置。

## 选型（为什么是这个适配器）

- Claude Code 本体不暴露 ACP server（`claude mcp` 是把它当 MCP client，不是 ACP；upstream feature request 仍 open）。
- 用 Zed 维护的官方适配器 **`@agentclientprotocol/claude-agent-acp`**（bin = `claude-agent-acp`）：以 Claude Agent SDK（内含 Claude Code 引擎）实现 ACP agent server。
- 勿用旧包：`@zed-industries/claude-code-acp`（旧名，已停更）；第三方 `acp-claude-code`（作者已标 deprecated）。npm 上旧包已标注迁移到新包。
- 适配器**不 spawn 系统 `claude` CLI**，但复用 `~/.claude` 登录态——**登录态是硬前置**，装没装 claude CLI 不是。

## 安装与前置

1. 装适配器：`npm i -g @agentclientprotocol/claude-agent-acp`
   （Windows 上 daemon 的 `resolve_executable` 会自动补 `.cmd` shim。）
2. 鉴权二选一（SDK 读取顺序：环境变量 > `~/.claude` 登录态）：
   - **claude.ai 订阅登录**：`claude auth login`（`claude auth status` 应显示 `loggedIn: true`）。Pro/Max 订阅额度即可，无需 API key。
   - **API key**：`ANTHROPIC_API_KEY` 写进 `AgentSpawn.env` / 配置——daemon 只透传白名单 env + `AgentSpawn.env`，不会自动带上 shell 里的变量。
3. `claude` CLI 本身非硬前置（装了更方便管理登录态）。

## 配置示例

```toml
[agents]
claude = ["claude-agent-acp"]

[[projects]]
name = "my-project"
path = "C:/path/to/project"
default_agent = "claude"
```

## 后端能力速览

- capabilities 比 copilot/opencode 全：原生 `load_session` / `fork` / `resume` / `close` / `list` / `delete`，支持 image 与 embedded_context prompt。
- `new_session` 暴露 `mode` / `model` / `effort` 三项 select：模型自动显示、`/model` 可切（与其他后端同套机制）；权限模式默认 `default`（Manual，危险操作问询——本项目 client 对 `request_permission` 自动放行，功能够用；切 `acceptEdits` / `bypassPermissions` 属将来增强）。
- 已知特性：冷启动 ~15–18s（适配器 + SDK 重），明显慢于其他后端；最终回复为整块 `agent_message_chunk`（非逐 token 碎片），流式格式化对它友好。

## 复现 / 冒烟

```bash
npm i -g @agentclientprotocol/claude-agent-acp   # 装适配器
claude auth status                               # 确认已登录
uv run python scripts/capture_acp_meta.py claude # 握手 / capabilities / 模型
uv run python scripts/smoke_claude.py            # 流式冒烟
uv run python scripts/smoke_resume.py claude     # load_session 跨进程恢复
```

> 历史版本（2026-08-14 前）含逐项实测证据表与 `agent_capabilities` JSON，见 git 历史。
