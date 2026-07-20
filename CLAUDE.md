# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目状态

**P0 已完成并真实验证通过**（2026-07-17，master `305fde3` 之后由用户在真实飞书环境实测）：ACP 流式输出实时回话题、话题内回复继续指挥 agent 两条验证均通过。此前经历过一轮深度 review + 三批修复（缺陷台账见 `docs/reviews/2026-07-17-p0-review.md`，22 项确认缺陷全部处理）。下一阶段是 P1（多 agent 并发 + worktree 隔离）与 P2（调度器 LLM 规划）。权威设计来源是 `docs/design.md`；若实现与设计冲突，先更新设计文档。文档统一用中文。

## 项目是什么

飞书驱动的个人 coding agent 调度器：用户在飞书群里描述任务，本地 daemon 通过 LLM 理解任务、按项目拆解，派发给底层 coding agent（Copilot CLI / OpenCode，均通过 ACP 协议控制）。每个 agent 对应一个飞书话题，用户可在话题内实时查看输出并中途下指令。P0 阶段无 LLM 规划（`/run` 命令直接匹配项目）。

## 架构（实现时必须遵守的关键决策）

- **飞书通信**：WebSocket 长连接（纯出站），用**普通群** + `reply_in_thread: true` 建话题；群主线 = 控制台（`/run`、`/agents`），话题 = agent 子会话，用根 `message_id` 路由（`root_id == message_id` 为根消息）。消息按 `message_id` 幂等去重（飞书对 ACK 异常事件会重推）。
- **Agent 控制**：ACP（JSON-RPC 2.0 over stdio），官方 `agent-client-protocol` SDK（**import 名是 `acp`**）。不要用 PTY hack。daemon 是 agent 无关的——按项目 `default_agent` 启动 `[agents]` 里配置的 argv。**Copilot（`copilot --acp`）与 OpenCode（`opencode acp`）均本地实测握手/流式通过**；冒烟 `scripts/smoke_acp.py` / `scripts/smoke_opencode.py`。注意 OpenCode 支持 `session/close`（Copilot 不支持，`aclose` 里已 catch 忽略）；OpenCode 思考为逐 token 流式（`_extract_text` 目前给每个 thought chunk 加 💭 前缀，卡片里会碎，待打磨）。
- **agent 生命周期**（review R2/R3 后的设计，改动 daemon.py 前必读）：一个 `/run` = 一个 `_AgentSession`，agent 进程与 ACP session **跨 turn 存活**（上下文在 session 里）；每 session 一个 prompt 队列 + 单消费者 worker 串行执行 turn；话题回复只入队；`/stop`（None 哨兵）、出错、**空闲超时挂起**或 daemon 退出才关闭。`AcpAgent.start()` 禁止二次调用（会抛 RuntimeError）。
- **概念模型（Project / Task / Session / Thread / Turn / Workspace / Agent）**：见 `docs/design.md`「概念模型」。**Task 是 daemon 拥有的一等持久实体**（`store.py` 的 `Task` + `TaskStore`，落盘 `tasks.json`，按 `task_id`=`t<N>` 短自增、持久单调计数器、**永不复用**）；Session（agent 侧记忆，`session_id`）与 Thread（飞书话题，`thread_root_id`）是外部系统的东西，Task 只握 id。运行态 `_AgentSession` 按 `thread_root_id` 索引，携带 `task_id`。
- **max_agents 名额 + 空闲挂起**：`max_agents`（默认 3）限活跃 session 数。worker 的 `queue.get()` 带 `idle_timeout`（默认 1800s，`<=0` 关闭）——超时挂起：关进程腾名额，Task 置 `suspended`（**保留**记录，区别于早期直接删），回复即 `load_session` 恢复。上限检查与 `_launch` 登记之间**不能有 await**（否则并发 `/run` TOCTOU 突破上限）。
- **Task status 生命周期**（worker 经 `store.update` 维护）：机械态 `starting`→`running`↔`idle`→`suspended`（自动）；语义终止态 `done`/`stopped`/`failed`（人/调度器）。`/stop` 标 `stopped`（保留历史，不删）；终止任务**不自动恢复**（回复提示重开），`suspended`/`idle` 才 `load_session` 无缝续。`_shutdown` 把活跃 Task 标 `suspended` 以便重启后状态准确。终止历史留最近 `keep_terminal`（默认 50）+ `clear_terminal`。
- **会话跨重启恢复**：重启后话题回复到达而无活跃 session 时，`store.by_thread(thread_root)` → Task（非终止且有 session_id）→ `AcpAgent(resume_session_id=Task.session_id)` 走 ACP `load_session`（load 期间 `_ClientImpl.set_suppress(True)` 抑制历史重放）。在途 turn 不恢复（只恢复会话上下文）。
- **输出转发**：`stream_mode` 二选一（config，默认 `card`）。`card`（`livecard.py`）——每回合一张 interactive 卡片,随输出 PATCH 原地更新(5 QPS/条、无编辑次数上限)，顶部状态灯(🔄/✅/❌/🛑)，body 超 25KB 滚动到新卡片；`text`（`throttler.py`）——每 ~500ms 批次发一条新文本消息(兜底)。两模式经 `_AgentSession.current_channel` 间接层做到每回合独立、对 worker 透明。状态类消息(🚀/▶️/✅/❌)始终走纯文本。
- **权限**：`request_permission` 自动放行——必须返回 `AllowedOutcome(outcome="selected", option_id=...)` 结构（从 options 挑 allow_once/allow_always），裸字符串过不了 pydantic 校验。fs/terminal 能力未通告也未实现。
- **环境变量**：agent 子进程只拿 SDK 白名单（PATH/APPDATA/USERPROFILE 等 12 个）+ `AgentSpawn.env` 显式追加项，**不再透传完整 os.environ**。要给 agent 传 token 就写进 `AgentSpawn.env` / 配置。
- **调度器 LLM（P2，核心已接线，opt-in）**：配了 `[llm]`（OpenAI 兼容端点 base_url/api_key/model）才启用——群里非 `/命令` 的自然语言 root 消息交给 `scheduler.py` 的工具循环派发（`daemon._dispatch_nl`）。未配则回退到「用法」。工具集（Phase 2 后共 7 个）：`list_projects` / `spawn_agent`（**仅新建**，`send_root_message` 每次开新话题）/ `list_tasks` / `get_task(id)` / `send_to_task(id, msg)`（操作**已有**任务：在跑排队、挂起先 `load_session` 恢复）/ `resume_task(id)`（显式恢复挂起/终止任务，仅拉起不跑首轮）/ `mark_done(id)`（归档）。system prompt 明确「新建 vs 操作已有」，避免对已有任务重复 spawn 丢上下文。轻量 router 边界：只理解/识别项目/派发/查状态，**不碰代码**。真实端点已实测（deepseek）。`llm.py` = OpenAI 兼容 client（httpx）；`scheduler.py` = provider 无关引擎。
  - **对话记忆**：`SchedulerMemory`（主线 (user, assistant) 成对，跨重启持久化到 `scheduler_memory.json`，限长）——每次 dispatch 带上历史（`run_tool_loop(history=...)`），支持追问/修正/指代。**注意主线 = 跟调度器聊；话题内回复 = 跟 agent 聊，两层上下文分开**。
  - **完成/出错/挂起主线通知**：worker 在「完成一轮且已空闲 / 出错 / 空闲挂起」时经 `_notify_main`（`send_root_message`，不建话题）推一条主线消息（🔔/❌/💤）。
  - **状态**：任务态在 `store.py` 的 `Task.status`（+ `turns`），`_sched_list_tasks`/`_sched_get_task` 读台账报给 LLM（不是内存 session）。（审计动作日志 = 下一步 A。）
  - **命令（root/话题）**：root = `/run`、`/agents`、`/clear`（清终止历史）；话题内 = 回复追加、`/stop`（标 stopped）、`/done`（标 done 归档）。`/done` 与 `mark_done` 共用 `_finish_task`：有活跃 worker 走 None 哨兵优雅收尾（`_AgentSession.terminate_status` 决定落 stopped/done），无活跃则直接改台账。恢复逻辑收敛到 `_try_resume`（check→`_launch` 无 await 防 TOCTOU），`_launch(first_prompt=None)` = 仅拉起在线。
  - **回复分层（勿回退）**：对用户对话/命令用 `_reply_user`（`bridge.reply`，`reply_in_thread=false`，**不建话题**）；只有 agent 输出/状态进它自己的话题才用 `reply_in_thread=true`。
- **并发隔离**（P1）：仅并发时创建 git worktree + 临时分支（`agent/<project>-<task-id>`）。

## 开发命令

用 `uv` 管理（Python 3.12 已 pin；本机无系统 Python，一律 `uv run`）。

- 安装依赖：`uv sync`
- 测试：`uv run pytest -q`（135 个，含 daemon 生命周期 + 任务系统集成测试）
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
