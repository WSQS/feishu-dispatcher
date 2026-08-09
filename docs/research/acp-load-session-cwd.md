# ACP `load_session` 与 cwd / 工作区绑定

> wayfinder research for [#230](https://github.com/WSQS/feishu-dispatcher/issues/230)；map [#218](https://github.com/WSQS/feishu-dispatcher/issues/218)。
> 一手来源：本仓库源码、ACP v1 schema / session-setup、additionalDirectories RFD、opencode 公开缺陷。未跑真机换 cwd 冒烟（协议与后端证据已足够定不变量）。

## 结论（推荐不变量）

**一旦 Task 拥有可恢复的 `session_id`，`Task.workspace` 在该 Task 的可恢复生命周期内不可变。**

- 不能把已挂起 / 可恢复 Task 的 workspace「迁回」项目根、或改到另一 worktree 路径后再 `load_session` 指望接回原会话。
- 换路径 = 放弃该 `session_id`，走 `new_session`（丢 ACP 对话上下文），或继续使用原路径。
- 同路径 `load_session`（含「创建时就是 worktree 路径、挂起后再用同一 worktree 路径恢复」）是协议与本仓库现状都支持的路径。

---

## 1. 本仓库实际传参

### `AcpAgent.start()`（`feishu_dispatcher/acp_client.py`）

- 进程：`spawn_stdio_transport(..., cwd=self._spawn.cwd)`
- 新建：`self._conn.new_session(cwd=self._spawn.cwd)`
- 恢复：`self._conn.load_session(cwd=self._spawn.cwd, session_id=self._resume_session_id)`
- 即：**子进程 cwd 与 ACP session cwd 始终同一字符串**，来自 `AgentSpawn.cwd`。

### daemon 接线（`feishu_dispatcher/daemon.py`）

- `/run` / `spawn_agent` 建 Task：`workspace=str(project.path)`（尚无 worktree 写入）。
- `_launch`：`AgentSpawn(..., cwd=task.workspace, ...)` + 可选 `resume_session_id=task.session_id`。
- `_try_resume` / 话题惰性恢复 / `send_to_task` / `resume_task` / bg 唤回挂起 Task：一律 `_launch(..., resume_session_id=task.session_id)`，cwd 仍是 **落盘的 `Task.workspace`**。
- 冒烟 `scripts/smoke_resume.py`：phase1 `new_session` 与 phase2 `load_session` **强制同一 `CWD`**。

### SDK 签名（venv `acp`）

`ClientSideConnection.load_session(cwd, session_id, mcp_servers=None, additional_directories=None)` —— cwd 为必填位置参数；本仓库不传 `additional_directories`。

---

## 2. 官方 ACP：session 绑定创建时的 cwd

### Working Directory（[session-setup](https://agentclientprotocol.com/protocol/v1/session-setup)）

`cwd`：

- **MUST** 为绝对路径；
- **MUST** 作为该 session 的主文件系统上下文（与 agent 子进程从哪 spawn 无关）；
- **MUST** 作为相对路径解析基；
- **MUST** 属于 session 的 effective root set。

`session/new`、`session/load`、`session/resume` 均要求带 `cwd`。

### `LoadSessionRequest.additionalDirectories`（[schema v1](https://github.com/agentclientprotocol/agent-client-protocol/blob/main/schema/v1/schema.json)）

非空 `additionalDirectories`「可以与此前列表不同」——**前提是 request `cwd` matches the session's `cwd`**。  
同一措辞反复出现在 [additional-directories RFD](https://agentclientprotocol.com/rfds/additional-directories)。

推论（协议层）：

- **允许变的是附加根，不是主 `cwd`。**
- 协议未单独写「cwd 不一致必须返回某错误码」，但把「cwd 匹配」写成变更附加根的前置条件；`SessionInfo` **required: `sessionId` + `cwd`**，说明 cwd 是 session 身份的一部分。
- Client 正确用法：`load` / `resume` 时回传**该 session 原 cwd**。

### 本仓库接入后端（文档/冒烟层面）

CLAUDE.md / design：copilot、opencode、cline、claude-agent-acp、codex-acp 均本地冒烟过 `load_session`（`smoke_resume.py` 等同 cwd）。**无「换 cwd 再 load」的官方保证。**

---

## 3. 后端实证：换 cwd 的 `load_session` 不安全

[opencode#31964](https://github.com/anomalyco/opencode/issues/31964)（OPEN）：`session/new` 于 dir A → `session/load` 同 id 于 dir B 后进入 split-brain——工具仍按**创建时 cwd** 判「外部目录」、permission reply 被丢、`session/prompt` 挂死。作者亦承认：要么真正 rebind，要么 **load 时拒绝换 cwd**；现状是最差的静默损坏。

相关：[opencode#6697](https://github.com/anomalyco/opencode/issues/6697) 等——session 执行上下文与存储 directory 不一致的系列问题。

因此对「换到 worktree 路径再接回」：

| 场景 | 能否指望接回 |
|------|----------------|
| `new` @ worktree，`load` @ **同一** worktree | 是（与现有恢复路径一致） |
| `new` @ 项目根，`load` @ worktree（或反向） | **否**——协议要求 cwd 匹配；opencode 不拒但会坏 |

未对每个后端做换 cwd 冒烟；对 P1 决策，协议不变量 + 主用后端（opencode）已公开坏路径，足够排除「迁 cwd 再 load」。

---

## 4. 对本项目的推论（worktree）

1. **写入 `Task.workspace` 的路径 = ACP session 的 cwd。** 恢复时 daemon 已把该值原样传给 `load_session`；要接回就必须磁盘上仍是该路径（worktree 未删/未改挂）。
2. **不可**在保留 `session_id` 的前提下把挂起 Task「迁回」`project.path`。若产品要「并发结束后回到项目根」，只能：新 session，或根本不迁 cwd（isolation 策略另票 HITL）。
3. HITL「首个 agent 是否迁出项目根」（map 子票）受此约束：对**已有** session，迁出 ≠ `load_session` 换 cwd；要么创建时就进 worktree，要么迁出时放弃旧 session。

---

## 5. bg job 的 `cwd` vs `Task.workspace`

- `fdx bg run`（`agent_cli.py`）**不传 cwd**；控制面 `POST /v1/bg/run`：`cwd = body.cwd or task.workspace or "."`（`daemon.py`）。
- `Job.cwd` 落盘的是**该 job 启动时**解析出的目录。
- 一致性要求：
  - **默认**已与 `Task.workspace` 对齐——worktree 写入 Task 后，未显式覆盖的 bg job 落在 worktree。
  - API 允许 body 覆盖 cwd；隔离语义下应视「跑在别的树」为 agent 显式选择，**不**改变「resume 必须复用 `Task.workspace`」不变量。
  - 挂起/恢复不影响已在跑的 job（daemon 拥有进程）；唤回 prompt 仍挂到原 Task，agent 恢复时 cwd 仍是 `Task.workspace`。

---

## 完成判据答复

> workspace 路径在 task 生命周期内是否不可变？

**对「仍要用同一 `session_id` 做 `load_session` 恢复」的区间：是，不可变（事实约束，非口味）。**  
终止且不再恢复、或主动丢弃 session 另开 `new_session` 时，路径策略可由产品另定，但不在本票「接回」语义内。
