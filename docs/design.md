# feishu-dispatcher — 设计方案

> 2026-07-17 grilling session 产出

## 一句话定义

飞书驱动的个人 coding agent 调度器。你在飞书话题群里跟调度器对话，调度器理解任务、按项目拆解、派发给底层 coding agent（Copilot CLI / OpenCode）执行。每个 agent 在独立的飞书话题里运行，你可以随时跳进话题查看实时输出并直接指挥 agent。

## 架构总览

```
飞书话题群 ←─WebSocket 长连接─→ 本地 daemon（纯出站）
                                    │
                          ┌─────────┼─────────┐
                          │         │         │
                     LLM API    内置 Tools   Agent Manager
                    (规划/理解)  (项目管理)   (ACP 进程)
                                                    │
                                          ┌─────────┼─────────┐
                                          │         │         │
                                     Copilot   OpenCode    ...
                                     (ACP)     (ACP)
```

### 数据流

1. 你在飞书话题群发根消息（任务描述）
2. daemon 收到消息 → 调用调度器 LLM 理解任务
3. LLM 识别涉及的项目 → 调用 `spawn_agent` tool
4. daemon 启动 agent 进程（ACP over stdio）+ 创建飞书话题
5. agent 的 streaming output → daemon 批量节流 → 发到对应话题
6. 你在话题内回复 → daemon 将回复作为 ACP `session/prompt` 发给 agent
7. agent 完成/报错 → daemon 更新话题状态

## 决策清单

| # | 决策点 | 结论 |
|---|--------|------|
| 1 | 调度器职责 | 个人助手，按项目（档位 B）拆解任务，派发给 coding agent |
| 2 | 前端 | 普通群（主线=控制台）+ `reply_in_thread` 建话题（话题=agent 子 session） |
| 3 | 执行环境 | 本地 daemon，agent 通过 ACP 协议控制 |
| 4 | 任务���解粒度 | 按项目分派，不做步骤级拆解 |
| 5 | 项目管理 | 应用内 tool 自管理（LLM 自主注册/查询），落盘本地 |
| 6 | daemon 形态 | 独立 Python 进程 + LLM API + 内置 tool calling（非 MCP） |
| 7 | 飞书通信 | WebSocket 长连接（`lark-oapi` Python SDK），纯出站无需公网暴露 |
| 8 | agent 输出回飞书 | 全量转发 + 批量节流（~500ms 窗口合并） |
| 9 | 技术栈 | Python（原型验证后最终确认） |
| 10 | 调度器 LLM 边界 | 轻量 router：理解、拆解、分派、状态查询、并发判断。不碰代码 |
| 11 | worktree | 仅并发时创建 worktree + 临时分支（`agent/<project>-<task-id>`）隔离 |
| 12 | daemon 启动 | 手动启动（`feishu-dispatcher start`），后续可包装为系统服务 |

### 决策详情

**档位 B 拆解**：调度器理解任务涉及哪些项目，每个项目派一个 agent。不拆步骤级子任务（那是 agent 的工作），不做原子操作编排（那是重建 Devin）。

**ACP（Agent Client Protocol）**：
- JSON-RPC 2.0 over stdio，agent 作为子进程运行
- 流式输出通过 JSON-RPC notification，token 级实时推送
- Copilot CLI 已支持 ACP（2026-01 public preview），OpenCode 官方支持
- 官方 Python SDK：`agent-client-protocol` PyPI 包（asyncio + Pydantic）
- 消除了 PTY hack 的需要，输出结构化（text / tool call / diff / permission request）

**飞书话题群**：
- 飞书单聊不支持话题；话题形式群（`group_message_type: "thread"`）没有群主线，不适合做控制台
- 改用**普通群**：群主线 = 控制台（发 `/run` 等命令），`reply_in_thread: true` 在根消息下建话题 = agent 子 session
- 用根 `message_id` 路由消息到正确话题（`root_id == message_id` 为根消息，`root_id != message_id` 为话题回复）

**调度器 LLM 边界**：
```
你是一个任务调度器。你的职责：
1. 理解用户的任务描述，识别涉及哪些已注册项目
2. 为每个项目创建 agent 任务
3. 判断任务是否可以并发（同项目独立修改可并发，有依赖须串行）
4. 如需并发，为每个任务创建 git worktree 隔离工作区
5. 回答用户关于 agent 状态的问题
你不写代码、不改文件、不跑命令。这些是 agent 的工作。
```

## 内置 Tools（供调度器 LLM 调用）

| Tool | 参数 | 说明 |
|------|------|------|
| `list_projects()` | — | 返回已注册项目列表 |
| `register_project(path, name?, stack?, test_cmd?, default_agent?)` | — | 注册新项目，落盘到本地配置 |
| `spawn_agent(project, task, worktree?)` | — | 启动 agent 进程（ACP）+ 创建飞书话题。如需并发，自动创建 worktree |
| `send_to_agent(thread_id, message)` | — | 向指定 agent 发送消息（ACP `session/prompt`） |
| `get_agent_status(thread_id?)` | — | 查询 agent 状态（running / waiting / done / failed） |
| `list_agents()` | — | 列出所有活跃 agent |

## 原型验证计划

### 原型范围

只做 P0（核心闭环）+ 硬编码项目配置。不做 LLM 规划，不做多 agent 并发，不做 worktree。

**最小端到端 demo**：
```
你在飞书发消息 → daemon 启动 Copilot CLI（ACP）→
agent 输出实时回到飞书话题 →
你在话题回复 → 消息传回 agent
```

### P0 — 不通过则方案不成立

1. **ACP 流式输出 → 飞书实时转发链路**
   - daemon 作为 ACP client 启动 Copilot CLI
   - 收到 streaming notification → 批量节流 → 发到飞书话题
   - 验证：agent 思考过程能否近实时在飞书看到？延迟可接受？

2. **飞书话题双向通信**
   - 你在话题内回复 → daemon 收到 → 通过 ACP 发给 agent
   - 验证：agent 能否接收中途插入���指令并响应？

### P1 — 影响体验但不影响可行性（原型后迭代）

3. 多 agent 并发（独立话题 + worktree 隔离）
4. agent 生命周期管理（完成/报错/取消 → 进程清理 + 状态通知）

### P2 — 体验优化（可跳过）

5. 调度器 LLM 规划 —— ✅ **核心 + 记忆/通知/状态已接线**（`scheduler.py` 工具循环 +
   `llm.py` OpenAI 兼容 client + `daemon._dispatch_nl`；配 `[llm]` 后自然语言即派发，真实
   deepseek 端点已实测）。已加:主线对话记忆(跨重启持久化 `SchedulerMemory`)、agent
   完成/出错/挂起的主线通知、加厚的 `list_agents` 状态(state/turns)。

   **调度器职责说明书（2026-07-18 明确）**:定位=控制台主线的「控制塔」。
   - 该做:记住主线对话、掌握 agent 状态、派发、（下一步）路由消息到 agent、管理项目。
   - 不该做:写代码/改文件/跑命令（agent 的活）。
   - **两层上下文**:调度器上下文(主线,daemon 侧,`SchedulerMemory`) vs agent 上下文
     (每个 agent 自己的会话,agent 侧)。话题内回复=跟 agent 聊;主线=跟调度器聊。
   - **下一步 A（审计）**:记录每个 agent 的动作日志（ACP `tool_call` 事件）+
     `get_agent_status` 工具,支持「查看它做了什么」的事后审计。（B=事前审批，另开线。）
6. 项目自注册（原型阶段手动写死项目列表；`register_project` 工具尚未加）

## 依赖

| 依赖 | 用途 |
|------|------|
| `lark-oapi` | 飞书开放平台 Python SDK（WebSocket 长连接 + 消息 API） |
| `agent-client-protocol` | ACP 官方 Python SDK（ACP client） |
| LLM API（openai/anthropic） | 调度器大脑（tool calling） |

## 开放问题

- ACP Python SDK 的 async transport 在 Windows 上的兼容性（原型验证）
- 飞书消息卡片是否用于 agent 状态展示（当前方案是全量文本转发）
- 多 agent 并发时飞书通知的噪音问题（原型后评估）

## 待办 / 已知限制（post-P0）

### 会话跨 daemon 重启恢复（✅ 已实现 2026-07-17）

实现：`store.py` 把 `thread_root_id → {project, agent, session_id, cwd}` 落盘到
config 同目录 `sessions.json`；话题回复到达而无活跃 agent 时，daemon 用 ACP
`load_session` 惰性重连（`AcpAgent(resume_session_id=...)`，load 期间抑制历史重放，
避免旧对话灌进新卡片）；`/stop` 删记录，agent 未配置/加载失败则明确提示重开（不再
静默忽略）。已用 opencode 实测跨进程恢复通过。

**（历史）问题背景**：重启后所有 agent 会话曾会丢失。两个原因叠加：
1. `_Daemon._sessions` 是纯内存 dict（`daemon.py`），零持久化 —— 重启即忘记
   thread→session 的全部映射。
2. `AcpAgent.start()` 永远 `new_session`，从不 `load_session`；且 agent 子进程
   随 daemon 退出被回收。

用户侧症状：老话题变孤儿，回复被 `_forward_to_agent` 静默忽略（无任何提示）。

**恢复是可做的（零件已具备）**：底层 agent 自己把会话存了盘（opencode 有
`opencode.db` + `session list/resume`；copilot/opencode 均通告 `load_session=True`），
ACP SDK 也暴露了 `load_session(cwd, session_id)`（`connection.py`）/ `list_sessions` /
`resume_session`。

**补上大致需要**：
1. 建会话时把 `thread_root_id → {project, agent, session_id}` 落盘（JSON/sqlite），
   `/stop` 时删。`session_id` 是 agent 专属，映射必须记住是哪个 agent。
2. 启动读回映射；**惰性重连** —— 已知但未激活的话题来新回复时，重 spawn 对应
   agent 并 `load_session` 接回（而非 `new_session`），再把回复入队。
3. `load_session` 失败（agent 侧会话已过期/不存在）→ 明确提示「会话已失效，请
   `/run` 重开」，并修掉那个静默忽略。

**范围外**：重启时正好在途的那一轮（未跑完的 prompt + 排队指令）无法可靠恢复；
只恢复会话上下文，不恢复在途 turn。

### 无需 @ 机器人即可触发（自动触发）

**目标**：群里发消息不用 @ 机器人就触发流程。

**现状**：飞书群里机器人**默认只收到 @ 它的消息**；要收全部消息需授予
`im:message.group_msg:readonly` 权限（setup.md 已要求）。代码侧 `_parse_event_message`
已剥离 `@_user_N` 前缀、并不强制 @。**所以授予该权限后，「不用 @ 自动触发」基本已成立**
（尤其话题内回复无需 @）。

**待办**：
- 确认并文档化：授予 `group_msg:readonly` 后，root `/run` 与话题回复均无需 @ 即被处理。
- 噪音取舍：控制台群若只有「你 + bot」，自动处理所有消息没问题；但当前**非命令的
  root 消息会回「用法…」**，若群里有闲聊会打扰。可考虑：非命令 root 消息静默（不回用法），
  或用法提示仅在明确请求（如 `/help`）时给。

### max_agents 名额释放的三个坑（✅ 已修 2026-07-17）

`max_agents`（默认 3）限制 `_sessions` 里同时存活的 session 数，在 `/run`
（`_spawn_for_root`）和会话恢复（`_recover_or_notify`）两处检查。曾有三个坑，现已修：

- **空闲 agent 占名额** → 加了 **`idle_timeout`（默认 1800s）空闲自动挂起**：一轮跑完后
  超时无新回复，worker 关掉 agent 子进程、腾出名额，但**保留** `sessions.json` 记录
  （区别于 `/stop` 的删除）——之后在该话题回复即走 `load_session` 无缝恢复。一并了结
  review R17「`_agents_by_thread` 永不清理」的僵尸 agent 遗留。`idle_timeout<=0` 可关闭。
- **拒绝文案误导** → 「请先 `/stop` 或等待完成」改为「请先 `/stop` 一个」。
- **上限检查 TOCTOU 竞态** → 检查与 `_launch` 登记之间原有 `await`（发「🚀」提示），
  并发两条 `/run` 可都通过检查再各自登记、突破上限。改为**先原子地检查+登记、再发提示**。

### CLI ↔ ACP 会话交接（下一步方向）

**探索问题**：一个 coding agent session 能否被 CLI 和 ACP 客户端**同时**控制？

**实测结论（2026-07-18）**：分两种情形。
- **真·同时（两端都活着、并发发指令）——不行**。我们用 ACP over stdio，每个
  `opencode acp` / `copilot --acp` 是独占子进程 + 私有 stdin/stdout 管道，CLI 接不进去；
  ACP 本质 1 client ↔ 1 agent。硬让两个进程开同一 session 会并发读写 opencode.db 冲突。
  （opencode 的 `serve` + 多客户端 attach 理论上可多客户端连一 session，但那是 opencode
  私有 HTTP API、非 ACP，用它要放弃 agent 无关性 + copilot，且并发回合语义含糊，不推荐。）
- **交接（谁都能接手、一次一个）——可以，已验证**。ACP 建的 opencode session 与 CLI
  **共用同一 opencode.db + id 空间**：实测用 ACP 建 session 植入「5566」、关闭，再用
  `opencode run -s <同一 session_id>` 从 CLI 问，答「5566」。所以：飞书(daemon/ACP)跑着 →
  跳终端 `opencode run -s <id>` 接着干 → 交回 daemon（`load_session` 恢复）。与「空闲挂起」
  天作之合（挂起即释放会话，CLI 正好安全接手）。

**下一步要做的产品化**：一对命令把「一次一个、安全交接」做顺滑，避免手动记 id + 撞车。
- `/handoff`：daemon 挂起该 agent + 打印 `opencode run -s <id>` 供粘贴到终端；给会话上
  **交接锁**，期间 daemon 不自动 `load_session` 恢复。
- `/resume`（或解锁后回复）：解锁，daemon 重新接回。

**注意点**：① 即便交接也**必须一次只有一端活着**（daemon 进程或 CLI 进程），否则撞同一
会话——故需交接锁，防止用户在 CLI 操作时飞书回复触发 daemon 恢复。② 目前只对 opencode
验证（CLI `-s <id>` 续会话）；copilot CLI 能否从命令行按 id 续 ACP 建的会话尚未验证。

### 任务系统（Task system，下一步方向 · 2026-07-18）

**动机**：现在调度器「记得有哪些任务」只能靠主线对话记忆，而记忆是滑动窗口（默认 12 轮），
多几个任务就把早的挤掉、忘了。**正解不是把记忆调大（贵且仍会丢），而是把「有哪些任务、
各自状态」做成结构化、持久化的任务系统，调度器用工具去查而非靠记。** 两者分工：对话记忆
= 记住你怎么说话（指代/追问，小即可）；任务系统 = 记住发生了什么（可查、持久）。

**做法**：把现有 `sessions.json` 的 SessionRecord 升级成完整 **Task**（任务 ≈ session ≈ 话题，1:1）：
- 字段：`task_id`（稳定 id）、`project`/`agent`/`description`（当初的自然语言需求）、
  `status`（pending/running/idle/done/stopped/failed/suspended）、`created_at`/`updated_at`、
  `thread_root_id`、`session_id`、（审计）动作日志/摘要。
- 调度器工具（取代偏薄的 `list_agents`）：`list_tasks`、`get_task(id)`，可选 `stop_task`、
  `send_to_task(id, msg)`。
- **行为改动**：`/stop` 从「删记录」改为「标记 `status=stopped` 保留历史」（否则调度器还是
  忘了已停的任务）；恢复只对可恢复状态生效。完成/停止任务留最近 N 个（+ 可 `/clear`）。
- **一举多得**：解决遗忘 + 统一 session 恢复记录 + 是审计（A）的落脚点（动作日志挂 task 上）。

### 调度器审计 A（事后审计，紧随任务系统）

记录每个 task 的**动作日志**——agent 调了哪些工具（编辑哪些文件、跑什么命令），来源是
ACP 的 `tool_call` 事件流（已流经 `_extract_text`，改成也存一份进 task）。配 `get_task(id)`
让主线能问「brick-blast 都干了啥」做事后审计，不用翻整个话题。（**B = 事前审批**：破坏性
操作前飞书卡片按钮确认，替换现在「全自动放行」——更重、涉安全，单开一条线。）

### 其他已考虑方向（roadmap，待排期）

- **`send_to_agent` / `send_to_task`**：主线一句话路由进某个在跑的 agent 话题，不用手动切过去。
- **`register_project`（项目自注册）**：对话式注册新项目并落盘，免去改 config + 重启。
- **权限审批 B（安全）**：见上，替换 auto-allow-all，单开线。
- **P1 多 agent 并发 + worktree 隔离**：同项目并行的文件隔离（跨项目并发已可用）；见上文 P1。
- **per-turn 取消**：ACP `session/cancel`，agent 跑偏时只停这一轮、不杀整个 agent。
- **完成通知带摘要**：🔔 通知里附 agent 最后输出的一句摘要（依赖任务系统的 last_output/摘要）。
- **自动触发降噪**：非命令 root 消息静默/仅 `/help` 给用法（见上文「无需 @」）。
- **对话记忆可配**：`[llm]` 加 `memory_rounds`（默认 12 轮）。

**优先级读法（我的建议）**：近期最高价值 = **任务系统 → 审计 A**（连着，任务系统是审计地基，
且直接解决「调度器忘任务」）；中期 = register_project / send_to_task / per-turn 取消 / 通知摘要；
较大或单开线 = 权限审批 B、CLI↔ACP 交接、P1 并发+worktree。
