# Agent Zone

本仓库当前将 `feishu_dispatcher/agent/` 视为唯一实现区（Agent Zone）。

## 范围

- `feishu_dispatcher/agent/` 包含当前全部 Python 实现、Channel、Session、Store、ACP、CLI、控制面和 WebUI 源码。
- 根目录 `feishu_dispatcher/__init__.py` 仅作为包容器，不提供旧模块的兼容导入。
- `tests/`、`scripts/`、构建配置和文档用于验证、运行或描述 Agent Zone；它们不是稳定业务契约。

## 当前治理判断

这次迁移不声明任何现有业务接口已经提升为稳定契约。`agent/` 中的模块、类、函数和数据结构都可以在后续治理阶段重新划分、替换或删除。

迁移只改变 ownership 和 import 路径，不改变产品行为。后续若要提升稳定契约，必须单独明确：

- 输入、输出和错误语义；
- 生命周期与关闭顺序；
- 持久化或外部兼容性影响；
- 自动化与真实环境验证；
- 由谁承担长期维护；
- 回滚和 promotion 方式。

## 依赖方向

当前阶段允许 Agent Zone 内部自由重组，但不得通过旧路径重新导出兼容 facade。后续划定稳定契约时，候选实现应依赖稳定契约；稳定契约不得反向依赖候选实现。

## 修改与验证

Agent Zone 的后续修改仍需遵守 on-write / on-submit，并运行仓库定义的测试、Ruff、Pyright、WebUI 和构建检查。目录迁移不等于 promotion，也不自动赋予任何接口稳定性。
