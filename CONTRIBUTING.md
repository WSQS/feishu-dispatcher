# 贡献约定

本仓库为个人项目，但也接受自动化 agent（如 ZCode）和未来的协作者提交。本文件固化已形成的协作约定，让每个提交者（人或 agent）在动手前就知道标准，而不是撞墙后才发现。

## Git 协作约定

开 PR 前先读本节。仓库 GitHub 设置已锁死合并方式；不写下来只能撞墙后才发现。

### 合并策略

- **只用 merge commit**（`Merge pull request #N from …`）。仓库设置：`allow_squash_merge = false`、`allow_rebase_merge = false`——squash / rebase 合并会被拒，不要再试。
- PR 上的多个 commit **保留原样**，不要求压成单 commit；历史应可见完整迭代。

### 分支命名

按类型前缀命名（默认从最新 `main` 拉出；大型 feature 的后续切片可从其父 feature 分支拉出）：

| 前缀 | 用途 | 示例 |
|------|------|------|
| `feat/<scope>` | 新功能 | `feat/android-navigation` |
| `fix/<scope>` | bug 修复 | `fix/android-zt-toggle-animation` |
| `refactor/<scope>` | 重构 | `refactor/atomic-write` |
| `docs/<scope>` | 文档 | `docs/viewer-config-example` |
| `test/<scope>` | 测试 | `test/android-jvm-scaffold` |
| `style/<scope>` | 风格清理 | `style/android-comment-cleanup` |
| `perf/<scope>` | 性能 | `perf/android-viewerclient-reuse` |

### Commit message

采用 [Angular 提交信息规范](https://github.com/angular/angular/blob/main/CONTRIBUTING.md#commit)，描述以中文为主（与现有历史一致）：

- 格式：`<type>(<scope>): <subject>`
- `type` 取 Angular 集合：`build` / `ci` / `docs` / `feat` / `fix` / `perf` / `refactor` / `style` / `test`（本仓历史里偶见的 `chore` 视为与 `build`/`ci` 同类运维提交，新提交优先用上表）
- 示例：`feat(android): 导航框架`、`docs(config): 补 [http_channel] 段`
- issue 关联写在 **PR body**（`Closes #N` / `Refs #N`），不要塞进代码注释（见下方「注释规范」）。

### PR 流程

- **默认**从最新 `main` 开分支，PR **base = `main`**。
- **大型 feature / 叠罗汉 PR**：后续切片以**父分支**为 base（合入父 feature，而不是直接合进 `main`）；父分支最终再合 `main`。在 PR 描述里写清依赖（例如「基于 #N / `feat/…`」），避免审阅者按「一律 base=main」误合。
- 标题或 body 带 `(closes #N)` / `(refs #N)`（或 PR body 里的 `Closes #N` / `Refs #N`）。
- 写代码遵循 **[on-write](https://github.com/WSQS/agent-skills/tree/master/skills/on-write)**（单一结果、最小 diff）；提交 / 合入前按 **[on-submit](https://github.com/WSQS/agent-skills/tree/master/skills/on-submit)** 自检（一致性、必要性、无 drive-by）。技能集仓库：[WSQS/agent-skills](https://github.com/WSQS/agent-skills)。

### WebUI 浏览器测试

首次运行先安装 Playwright Chromium：

```powershell
uv run playwright install chromium
```

运行真实浏览器 E2E：

```powershell
uv run pytest -q -m webui_e2e
```

## 注释规范

注释的职责是说明「**是什么 / 为什么这样写**」，让代码自包含可读。

### 应该

- 解释**为什么**这么写——尤其是非显而易见的设计决策、规避的坑、性能/并发考量。
- 说明一段代码**是什么**——复杂逻辑的概括、外部库的作用。

### 不应该

- **带 issue 号引用**（`#122`、`#123`）——issue 引用属于 PR body / commit message，不属于代码注释。注释和外部工单耦合后，工单一旦关闭/迁移，注释就成了悬空引用。
- **带外部决策编号**（`决策 Q2`、`D6`）——和 issue 号同构，读者不看对应的 map/issue 根本不知道 Q2 是什么。
- **写未来实现意图**（「X 会用它」「先带上，后续改」「归 #123」）——实现细节会变，注释写死会绑死。代码演进后这类注释必然过时，却没人回头清理。
- **依赖注释**：只说库作用，不提「给谁用」。

### 理由

注释应该自包含——不依赖外部工单、issue、决策册才读得懂。issue 号和决策编号让注释和外部状态耦合；未来意图会过时。历史上一轮注释清理提交清掉的就是这类反模式。

### 边界：可执行的 TODO

代码里的 `/* TODO: open file content */` 这类**自包含、可执行**的待办标记是允许的——它点明了当前缺什么、下一步做什么，不依赖外部上下文。禁止的是「#123 会用它」这种**指向外部、不可执行**的意图描述。

## 记录与文档约定

决策与实现记录以 GitHub 为家，仓库文档只留稳定内容。架构权威来源是 pinned 的架构 map issue。

### 归口

| 内容 | 归属 |
|------|------|
| 设计决策、方案取舍、暂缓/否决的 rationale | GitHub issue；架构类（总览/概念模型/基础决策）进 pinned 的架构 map，具体决策的裁定现场是对应 issue/PR |
| 实现过程、修复记录、验证证据 | PR body（`Closes #N` / `Refs #N`） |
| 行为契约 | 测试（可执行） |

### 仓库文档只留四类例外

1. 用户操作手册（如 docs/usage.md）
2. 部署运维（如 docs/setup.md、后端接入手册）
3. 对外跨仓库 API
4. 脱离 GitHub 仍须长期保留的知识（本文件与 CLAUDE.md 的协作/操作规范）

### 反模式（不要再做）

- 新增 design.md 类设计文档或调研报告文件——决策进 issue，调研结论贴进对应 issue 的正文/评论
- 在文档里留「✅ 已实现 / 已修 YYYY-MM-DD」时间线——实现记录在 PR body
- 往 README 写能力清单 / roadmap 快照——必然漂移，进展与计划看 issues
- 在代码注释里引用 issue 号、决策编号或未来意图（详见上方「注释规范」）
