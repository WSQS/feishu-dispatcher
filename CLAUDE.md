# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态

**P0 已完成并真实验证通过**（2026-07-17，master `305fde3` 之后由用户在真实飞书环境实测）：ACP 流式输出实时回话题、话题内回复继续指挥 agent 两条验证均通过。此前经历过一轮深度 review + 三批修复（缺陷台账见 `docs/reviews/2026-07-17-p0-review.md`，22 项确认缺陷全部处理）。下一阶段是 P1（多 agent 并发 + worktree 隔离）与 P2（调度器 LLM 规划）。权威设计来源是 `docs/design.md`；若实现与设计冲突，先更新设计文档。文档统一用中文。

## 项目是什么

飞书驱动的个人 coding agent 调度器：用户在飞书群里描述任务，本地 daemon 通过 LLM 理解任务、按项目拆解，派发给底层 coding agent（Copilot CLI / OpenCode，均通过 ACP 协议控制）。每个 agent 对应一个飞书话题，用户可在话题内实时查看输出并中途下指令。P0 阶段无 LLM 规划（`/run` 命令直接匹配项目）。

## 架构（实现时必须遵守的关键决策）

- **飞书通信**：WebSocket 长连接（纯出站），用**普通群** + `reply_in_thread: true` 建话题；群主线 = 控制台（`/run`、`/agents`），话题 = agent 子会话，用根 `message_id` 路由（`root_id == message_id` 为根消息）。消息按 `message_id` 幂等去重（飞书对 ACK 异常事件会重推）。
- **Agent 控制**：ACP（JSON-RPC 2.0 over stdio），官方 `agent-client-protocol` SDK（**import 名是 `acp`**）。不要用 PTY hack。daemon 是 agent 无关的——按项目 `default_agent` 启动 `[agents]` 里配置的 argv。**Copilot（`copilot --acp`）与 OpenCode（`opencode acp`）均本地实测握手/流式通过**；冒烟 `scripts/smoke_acp.py` / `scripts/smoke_opencode.py`。注意 OpenCode 支持 `session/close`（Copilot 不支持，`aclose` 里已 catch 忽略）；OpenCode 思考为逐 token 流式（`_extract_text` 目前给每个 thought chunk 加 💭 前缀，卡片里会碎，待打磨）。
- **agent 生命周期**（review R2/R3 后的设计，改动 daemon.py 前必读）：一个 `/run` = 一个 `_AgentSession`，agent 进程与 ACP session **跨 turn 存活**（上下文在 session 里）；每 session 一个 prompt 队列 + 单消费者 worker 串行执行 turn；话题回复只入队；`/stop`（None 哨兵）、出错或 daemon 退出才关闭。`AcpAgent.start()` 禁止二次调用（会抛 RuntimeError）。
- **输出转发**：`stream_mode` 二选一（config，默认 `card`）。`card`（`livecard.py`）——每回合一张 interactive 卡片,随输出 PATCH 原地更新(5 QPS/条、无编辑次数上限)，顶部状态灯(🔄/✅/❌/🛑)，body 超 25KB 滚动到新卡片；`text`（`throttler.py`）——每 ~500ms 批次发一条新文本消息(兜底)。两模式经 `_AgentSession.current_channel` 间接层做到每回合独立、对 worker 透明。状态类消息(🚀/▶️/✅/❌)始终走纯文本。
- **权限**：`request_permission` 自动放行——必须返回 `AllowedOutcome(outcome="selected", option_id=...)` 结构（从 options 挑 allow_once/allow_always），裸字符串过不了 pydantic 校验。fs/terminal 能力未通告也未实现。
- **环境变量**：agent 子进程只拿 SDK 白名单（PATH/APPDATA/USERPROFILE 等 12 个）+ `AgentSpawn.env` 显式追加项，**不再透传完整 os.environ**。要给 agent 传 token 就写进 `AgentSpawn.env` / 配置。
- **调度器 LLM 边界**（P2）：轻量 router，只做理解/拆解/分派/状态查询/并发判断，不碰代码。
- **并发隔离**（P1）：仅并发时创建 git worktree + 临时分支（`agent/<project>-<task-id>`）。

## 开发命令

用 `uv` 管理（Python 3.12 已 pin；本机无系统 Python，一律 `uv run`）。

- 安装依赖：`uv sync`
- 测试：`uv run pytest -q`（50 个，含 daemon 生命周期集成测试）
- Lint / 格式：`uv run ruff check .` / `uv run ruff format .`
- daemon：`uv run feishu-dispatcher start`（`--discover` 发现 chat_id；`-v` 调试日志；`--config <path>`）
- ACP 冒烟（不经飞书，真实 Copilot）：`uv run python scripts/smoke_acp.py`
- 飞书应用配置全流程：`docs/setup.md`

包结构：`feishu_dispatcher/`（`cli.py` 入口、`config.py`、`daemon.py` 调度主循环、`acp_client.py` ACP 封装、`feishu.py` 飞书 WS+HTTP 桥、`throttler.py` 节流、`_lark_compat.py` SDK 兼容 shim）。

## 已知风险与注意事项

- **lark-oapi 在 Windows + Defender 下会崩**：`import lark_oapi` eager import 57 个 API namespace 触发 access violation（0xC0000005）。已用 `_lark_compat.py` 空壳 shim 绕开，实际只加载 `ws.pb`（protobuf）+ `ws.const`，事件 JSON 全部手写 dict 解析。**改飞书相关代码务必保持 `__init__.py` 里 `_lark_compat` 最先 import**。
- **feishu.py 不走 `lark.ws.Client`**（其依赖链会触发上述崩溃），自实现 WS 长连接。frame/ACK/ping/合包语义**必须对照官方参考实现** `.venv/Lib/site-packages/lark_oapi/ws/client.py`：ACK 成功 `{"code": 200}` 失败 500 + `biz_rt` 头；ping 间隔服务端可下发（endpoint 发现响应 / pong payload 的 `PingInterval`）；合包缓存 5s TTL。
- **飞书限频**：同群全部机器人共享 5 QPS（全应用 50/s）；`max_agents` 默认 3 与之配套；HTTP 层已带 Retry（429/5xx，尊重 Retry-After）。多 agent 高并发需要 per-chat 令牌桶（P1，未做）。
- **安全默认**：`chat_id` 必填（空则拒绝启动，发现模式用 `--discover`）；`sender_whitelist` 建议配置；`/run` 并发上限 `max_agents`。
- WS 线程死亡由 daemon 30s 看门狗自动重启；agent 子进程 stderr 有后台 drain（防管道满卡死）。
- P1/P2 待办：LLM 规划、多 agent worktree 隔离、卡片流式（PATCH interactive card 无编辑次数上限，见 setup.md §8）、per-chat 令牌桶。
