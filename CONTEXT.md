# Domain Language

## Session Event

Session 在运行期间已经发生的事实。消费者可以投影这些事实，但不能据此反向改变事实本身。

## Agent Output Lifecycle

一个 Agent Turn 从 `Agent Output Started` 开始，到唯一一个 `Agent Output Finished` 结束。成功发布 Started 后，运行时应尽力发布对应的 Finished。

## Interrupted Output

Agent Turn 因宿主或运行时生命周期结束而未获得正常 ACP 结果。它不同于用户主动取消，也不同于 Agent 执行失败。

## Channel

Conversation 的交互边界。Channel 消费 Session Event，并自行管理输出展示的创建、更新、终态与防御性清理。

## ACP Session Runtime

通过 Agent Client Protocol 驱动持久 Worker Session 的运行实例。它同时拥有 Turn 队列、取消/终止语义、当前 Turn 归属和 ACP 资源生命周期；daemon 只通过提交、取消、终止等行为接口协作，不读取这些机械状态。

## Agent Zone

承载尚未提升实现的实验 ownership 区域。当前具体实现在 `feishu_dispatcher/agent/`；ConversationRef 与 Channel 契约已由用户提升，具体范围和依赖例外见 AGENTS.md。目录归属变更不表示产品行为本身发生变化。

## Promotion

将 Agent Zone 中经过验证的行为、接口或数据形状提升为需要长期兼容和维护的契约。Promotion 是独立的治理决定，不等同于文件移动、目录重命名或分支合并。
