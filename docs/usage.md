# 使用手册

daemon 跑起来之后，日常怎么用。第一次配置见 [setup.md](setup.md)。

## 先理解两个地方：控制台 vs 话题

这是唯一需要记住的心智模型：

- **群主线 = 控制台**。你在群里直接发的消息（不在任何话题里）都是对 daemon 说的：派任务、查状态、管项目。
- **话题 = 某个 agent 的专属会话**。每次派任务，daemon 会在你那条消息下建一个话题。**话题里的每条回复都是发给那个 agent 的**——它记得整个上下文，你可以一直追问、纠正、让它继续。

> 一句话：**在群里说话是指挥调度器，在话题里说话是指挥某个 agent。**

顺带几个词：
- **项目（Project）**：一个代码仓库 + 用哪个 agent 处理，在配置或 `/project add` 里登记。
- **任务（Task）**：一次 `/run` 就是一个任务，有短 id（`t1`、`t2`…），daemon 落盘记着它，重启也不丢。
- **Agent 后端**：真正干活的 coding CLI（Copilot / OpenCode / Claude Code / Cline / Codex / ZCode）。

## 两种派发方式

### 1. 命令式：`/run`

```
/run <项目名> <任务描述>
```

例：`/run feishu-dispatcher 把 setup.md 里的英文注释翻译成中文`

- daemon 在这条消息下建话题，启动项目默认 agent，输出实时回话题。
- 想临时换个 agent：`/run <项目> <任务> --agent opencode`（该 agent 名须在配置的 `[agents]` 里）。

### 2. 自然语言（需先配调度器 LLM）

配了 `[llm]`（见 setup.md「自然语言派发」）后，**群里直接说人话**即可，不用打 `/run`：

```
帮 feishu-dispatcher 把 README 补个英文版
```

调度器 LLM 会认项目、派 agent，还能**追问 / 修正 / 指代**（"上一个任务改跑 opencode""t3 那个停了吧"），因为它记着最近几轮对话。注意：**主线是在跟调度器聊，话题里是在跟 agent 聊，两层上下文分开**。

没配 LLM 的话，非命令的自然语言消息会回一句用法提示，`/run` 等命令照常。

## 控制台命令（群主线发）

| 命令 | 作用 |
|---|---|
| `/run <项目> <任务> [--agent <名>]` | 派任务给 agent，建话题；`--agent` 单次覆盖项目默认 agent |
| `/agents` | 列出活跃 + 历史任务（异常暂停的任务单列一段） |
| `/task <id>` | 看某任务详情：状态、模型、最近回复、动作日志（改了哪些文件 / 跑了什么命令，事后审计用） |
| `/project` | 列出所有项目（标明配置内置 / 运行时注册） |
| `/project add <名> <agent> <路径>` | **运行时**注册新项目，不用改配置重启 |
| `/project remove <名>` | 删除运行时注册的项目（配置内置的删不了） |
| `/models` | 列出各 agent 已知的可选模型 |
| `/models refresh [agent]` | 主动刷新模型缓存（不带 agent 名则全刷） |
| `/llm` | 列出调度器 LLM 的 profile |
| `/llm <名>` | 运行时切换调度器后端（不改配置重启） |
| `/clear` | 清理已结束任务的历史记录 |
| `/reboot` | 重启整个 daemon（任务会自动恢复上下文） |
| `/help` | 显示控制台用法 |

## 话题内命令（在某个 agent 的话题里发）

| 命令 | 作用 |
|---|---|
| 直接回复 | 给这个 agent 追加指令（排队串行执行，上下文保留） |
| `/cancel` | 停掉当前正在跑的这一轮，但**保留 agent**（回到待命） |
| `/cancel <新指令>` | 停当前轮，停完接着做你给的新指令 |
| `/stop` | 停当前轮并**结束**这个 agent（标记 stopped，历史保留） |
| `/done` | 把这个任务标记完成并归档（done） |
| `/model` | 查看当前模型 + 可切换的模型列表 |
| `/model <名>` | 切换模型（下一轮生效；有的后端如 copilot 不暴露模型则提示不支持） |
| `/raw <指令>` | 把 `<指令>` 原样透传给 agent（比如 `/raw /model` 是让 agent 自己执行它的 `/model`，而不是 daemon 拦截） |
| `/help` | 显示话题内用法 |

**`/cancel` vs `/stop` 的区别**：`/cancel` 是"这轮不干了，但你别走，听我下一句"；`/stop` 是"停下并散会"。被取消的那一轮，已经改动的文件不会回滚。

## 长任务：让 agent 起了训练 / build 不卡住

如果 agent 要跑一个很久的命令（训练、编译、跑测试），它可以用内置的 `fdx` CLI 把长进程交给 daemon 托管，**当轮立刻释放**，不阻塞你继续对话。跑完后 daemon 会**自动把 agent 唤回同一个任务**继续。

这部分主要是 **agent 自己**在用（命令在 agent 的运行环境里执行），你了解一下行为即可：

| agent 侧命令 | 作用 |
|---|---|
| `fdx bg run [--timeout N] -- <命令>` | 起一个后台长任务，立刻返回 |
| `fdx bg list` | 列出本任务起的后台 job |
| `fdx bg logs <id> [--tail N]` | 查某 job 的输出尾部（中途看进度） |
| `fdx bg kill <id>` | 终止一个在跑的 job |

- 后台 job 跑完时，daemon 会先往话题发一条**可见的完成消息**（带输出尾部），再自动唤回 agent 接着干——你在话题里看得到，不会"人间蒸发"。
- 超时兜底：`bg_job_timeout`（配置，默认 0 = 不超时，长训练不砍），或 agent 用 `--timeout N` 单次指定。
- **注意**：v1 不做 daemon 重启穿越——daemon 重启时正在飞的后台 job 会丢。

## 关机重开也能接着聊

daemon 崩了 / 升级 / 重开机之后，**不用重新 `/run`**：直接在旧话题里回复，daemon 会自动 `load_session` 把那个会话的上下文接回来继续。

- 能自动恢复的状态：空闲挂起（`suspended`）、待命（`idle`）、turn 出错卡住（`failed`，多半能接回）。
- **不**自动恢复的：已 `/stop`（stopped）或 `/done`（done）的终止任务——会明确提示你重开，不会石沉大海。
- 重启时**正好在跑的那一轮**（未完成 + 排队的指令）不恢复，只恢复会话上下文——重启后把那条指令重发一次即可。

## 空闲会自动挂起（省资源）

一个 agent 空闲超过 `idle_timeout`（默认 30 分钟）没新指令，daemon 会自动挂起它：关掉子进程腾出并发名额，但**会话记录保留**。你下次在话题里回复，它会自动恢复。想关掉这行为把 `idle_timeout` 设 `<=0`。

## 排障 FAQ

**daemon 起不来 / 报 chat_id 为空？**
`chat_id` 必填。先 `uv run feishu-dispatcher start --discover`，在群里发条消息，日志会打印 `chat_id`，填进 `config.toml`。详见 [setup.md](setup.md)「配置文件与启动」。

**已经有一个 daemon 在跑，第二个 `start` 直接报错？**
这是**单实例锁**在保护你——同时跑两个 daemon 会共用 `tasks.json` / 抢 WS，把台账踩坏。先把旧的停掉（或用 `/reboot` 让它自己重启），再启新的。

**群里发消息 agent 没反应？**
- 确认权限 `im:message.group_msg` 已开并**发布版本**（默认机器人只收 @ 它的消息，话题回复不会 @ 机器人）。
- 确认发消息的人在 `sender_whitelist` 里（配了白名单的话）。
- `-v` 启动看调试日志，确认事件有到、命中了哪条路由。

**想加个新项目，非得改配置重启吗？**
不用。群里 `/project add <名> <agent> <路径>`（或配了 LLM 就直接说"注册一个项目…"）即可运行时登记。

**agent 输出刷屏 / 撞限流？**
默认 `stream_mode = "card"` 是**原地更新一张卡片**，不刷屏。飞书同群 5 QPS 由令牌桶（`feishu_qps`）兜底，多 agent 并发时汇总限速；撞到 429 HTTP 层会自动退避重试。

**模型显示为空 / 切不了模型？**
不是所有后端都暴露标准 ACP 模型配置——OpenCode 会上报具体模型，Copilot / Cline / ZCode bridge 不提供可切换模型列表，`/model` 会提示该后端不支持切换。这是后端能力差异，不是 bug。

**任务状态里的 `failed` 是死了吗？**
不是。`failed` = 某一轮出异常"卡住等恢复"，会话多半还在。话题里再回复一句就会尝试恢复；真的会话过期了才会停在 `failed` 并提示重开。

**怎么彻底重启 daemon？**
群里发 `/reboot`，daemon 会干净关闭（活跃任务标挂起）再用同环境重新拉起，重启完发一条"已重启"回执。

## 还想了解更多

- 从零配置：[setup.md](setup.md)
- 架构与设计决策（偏开发者）：[design.md](design.md)
- Claude Code 后端接入细节：[claude-code-backend.md](claude-code-backend.md)
- ZCode 后端接入细节：[zcode-backend.md](zcode-backend.md)
