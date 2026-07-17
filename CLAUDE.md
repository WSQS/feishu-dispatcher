# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态

P0 原型核心闭环已实现并通过 ACP 端到端冒烟验证（无飞书侧）。权威设计来源仍是 `docs/design.md`；若实现与设计冲突，先更新设计文档。所有源码在 `feishu_dispatcher/` 包下，文档统一用中文。

## 项目是什么

飞书驱动的个人 coding agent 调度器：用户在飞书话题群里描述任务，本地 daemon 通过 LLM 理解任务、按项目拆解，派发给底层 coding agent（Copilot CLI / OpenCode，均通过 ACP 协议控制）。每个 agent 对应一个飞书话题，用户可在话题内实时查看输出并中途下指令。

## 架构（实现时必须遵守的关键决策）

数据流：飞书话题群 ←WebSocket 长连接→ 本地 daemon → { 调度器 LLM（tool calling）、内置项目管理 tools、Agent Manager（ACP 子进程）}

- **飞书通信**：`lark-oapi` SDK 的 WebSocket 长连接，纯出站，无需公网暴露。飞书单聊不支持话题，必须用话题形式群（`group_message_type: "thread"`）；根消息 = 任务派发，`reply_in_thread: true` 创建话题 = agent 子 session，用 `thread_id` 路由消息。
- **Agent 控制**：ACP（Agent Client Protocol），JSON-RPC 2.0 over stdio，agent 作为子进程运行，用官方 `agent-client-protocol` PyPI SDK（asyncio + Pydantic）。不要用 PTY hack。
- **输出转发**：agent 流式输出全量转发到飞书话题，批量节流（~500ms 窗口合并）。
- **调度器 LLM 边界**：轻量 router，只做理解/拆解/分派/状态查询/并发判断。不写代码、不改文件、不跑命令——那是底层 agent 的职责。
- **拆解粒度**：按项目分派（一个项目一个 agent），不做步骤级子任务拆解。
- **并发隔离**：仅并发时才创建 git worktree + 临时分支（`agent/<project>-<task-id>`）。
- **内置 tools**（供调度器 LLM 调用，非 MCP）：`list_projects` / `register_project` / `spawn_agent` / `send_to_agent` / `get_agent_status` / `list_agents`，项目配置落盘本地。

## 原型范围（P0 优先）

原型只验证核心闭环：飞书发消息 → daemon 启动 Copilot CLI（ACP）→ agent 输出实时回话题 → 话题回复传回 agent。原型阶段硬编码项目配置，不做 LLM 规划、多 agent 并发和 worktree（分别是 P1/P2）。P0 两条验证不通过则整个方案不成立，详见 `docs/design.md` 的原型验证计划。

## 开发命令

用 `uv` 管理（Python 3.12，`.python-version` 已 pin）。

- 安装依赖：`uv sync`（dev 组：pytest / pytest-asyncio / ruff）
- 跑测试：`uv run pytest -q`
- Lint：`uv run ruff check .`（`--fix` 自���修）
- daemon 启动：`uv run feishu-dispatcher start`（或 `--config <path>` / `-v`）
- ACP 端到端冒烟（不经过飞书，直接验证 daemon↔Copilot 链路）：`uv run python scripts/smoke_acp.py`

包结构：`feishu_dispatcher/`（`cli.py` 入口、`config.py`、`daemon.py` 主循环、`acp_client.py` ACP 封装、`feishu.py` 飞书通信、`throttler.py` 节流、`_lark_compat.py` SDK 兼容 shim）。CLI 入口已在 `pyproject.toml` 声明：`feishu-dispatcher = "feishu_dispatcher.cli:main"`。

## 已知风险（实现时注意）

- **ACP Windows 兼容性已验证通过**：`agent-client-protocol` 0.11.0（import 名 `acp`）在 Windows + Python 3.12 上工作正常；`copilot.cmd` shim + `spawn_stdio_transport` 可用。注意 import 名是 `acp` 不是 `agent_client_protocol`。
- **lark-oapi SDK 在 Windows + Defender 下有严重问题**：`import lark_oapi` 会 eager import 全部 57 个 API namespace，Defender 实时扫描数百个小 `.py` 文件导致 access violation（exit 0xC0000005）。已用 `feishu_dispatcher/_lark_compat.py` 绕开（装空壳包跳过 eager import���只按需加载 `im.v1` + protobuf）。修改飞书通信相关代码时务必保持 `__init__.py` 里的 `_lark_compat` 首次 import 顺序。
- **飞书通信实现不走 `lark.ws.Client`**：因为它依赖 `EventDispatcherHandler`（eager import 25 个 processor）。`feishu.py` 改为直接用 `websockets` + 官方 protobuf Frame + `requests` 自实现 WS 长连接与 HTTP 发消息，绕开 SDK 的重依赖链。
- **Copilot 权限**：`_ClientImpl.request_permission` 自动返回 allow；Copilot 的写文件类工具可能仍按其内部策略执行（实测部分文件操作未落盘，需后续调权限交互）。
