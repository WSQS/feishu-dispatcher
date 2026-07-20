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

## 概念模型（2026-07-18，任务系统地基）

**核心四层 + 附属：**

- **Project 项目**（顶层/目标）：要改的代码库。id=`name`；属性 path、default_agent；长期（注册后一直在）。
- **Task 任务**（**一等公民**，编排单元）：派发在某项目上的一个工作单元。id=`task_id`；属性
  project、agent、description、status、时间戳；**持有** session_id + thread_root_id + workspace。
  **daemon 真正拥有并持久化的核心实体（tasks.json）。**
- **Session 会话**（agent 侧）：agent 的记忆/上下文。id=`session_id`；live/dormant；活在 opencode.db，daemon 只握 id。
- **Thread 话题**（飞书侧）：展示 + 你回复 agent 的地方。id=`thread_root_id`；活在飞书，daemon 只握 id。
- **Turn 回合**：Task 的 session 的一次 prompt→响应。Task 1:N Turn；审计（A）的分组单位。
- **Action 动作**：某回合里 agent 调的工具（编辑/命令），来自 ACP `tool_call`。Turn 1:N Action。
- **Workspace 工作区**：Task 的工作目录。默认=项目目录；同项目并发（P1）=git worktree+临时分支。Task 1:1 Workspace。
- **Agent（后端类型+能力）**：copilot/opencode 等，带能力元数据（load_session/session_close/cancel 支持度）；Task 引用它，决定可做哪些操作。

```
Project ─1:N─► Task（一等公民）
                 ├─1:1─ Session   (agent 记忆, opencode.db)
                 ├─1:1─ Thread    (飞书话题)
                 ├─1:1─ Workspace (项目目录 / P1 worktree)
                 └─1:N─ Turn ─1:N─ Action   (审计)
Agent(后端类型+能力) ◄── Task 引用
```

**daemon 拥有 Project 与 Task；Session/Thread 是外部（agent/飞书）的，只握 id。**

- **task_id 规则**：`t<N>` 短自增，**持久单调计数器，永不复用**；自然指代（「brick-blast 那个」）由调度器 `list_tasks` 查表解析成 id。
- **status 生命周期**：
  - 机械态（自动，worker 更新）：`starting`→`running`↔`idle`→`suspended`。
  - 语义终止态（人/调度器管理）：`done`（手动归档）、`stopped`（中途结束）、`failed`（出错）。
  - `done` 经 `mark_done` 工具 + `/done` 命令；默认只在明确指示时触发，自主归档走「提议+确认」。
  - 终止任务默认不自动恢复（可显式 `resume_task`）；`suspended` 才「回复即无缝恢复」。历史留最近 N（+ `/clear`）。
- **交互落点**：话题回复 → `thread_root_id` → Task → 它的 Session（挂起先 `load_session`）；主线 → 调度器 → 按 `task_id` 操作 Task。

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

**进展**：✅ **Phase 1 已实现（2026-07-18）**——`store.py` 的 `Task` + `TaskStore`（落盘
`tasks.json`，task_id 短自增永不复用），status 生命周期（starting/running/idle/suspended +
done/stopped/failed），`/stop` 改为标记 stopped 保留历史，恢复走 `by_thread → Task`，终止任务
不自动恢复，`_list_agents` 与调度器状态改读任务台账，历史保留 keep_terminal + clear_terminal。

✅ **Phase 2 已实现（2026-07-20）**——调度器工具扩到 7 个，新增 `list_tasks`/`get_task(id)`/
`send_to_task(id, msg)`/`resume_task(id)`/`mark_done(id)`（`list_agents` 更名 `list_tasks`），
彻底修掉「调度器只能新建」的缺陷（见下节）；新命令 `/done`（话题内标 done 归档）、`/clear`
（清终止历史）。`/done` 与 `mark_done` 共用 `_finish_task`（有活跃 worker 走 None 哨兵优雅收尾、
`terminate_status` 决定 stopped/done；无活跃则直接改台账）；恢复逻辑收敛到 `_try_resume`
（check→`_launch` 之间无 await 防 TOCTOU），`_launch(first_prompt=None)` 支持「仅拉起在线不跑
首轮」。system prompt 明确「新建 vs 操作已有」。测试 118→135。

✅ **审计 A 已实现（2026-07-20）**——ACP `tool_call` → `Task.actions` 动作日志，`get_task` 带
`recent_actions`，新增 `/task <id>` 人读命令。详见下文「调度器审计 A」。测试 135→148。

**做法**：把现有 `sessions.json` 的 SessionRecord 升级成完整 **Task**（任务 ≈ session ≈ 话题，1:1）：
- 字段：`task_id`（稳定 id）、`project`/`agent`/`description`（当初的自然语言需求）、
  `status`（pending/running/idle/done/stopped/failed/suspended）、`created_at`/`updated_at`、
  `thread_root_id`、`session_id`、（审计）动作日志/摘要。
- 调度器工具（取代偏薄的 `list_agents`）：`list_tasks`、`get_task(id)`，可选 `stop_task`、
  `send_to_task(id, msg)`。
- **行为改动**：`/stop` 从「删记录」改为「标记 `status=stopped` 保留历史」（否则调度器还是
  忘了已停的任务）；恢复只对可恢复状态生效。完成/停止任务留最近 N 个（+ 可 `/clear`）。
- **一举多得**：解决遗忘 + 统一 session 恢复记录 + 是审计（A）的落脚点（动作日志挂 task 上）。

### 调度器只能新建、不能操作已有 agent（✅ 已修 2026-07-20，任务系统 Phase 2）

**问题**：调度器现在的工具只有 `spawn_agent`（新建），缺两项对**已有** agent 的操作：
- **恢复已有（挂起/可恢复）的 agent**——主线说「把 brick-blast 那个恢复一下继续做 X」，它只能
  再新建一个**全新 session（丢上下文）**，而不是 `load_session` 接回原来那个。
- **向在跑的 agent 发消息**——主线说「让 brick-blast 的 agent 先跑测试」，它没法把消息路由进那个
  话题，只能又新建一个。

**后果**：主线里任何针对「已有任务」的指令，调度器都只会**再 spawn 一个新 agent** → 重复 agent、
丢上下文、话题也乱。（当前只有「用户亲自在话题里回复」才能续/恢复已有 agent；调度器够不着。）

**解法（正是任务系统带来的）**：任务有稳定 id 后，给调度器加对已有任务的工具：
- `send_to_task(task_id, message)`——把消息入队给该任务的 agent（在跑就直接发；挂起就先
  `load_session` 恢复再发）。这也覆盖上文 roadmap 的 `send_to_agent`。
- （可选）`resume_task(task_id)`——显式恢复一个挂起任务。
调度器据此能「认领并操作已有任务」，而不只是不断新建。这是任务系统要一并解决的核心能力之一。

### 调度器审计 A（事后审计）✅ 已实现 2026-07-20

记录每个 task 的**动作日志**——agent 调了哪些工具（编辑哪些文件、跑什么命令）。实现：
`acp_client.py` 的 `session_update` 旁路 `_extract_action`（只认 `tool_call` 首次通告，取
`kind`+`title`），经新的 `on_action` 回调（与 `on_output` 并列）送到 `daemon._launch` 的闭包，
`store.add_action(task_id, {turn, kind, title})` 挂到 `Task.actions`（落盘 tasks.json，单 task 上限
`_MAX_ACTIONS=200` 丢最旧）。turn = 进行中的回合号（已完成 turns + 1）。load_session 重放历史时
`set_suppress(True)` 期间不记（放在 suppress 之后）。**读取**：`get_task(id)` 加 `action_count` +
`recent_actions`（末 30）；人读入口 `/task <id>` 命令（无需 LLM）列详情 + 末 15 条动作。

**未做（后续增强）**：① `tool_call_update` 的完成/失败状态（挂 tool_call_id 匹配，标 ✅/❌）；
② 动作里带文件路径（`tool_call.locations`）；③ 写透式每 tool_call 刷盘，chatty agent 可批量化。

（**B = 事前审批**：破坏性操作前飞书卡片按钮确认，替换现在「全自动放行」——更重、涉安全，单开一条线。）

### 挂起任务 send_to_task 不自动恢复（✅ 已修 2026-07-20）

**现象**：主线让调度器给一个**已挂起**的任务发消息，它不会自动恢复；得先让调度器显式
`resume_task` 才能把消息加进去。

**诊断**：**代码本就是对的**——`daemon._sched_send_to_task` 对非活跃且非终止（= 挂起/idle）
的任务本就会走 `_try_resume`（`load_session` 恢复）再把消息作为首轮（`test_send_to_task_
resumes_suspended_task` 已证）。根因在**调度器 LLM 的工具选择**：`SYSTEM_PROMPT` 原写「恢复一个
挂起或已结束的任务 → resume_task」，LLM 看到任务 suspended 就先调 resume_task，不知道
send_to_task 会自动恢复。

**修复**（`scheduler.py`，纯 prompt/工具描述澄清，无逻辑改动）：`SYSTEM_PROMPT` 与 send_to_task
/resume_task 工具描述都改为明确——「给**挂起**任务发消息直接用 send_to_task（它自动 load_session
恢复），resume_task 只用于①不带消息只想让 agent 上线，或②恢复**已终止**（done/stopped/failed）
任务」。代码路径无需改（既有行为测试已覆盖）。

### 显示 code agent 当前使用的模型（✅ 已实现 2026-07-20，opencode 可见 / copilot 不暴露）

**目标**：在飞书里看到某任务的 agent 当前用的是哪个模型。

**调研结论**：ACP 协议 / `acp` SDK **没有标准的「当前模型」字段**（`initialize`、`new_session`/
`load_session` 响应、13 个 `session_update` 变体、所有方法里都没有 model）。但 agent 可自定义塞进
`config_options`。**运行时抓包实测**（`scripts/capture_acp_meta.py`，起真 agent dump `new_session`）：
- **opencode**：`new_session` 响应 `config_options` 里有一项 `id=="model"` / `category=="model"`
  的 select，`current_value` 即当前模型（实测 `ns-deepseek/deepseek-v4-pro`）。另外还暴露 `effort`、
  `mode` 两项 select。**→ 可拿到。**
- **copilot**：`config_options` 只有 `mode` / `agent` / `allow_all`（+ `modes` 是 Agent/Plan/
  Autopilot 行为模式，非模型）。**没有 model 项 → 拿不到，协议层不暴露。**
- 两者在一次普通 turn 的 `session/update` 流里都**不重发**模型（无 model 相关变体），故只在
  `new_session`/`load_session` 时读一次。

**实现**：`acp_client._extract_model(resp)` 从 `config_options` 找 `id`/`category=="model"` 取
`current_value`；`AcpAgent` 在 `start()`（new_session 与 load_session 两条路径）都采集、`.model`
暴露。`Task.model` 落台账（worker 启动成功后 `store.update(model=...)`）。展示：agent 就绪消息带
「（模型：X）」、`get_task` 返回 `model`、`/task` 列「模型: X」。copilot 无则一律留空、不显示。

**后续可选**：切模型（`set_config_option(config_id="model", ...)`）、也展示 effort/mode、
订阅 `config_option_update` 跟踪运行中切换。

### 待实现：调度器工具调用实时卡片显示（2026-07-20，最高优先）

**目标**：主线上跟调度器对话时，像 code agent 话题那样，把调度器**正在调用的工具**（`spawn_agent`
/`send_to_task`/`mark_done`…）用一张**活卡片实时显示**出来（工具名 + 参数 + 返回），而不是像现在
只在最后甩一句文字。

**动机（双重）**：① 体验——让「控制塔」在干什么可见、有反馈；② **直接缓解「说了没做」幻觉**——
调度器 LLM 有时只回「已新建 t3」却没真调 `spawn_agent`（已用 `daemon.log` 抓实，见下条）。工具调用
上卡片后，**没有卡片冒出来 = 它在放空炮**，用户当场可见，不必事后翻日志。

**现状与落点**：现在 `daemon._dispatch_nl` 跑 `run_tool_loop`，只把**最终文本**经 `_reply_user`
（`bridge.reply`，不建话题）发出；中间工具调用只进 `daemon.log`（诊断日志）。卡片机制已有
（`livecard.py` 的 `LiveCard` + `card.py` 的 `build_card`），但目前只服务于 agent 话题（`reply_card`
/`patch_card`）。要做的大致是：
- `run_tool_loop` 加一个 `on_tool_call(name, args, result)` 回调（现有的诊断 `logger.info` 挂点即
  同一处），daemon 用它把每步喂进一张主线活卡片。
- **约束（勿回退）**：调度器回复**不建话题**（`reply_in_thread=false`）。需确认 `reply_card` 回主线
  消息**不会**建话题（livecard 现在用在 agent 话题；主线卡片的建/patch 路径要走通、且不 thread 化）。
- 卡片内容：逐条「🔧 spawn_agent(brick-blast, …) → 已建任务 t3」，末尾附最终文本 + 状态灯。
- 未配 `[llm]`（无自然语言派发）时不涉及。

### 待记：Claude Code 作为第三种后端（2026-07-20）

**目标**：`[agents]` 里除 copilot/opencode 外，再支持 **Claude Code** 作为 ACP agent（按项目
`default_agent` 选用）。daemon 本就 agent 无关（按 argv 启动 ACP 子进程），若 Claude Code 说 ACP，
理论上只是加一条 `[agents]` 配置。

**开放问题（需先验证）**：`claude` CLI 是否**原生**通过 ACP（stdio JSON-RPC）暴露？还是需要一个
**ACP 适配器**（社区有 `claude-code-acp` 之类把 Claude Code 包成 ACP agent server）。用
`scripts/capture_acp_meta.py` 起来 dump 一下握手/`new_session` 就能定性（同当初查 model 的做法）。
确认后：加配置、跑冒烟（握手/流式/`load_session` 支持度）、补 agent 能力元数据。

### 待修复：调度器「说了没做」幻觉（2026-07-20 用 daemon.log 抓实，暂缓）

**现象/根因**：调度器 LLM（deepseek-v4-pro 走代理端点）**工具调用不稳定**——有时只回文字
「已新建 t3 / 已发送」却**根本没调 `spawn_agent`/`send_to_task`**。`daemon.log` 实证：
`调度器收尾（无工具调用）: 已新建 t3…`，且 `tasks.json` 里 `seq=2`、根本没有 t3（那次是空炮，真正的
任务是**之后**另一轮才建的）。这就是用户体感「两次生效一次」的根因。**不是代码 bug**——
`spawn_agent`/`send_to_task` 本身都对，是模型层「narrate 而不 call」。

**修复方案（A+B，暂缓在「工具卡片」之后）**：
- **A（prompt 硬约束）**：`SYSTEM_PROMPT` 加铁律——凡要执行的动作**必须真的调用对应工具**；
  工具没调=什么都没发生；**绝不声称做了其实没做的事**。降低发生率。
- **B（最后一道闸）**：`run_tool_loop` 追踪本轮调过哪些工具；`_dispatch_nl` 检测「本轮没调任何
  **变更类**工具（spawn/send/resume/mark_done），但最终回复里出现『已新建/已派发/已发送…』完成
  声明」→ 不把这句假话原样转给用户，改发「⚠️ 我可能只是嘴上说了没真执行，请 /agents 核对或再说
  一次」。兜住漏网、至少不骗人。keyword 命中宁宽勿漏（误报只是多一句提醒，漏报=退回原 bug）。
- **注意**：「工具卡片显示」落地后，幻觉在卡片上已当场可见，B 的价值下降但仍值得（把假话拦在回复里）。
- （C = 换更强 tool-calling 的模型/端点，是用户配置层面的事，最治本但不在代码内。）

### 其他已考虑方向（roadmap，待排期）

- **`send_to_agent` / `send_to_task`**：主线一句话路由进某个在跑的 agent 话题，不用手动切过去。
- **`register_project`（项目自注册）**：对话式注册新项目并落盘，免去改 config + 重启。
- **权限审批 B（安全）**：见上，替换 auto-allow-all，单开线。
- **P1 多 agent 并发 + worktree 隔离**：同项目并行的文件隔离（跨项目并发已可用）；见上文 P1。
- **per-turn 取消**：ACP `session/cancel`，agent 跑偏时只停这一轮、不杀整个 agent。
- ~~**完成通知带摘要**~~：✅ 已实现 2026-07-20——`Task.last_output`（每轮 agent 收尾 message，
  截断 800）落台账，🔔 通知带一行摘要，`get_task`/`/task` 展示。（后续可选：per-turn 历史输出。）
- **自动触发降噪**：非命令 root 消息静默/仅 `/help` 给用法（见上文「无需 @」）。
- **对话记忆可配**：`[llm]` 加 `memory_rounds`（默认 12 轮）。

**优先级读法（我的建议）**：近期最高价值 = **任务系统 → 审计 A**（连着，任务系统是审计地基，
且直接解决「调度器忘任务」）；中期 = register_project / send_to_task / per-turn 取消 / 通知摘要；
较大或单开线 = 权限审批 B、CLI↔ACP 交接、P1 并发+worktree。
