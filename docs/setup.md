# 飞书应用配置指南

从零把 feishu-dispatcher 跑起来的完整步骤。前置：本机装好 `uv`，以及至少一个 coding agent CLI（npm 全局）——两者均经 ACP 协议控制、本地实测可用：

- **Copilot CLI**：`copilot` 已登录过 GitHub 账号。ACP 冒烟 `uv run python scripts/smoke_acp.py`。
- **OpenCode**：`opencode` 已配好 provider/凭据（`opencode providers`）。ACP 冒烟 `uv run python scripts/smoke_opencode.py`。

在 `config.toml` 的 `[[projects]]` 里用 `default_agent = "copilot" | "opencode"` 指定每个项目由哪个 agent 处理。

## 1. 创建飞书应用

1. 打开 [飞书开发者后台](https://open.feishu.cn/app)，创建**企业自建应用**（长连接模式只支持自建应用，商店应用不行）。
2. 「应用能力」→ 添加**机器人**能力。
3. 记下「凭证与基础信息」里的 **App ID** 和 **App Secret**。

## 2. 开通权限（重要）

「权限管理」中开通：

| 权限 | 用途 | 说明 |
|---|---|---|
| `im:message.group_msg:readonly` | 接收群聊中**所有**用户消息 | **必须**。默认机器人只收 @ 它的消息，而话题内回复不会 @ 机器人 |
| `im:message` | 以机器人身份发送消息 | 发状态/转发 agent 输出 |

个人租户可自行审批；企业租户的 `group_msg` 属敏感权限可能需管理员审批。

开完权限后**创建版本并发布**（权限发布后才生效）。

## 3. 配置长连接事件订阅

注意先后顺序——保存长连接订阅方式时，飞书会检查本地客户端是否已连上：

1. 先在本地把 daemon 以发现模式跑起来（见第 5 步，此时 chat_id 可以为空）：
   ```
   uv run feishu-dispatcher start --discover
   ```
2. 开发者后台 →「事件与回调」→「事件配置」→ 订阅方式改为**使用长连接接收事件**，保存。
3. 「添加事件」→ 订阅 `im.message.receive_v1`（接收消息），并授予其要求的权限。

长连接为纯出站 WebSocket，无需公网地址、无需 encrypt key / verification token。约束：每应用最多 50 个连接；事件须 3 秒内处理完（daemon 已即时 ACK + 异步处理，满足）；集群模式下多实例只有随机一个收到事件——**只跑一个 daemon 实例**。

## 4. 建控制台群

当前实现用**普通群**（不是「话题形式群」）：群主线 = 控制台（发 `/run` 等命令），机器人对根消息 `reply_in_thread` 建话题 = agent 子会话。

1. 飞书客户端建一个普通群（只有你自己即可）。
2. 群设置 → 群机器人 → 添加机器人 → 选你的应用。

## 5. 本地配置

```powershell
mkdir ~/.feishu-dispatcher
cp config.example.toml ~/.feishu-dispatcher/config.toml
```

填入 `app_id` / `app_secret`，然后跑发现模式拿群 id：

```powershell
uv run feishu-dispatcher start --discover
```

在群里随便发条消息，日志会打印：

```
[discover] chat_id='oc_xxx' sender_id='ou_xxx' — 填入 config.toml 的 chat_id 即可
```

把 `chat_id` 填进配置；建议同时把自己的 `ou_xxx` 填进 `sender_whitelist`（否则群里任何成员都能指挥 daemon）。`[[projects]]` 按需增改。

## 6. 正式启动与使用

```powershell
uv run feishu-dispatcher start        # 前台运行；-v 出调试日志
```

群里用法：

| 操作 | 效果 |
|---|---|
| `/run <项目名> <任务描述>` | 启动 agent，在该消息下建话题，流式输出回话题 |
| 话题内直接回复 | 追加指令（排队串行执行，同一 session 保留上下文） |
| 话题内发 `/stop` | 结束该 agent |
| `/agents` | 列出活跃 + 可恢复的 agent |

**重启恢复**：daemon 重启后（崩溃/升级/重开机），在旧 agent 话题里直接回复即可——daemon 会自动 `load_session` 恢复该会话的上下文继续对话；`sessions.json` 记录随之维护。若会话已在 agent 侧过期或 agent 已从配置移除，会明确提示你 `/run` 重开（不再石沉大海）。

## 6.5 自然语言派发（可选，P2）

配了 `[llm]` 后，群里**不用 `/run`、直接用自然语言说需求**，调度器 LLM 会识别项目并派 agent（如「帮 feishu-dispatcher 加个深色模式」）。任何 OpenAI 兼容端点均可，可直接照抄 `~/.config/opencode/opencode.json` 里的 provider 配置：

```toml
[llm]
base_url = "https://ai.jiachengyun.com/v1"
api_key = "sk-..."
model = "deepseek-v4-pro"
```

不配则自然语言消息回退到「用法」提示，`/run`/`/agents`/`/stop` 照常。冒烟：`uv run python scripts/smoke_llm.py "你的需求"`。

## 7. 已知约束

- **群内限频 5 QPS**（群里全部机器人共享，全应用 50/s）：单 agent 的 500ms 节流窗口 ≈ 2 msg/s 没问题；多 agent 并发共享此额度，`max_agents` 默认 3 是配套上限。撞限流时 HTTP 层会自动退避重试（尊重 Retry-After）。
- **文本消息上限 150KB**：节流器单批 4000 字符，远低于上限。
- **消息重推**：飞书对 ACK 异常/超时的事件会重推，daemon 已按 `message_id` 幂等去重。
- **在途 turn 不恢复**：重启时正好在跑的那一轮（未完成的 prompt + 排队指令）无法恢复，只恢复会话上下文——重启后重发那条指令即可。
- **ACP 冒烟**：`uv run python scripts/smoke_acp.py`（copilot）/ `scripts/smoke_opencode.py`（opencode）；`scripts/smoke_resume.py` 验证 load_session 跨进程恢复。

## 8. 后续可选优化（来自调研，未实现）

- **话题形式群**（`group_message_type: "thread"`）可由 API 直接创建（机器人自动入群当群主），事件带 `thread_id` 可做更稳的路由——如果普通群方案路由不可靠可切换。
- **卡片流式**：`PATCH /im/v1/messages/:id`（interactive card）单条消息 5 QPS、无编辑次数上限（文本 PUT 编辑上限 20 次），适合做「单条消息原地更新」的流式展示，替代刷屏。
