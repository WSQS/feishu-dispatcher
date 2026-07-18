# feishu-dispatcher

飞书驱动的个人 coding agent 调度器：飞书群发 `/run <项目> <任务>`，本地 daemon 启动 Copilot CLI（ACP 协议），agent 流式输出实时回到飞书话题，话题内回复即可继续指挥 agent。

## 文档

- 设计方案：[docs/design.md](docs/design.md)
- 配置指南（从零跑起来）：[docs/setup.md](docs/setup.md)
- P0 实现审查报告：[docs/reviews/2026-07-17-p0-review.md](docs/reviews/2026-07-17-p0-review.md)

## 状态

P0 已完成并在真实飞书环境验证通过。之后陆续落地：卡片流式、双 agent（Copilot/OpenCode）、会话跨重启恢复、空闲挂起、以及 **P2 调度器 LLM（自然语言派发，配 `[llm]` 后可用，真实端点已实测）**。下一步候选：P1 多 agent 并发（worktree 隔离）、CLI↔ACP 会话交接。

## 快速开始

```powershell
uv sync
uv run pytest -q                        # 50 个测试
uv run python scripts/smoke_acp.py      # ACP 冒烟（需本机 copilot 已登录）
uv run feishu-dispatcher start          # 需先按 docs/setup.md 配置飞书应用
```
