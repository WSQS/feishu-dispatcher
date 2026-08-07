# feishu-dispatcher

> 在飞书群里发一句话，本地 daemon 就帮你把活派给 coding agent；agent 的输出实时回到飞书话题，你随时在话题里接着指挥它。

一个**个人用**的 coding agent 调度器。你人不在电脑前，也能用飞书（手机 / 网页 / 客户端都行）给本地的 Copilot、OpenCode、Claude Code、Cline、Codex 派任务、看进度、下指令。任务在你自己的机器上跑，代码和凭据都不出本地。

## 它是怎么工作的

```
你在飞书群里说要做什么
        │
        ▼
本地 daemon 理解任务 → 挑项目 → 启动一个 coding agent（ACP 协议控制）
        │
        ▼
agent 边干边把输出流式发回飞书 —— 每个任务一个独立「话题」
        │
        ▼
你在话题里直接回复 = 继续给这个 agent 下指令（上下文一直在）
```

- **群主线 = 控制台**：发命令派任务、查状态。
- **每个话题 = 一个 agent 的专属会话**：实时看它干活，随时插话、切模型、喊停。
- daemon 关掉再开，话题还能接着聊（会话自动恢复）。

## 两种派发方式

**1. 命令式（随时可用）**

```
/run feishu-dispatcher 把 README 翻译成英文
```

在这条消息下建一个话题，agent 的输出实时回到话题里。

**2. 自然语言（配了调度器 LLM 后）**

直接说人话，调度器 LLM 帮你认项目、派 agent：

```
帮 feishu-dispatcher 加个深色模式
```

不配 LLM 也不影响用，`/run` 等命令照常。

## 支持的 agent 后端

都通过 ACP 协议（Agent Client Protocol）控制，本地实测握手 / 流式 / 会话恢复均通过，可按项目分别指定：

| 后端 | 启动命令 | 前置 |
|---|---|---|
| **Copilot CLI** | `copilot --acp` | `copilot` 已登录 GitHub |
| **OpenCode** | `opencode acp` | `opencode` 配好 provider |
| **Claude Code** | `claude-agent-acp` | 装社区适配器 + `claude` 已登录（详见 [docs/claude-code-backend.md](docs/claude-code-backend.md)） |
| **Cline** | `cline --acp` | `cline` v3.0.47+ 且 `cline auth` 登录 |
| **Codex CLI** | `codex-acp` | 装社区适配器 + `codex login`（Windows 需设 `CODEX_PATH` 指向全局 codex，详见 [config.example.toml](config.example.toml)） |

## 快速开始

前置：本机装好 [`uv`](https://docs.astral.sh/uv/)，以及至少一个上面的 agent CLI。

```powershell
git clone https://github.com/WSQS/feishu-dispatcher.git
cd feishu-dispatcher
uv sync                                  # 装依赖
```

项目就绪。接下来配飞书应用、拿 chat_id、启动 daemon，**一步步跟着 👉 [docs/setup.md](docs/setup.md) 走**（从零到能用）。

## 常用命令速查

| 在哪发 | 命令 | 作用 |
|---|---|---|
| 群主线 | `/run <项目> <任务>` | 派任务，建话题 |
| 群主线 | `/agents` | 列活跃 + 历史任务 |
| 群主线 | `/project` | 看 / 加 / 删项目 |
| 话题内 | 直接回复 | 给这个 agent 追加指令 |
| 话题内 | `/cancel [新指令]` | 停当前轮但保留 agent |
| 话题内 | `/stop` / `/done` | 结束 / 归档任务 |
| 话题内 | `/model [名]` | 查看 / 切换模型 |

完整命令、心智模型、长任务、排障 👉 **[docs/usage.md 使用手册](docs/usage.md)**。

## 文档

- **[使用手册 docs/usage.md](docs/usage.md)** — 装好之后怎么用（命令全集 + FAQ）
- **[配置指南 docs/setup.md](docs/setup.md)** — 从零把飞书应用 + daemon 跑起来
- [Claude Code 后端接入 docs/claude-code-backend.md](docs/claude-code-backend.md)
- [设计方案 docs/design.md](docs/design.md)（架构与决策，偏开发者）

## 状态

已在真实飞书环境验证可用。当前能力：ACP 流式输出实时回话题、话题内继续指挥、多后端（Copilot / OpenCode / Claude Code / Cline / Codex）、会话跨重启恢复、空闲自动挂起省资源、自然语言派发（调度器 LLM）、后台长任务跑完自动唤回 agent。下一步方向：多 agent 并发的 worktree 隔离。

想深入实现细节，看 [docs/design.md](docs/design.md)。
