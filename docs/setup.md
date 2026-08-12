# 飞书应用配置指南

从零把 feishu-dispatcher 跑起来的完整步骤。跑起来之后日常怎么用，见 [usage.md 使用手册](usage.md)。

从整体上看，初始化包含三个部分：**飞书机器人**、**飞书群聊**、**配置文件**。

- **飞书机器人**——提供 App ID 和 App Secret，供我们的应用连接飞书。
- **飞书群聊**——作为主要的对话场所，让我们和机器人在其中对话、分配话题（thread）。
- **配置文件**——记录机器人的 App ID/Secret 和群聊的 chat_id，daemon 据此知道连哪个应用、管哪个群。

下面按这个顺序来。

## 前置环境

本机装好 [`uv`](https://docs.astral.sh/uv/)，以及至少一个 coding agent CLI（npm 全局）——都经 ACP 协议控制、本地实测握手/流式/会话恢复通过：

- **Copilot CLI**：`copilot` 已登录过 GitHub 账号。冒烟 `uv run python scripts/smoke_acp.py`。
- **OpenCode**：`opencode` 已配好 provider/凭据（`opencode providers`）。冒烟 `uv run python scripts/smoke_opencode.py`。
- **Claude Code**：无原生 ACP，经社区适配器接入——`npm i -g @agentclientprotocol/claude-agent-acp` + `claude` 已登录。详见 [claude-code-backend.md](claude-code-backend.md)，冒烟 `uv run python scripts/smoke_claude.py`。
- **Cline**：`cline` v3.0.47+ 原生带 `--acp`，`cline auth` 登录某 provider。冒烟 `uv run python scripts/smoke_cline.py`。
- **Codex CLI**：无原生 ACP，经社区适配器接入——`npm i -g @agentclientprotocol/codex-acp` + `codex login`（或在 `~/.codex/config.toml` 配好自定义 provider/model，如 deepseek 等 OpenAI 兼容端点）。**Windows 注意**：适配器自带的 codex 常缺原生二进制，需用 `[agents.codex]` 表形式设 `CODEX_PATH` 指向本机全局 codex（`npm i -g @openai/codex`）——见 [config.example.toml](../config.example.toml)。普通任务使用默认受限 profile；review/subagent 任务见下节。冒烟 `uv run python scripts/smoke_codex.py`。

在 `config.toml` 的 `[[projects]]` 里用 `default_agent` 指定每个项目由哪个 agent 处理（agent 名须在 `[agents]` 里配过）。

### Codex review / subagent 权限

`codex-acp` 默认使用 `INITIAL_AGENT_MODE=agent`：按需审批、`workspace-write` 沙箱且禁止网络。普通 coding 任务应保留这个安全默认；但实测 Codex 的 `/review`、`/review-commit` 和 subagent 类任务可能在该沙箱中永久等待，既没有流式事件，也不会返回本轮结果。

需要这些能力时，单独配置一个 full-access profile，而不是放宽普通 `codex`：

```toml
[agents.codex-full-access]
command = ["codex-acp"]
env = { CODEX_PATH = "codex", INITIAL_AGENT_MODE = "agent-full-access" }
```

运行时显式选择它：

```text
/run <项目名> <任务描述> --agent codex-full-access
```

> **安全警告**：`agent-full-access` 等价于 `approval=never` + `danger-full-access`。Codex 可以联网、运行命令并修改工作区外文件，且不会经过逐次权限审批。只对可信仓库和可信任务使用，不建议将它设为项目的 `default_agent`。

## 获取代码

```powershell
git clone https://github.com/WSQS/feishu-dispatcher.git
cd feishu-dispatcher
uv sync                                  # 装依赖
```

后续步骤都默认你在仓库根目录下操作。

## 飞书机器人

在飞书后台造一个能收发消息的机器人。这一节做完，机器人就「存在且有能力」（能登录、有权限、已发布）。

1. 打开 [飞书开发者后台](https://open.feishu.cn/app)，创建**企业自建应用**（长连接模式只支持自建应用，商店应用不行）。
2. 「应用能力」→ 添加**机器人**能力。
3. 记下「凭证与基础信息」里的 **App ID** 和 **App Secret**——待会儿填进 `~/.feishu-dispatcher/config.toml`（见「配置文件与启动」）。
4. 「权限管理」中开通：

   | 权限 | 用途 | 说明 |
   |---|---|---|
   | `im:message.group_msg` | 接收群聊中**所有**用户消息 | **必须**。默认机器人只收 @ 它的消息，而话题内回复不会 @ 机器人 |
   | `im:message` | 以机器人身份发送消息 | 发状态/转发 agent 输出 |

   个人 tenant（你自己注册的飞书账号）可自行审批；企业 tenant 的 `group_msg` 属敏感权限可能需管理员审批。

5. 开完权限后**创建版本并发布**（权限发布后才生效）。

> 长连接事件订阅**不**在这一节——飞书后台保存订阅方式时会校验「本地客户端已连上」，而那个客户端是「配置文件与启动」里跑起来的 `start --discover`。所以订阅放到那一节的后半段，等本地能连上了再回后台点。

## 群

建个群、把机器人拉进去，给它一个工作场所。

建**普通群**即可（不要建「话题形式群」）——运行时 daemon 会用 `reply_in_thread` 在根消息下建话题（群主线 = 控制台，话题 = agent 子会话），心智模型见 [usage.md](usage.md)「控制台 vs 话题」。

1. 飞书客户端建一个普通群（只有你自己即可）。
2. 群设置 → 群机器人 → 添加机器人 → 选你的应用。

## 配置文件与启动

把前两节拿到的东西（App ID/Secret、群）接进 daemon，连上飞书、拿到群 id、正式启动。这一节内部有严格的先后——`chat_id` 得先 discover 才能拿到，而 discover 得先有 App ID 才能连上。

### 1. 复制模板、填 App 凭证（chat_id 暂留空）

```powershell
mkdir ~/.feishu-dispatcher
cp config.example.toml ~/.feishu-dispatcher/config.toml
```

填入「飞书机器人」里拿到的 `app_id` / `app_secret`，`chat_id` 先留着空（下一步 discover 拿）。

### 2. discover 连上飞书

```powershell
uv run feishu-dispatcher start --discover
```

发现模式允许 `chat_id` 为空启动，只把收到的消息打印到日志，不执行任何命令。此时 daemon 已作为一个客户端连上飞书——**先别关它**，下一步要用这个连接去后台保存订阅。

### 3. 回开发者后台配长连接订阅

此时本地客户端已连上，保存订阅方式能通过飞书的校验：

1. 开发者后台 →「事件与回调」→「事件配置」→ 订阅方式改为**使用长连接接收事件**，保存。
2. 「添加事件」→ 订阅 `im.message.receive_v1`（接收消息），并授予其要求的权限。

长连接为纯出站 WebSocket，无需公网地址、无需 encrypt key / verification token。约束：每应用最多 50 个连接；事件须 3 秒内处理完（daemon 已即时 ACK + 异步处理，满足）；集群模式下多实例只有随机一个收到事件——**只跑一个 daemon 实例**。

### 4. 拿 chat_id

回到「群」里建的那个群里随便发条消息，discover 进程的日志会打印：

```
[discover] chat_id='oc_xxx' sender_id='ou_xxx' — 填入 config.toml 的 chat_id 即可
```

### 5. 回填 config

把 `chat_id` 填进配置；建议同时把自己的 `ou_xxx` 填进 `sender_whitelist`（否则群里任何成员都能指挥 daemon）。`[[projects]]` 按需增改。然后停掉 discover（Ctrl+C）。

### 6. 正式启动

```powershell
uv run feishu-dispatcher start        # 前台运行；-v 出调试日志
```

初始化到此结束。群里最常用的几个（**完整命令、心智模型、排障 FAQ 见 [usage.md](usage.md)**）：

| 操作 | 效果 |
|---|---|
| `/run <项目名> <任务描述>` | 启动 agent，在该消息下建话题，流式输出回话题 |
| 话题内直接回复 | 追加指令（排队串行执行，同一 session 保留上下文） |
| 话题内 `/cancel` \| `/stop` \| `/done` | 停当前轮保留 agent \| 停并结束 \| 归档 |
| `/agents` / `/task <id>` | 列出任务 / 看某任务详情与动作日志 |
| `/project add <名> <agent> <路径>` | 运行时注册新项目（不用改配置重启） |

**重启恢复**：daemon 重启后（崩溃/升级/重开机），在旧 agent 话题里直接回复即可——daemon 会自动 `load_session` 恢复该会话的上下文继续对话；`sessions.json` 记录随之维护。若会话已在 agent 侧过期或 agent 已从配置移除，会明确提示你 `/run` 重开（不再石沉大海）。

## 自然语言派发（可选，配了 LLM 就能直接说人话）

配了 `[llm]` 后，群里**不用 `/run`、直接用自然语言说需求**，调度器 LLM 会识别项目并派 agent（如「帮 feishu-dispatcher 加个深色模式」），还支持追问 / 修正 / 指代（记着最近几轮对话）。任何 OpenAI 兼容端点均可，可直接照抄 `~/.config/opencode/opencode.json` 里的 provider 配置：

```toml
[llm]
base_url = "https://api.deepseek.com/v1"
api_key = "sk-..."
model = "deepseek-chat"
```

不配则自然语言消息回退到「用法」提示，`/run`/`/agents`/`/stop` 照常。冒烟：`uv run python scripts/smoke_llm.py "你的需求"`。

## 已知约束

- **群内限频 5 QPS**（群里全部机器人共享，全应用 50/s）：单 agent 的 500ms 节流窗口 ≈ 2 msg/s 没问题；多 agent 并发共享此额度，`max_agents` 默认 7 是配套上限（配 `feishu_qps` 令牌桶兜底）。撞限流时 HTTP 层会自动退避重试（尊重 Retry-After）。
- **文本消息上限 150KB**：节流器单批 4000 字符，远低于上限。
- **消息重推**：飞书对 ACK 异常/超时的事件会重推，daemon 已按 `message_id` 幂等去重。
- **在途 turn 不恢复**：重启时正好在跑的那一轮（未完成的 prompt + 排队指令）无法恢复，只恢复会话上下文——重启后重发那条指令即可。
- **ACP 冒烟**：`uv run python scripts/smoke_acp.py`（copilot）/ `scripts/smoke_opencode.py`（opencode）；`scripts/smoke_resume.py` 验证 load_session 跨进程恢复。

## 后续可选优化（来自调研，未实现）

- **话题形式群**（`group_message_type: "thread"`）可由 API 直接创建（机器人自动入群当群主），事件带 `thread_id` 可做更稳的路由——如果普通群方案路由不可靠可切换。
