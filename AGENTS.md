# Agent Zone

本仓库将候选实现与用户已提升的契约分开治理。

## 范围

- Human Zone 当前仅包含两个已提升的契约文件：`feishu_dispatcher/conversation.py` 和 `feishu_dispatcher/channel.py`。它们的定义、签名和语义只有得到用户针对该变更的明确授权后才能修改。
- `feishu_dispatcher/agent/` 包含具体 Channel、Session、Store、ACP、CLI、控制面和 WebUI 等候选实现，直接引用上述契约，不复制或重新导出旧路径兼容定义。
- 根目录 `feishu_dispatcher/__init__.py` 仅作为包容器，不提供旧模块的兼容导入。
- `tests/`、`scripts/`、构建配置和文档用于验证、运行或描述 Agent Zone；它们不是稳定业务契约。

## 当前治理判断

用户已明确提升上述两个文件；其它模块不因此获得稳定契约地位。`SessionEvent` 仍定义在 `feishu_dispatcher/agent/session_event.py`，本轮不提升、不复制其定义。

迁移只改变 ownership 和 import 路径，不改变产品行为。后续若要提升稳定契约，必须单独明确：

- 输入、输出和错误语义；
- 生命周期与关闭顺序；
- 持久化或外部兼容性影响；
- 自动化与真实环境验证；
- 由谁承担长期维护；
- 回滚和 promotion 方式。

## 依赖方向

当前阶段允许 Agent Zone 内部自由重组，但不得通过旧路径重新导出兼容 facade。候选实现应依赖已提升契约，已提升契约原则上不得反向依赖候选实现。

本轮用户接受的唯一暂时例外：`feishu_dispatcher/channel.py` 的事件参数引用 `feishu_dispatcher.agent.session_event.SessionEvent`。这是尚未消除的类型依赖，不代表 SessionEvent 已提升，也不授权其它反向依赖。

## 修改与验证

Agent Zone 的后续修改仍需遵守 on-write / on-submit，并运行仓库定义的测试、Ruff、Pyright、WebUI 和构建检查。目录迁移不等于 promotion，也不自动赋予任何接口稳定性。
