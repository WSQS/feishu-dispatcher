# feishu-dispatcher

飞书驱动的个人 coding agent 调度器：飞书群发 `/run <项目> <任务>`，本地 daemon 启动 Copilot CLI（ACP 协议），agent 流式输出实时回到飞书话题，话题内回复即可继续指挥 agent。

## 文档

- 设计方案：[docs/design.md](docs/design.md)
- 配置指南（从零跑起来）：[docs/setup.md](docs/setup.md)
- P0 实现审查报告：[docs/reviews/2026-07-17-p0-review.md](docs/reviews/2026-07-17-p0-review.md)

## 状态

P0 原型代码完成（含深度 review 与修复）；ACP↔Copilot 链路已本地实测通过，飞书端 E2E 待真实凭据验证。

## 快速开始

```powershell
uv sync
uv run pytest -q                        # 50 个测试
uv run python scripts/smoke_acp.py      # ACP 冒烟（需本机 copilot 已登录）
uv run feishu-dispatcher start          # 需先按 docs/setup.md 配置飞书应用
```
