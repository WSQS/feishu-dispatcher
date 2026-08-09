# Research: Windows 上 `git worktree` 创建/移除/残留回收硬约束

> Wayfinder research for [research: Windows 上 git worktree 创建/移除/残留回收的硬约束](https://github.com/WSQS/feishu-dispatcher/issues/229)  
> Map: [wayfinder map: P1 同项目 worktree 隔离（#37）](https://github.com/WSQS/feishu-dispatcher/issues/218)  
> Date: 2026-08-09 · Host git: `git version 2.54.0.windows.1`  
> Local experiments: temp repos under `%TEMP%\fdx-wt-research-*`（**不是**本仓主工作区）

## Sources

| ID | Source |
|----|--------|
| D1 | Official docs: [git-worktree](https://git-scm.com/docs/git-worktree) (synced with 2.54.0) |
| D2 | Local `git worktree -h` / `git worktree --help` on `2.54.0.windows.1` |
| D3 | Local temp-repo experiments (this session; commands + exit codes below) |
| D4 | Field reports of Windows remove failures (file locks / read-only / CWD-in-tree): [fastled#2610](https://github.com/fastled/fastled/issues/2610), [claude-code#41740](https://github.com/anthropics/claude-code/issues/41740), [arbor@7307cb1](https://github.com/morellodev/arbor/commit/7307cb10cae14acdcc121fa097919f358f2ced76) |

---

## 1. `worktree add` 路径形态与常见失败

### 路径形态（D1 + D3）

| 形态 | 结果（本机） | 备注 |
|------|-------------|------|
| 相对路径 `../wt-rel` | OK | porcelain 列出时规范为绝对路径，且用 `/`（`C:/Users/...`） |
| Windows 绝对路径 `C:\...\wt-abs` | OK | |
| 正斜杠绝对 `C:/.../wt-fwd` | OK | |
| 含空格 `...\wt with spaces` | OK | 管理目录名把空格收成连字符：`.git/worktrees/wt-with-spaces`；链接 worktree 根的 `.git` 文件内容为 `gitdir: <common>/.git/worktrees/wt-with-spaces` |
| 盘符 | 与绝对路径相同，无额外限制 | 未测跨盘网络盘；D1 对「可拔插/网络盘」建议 `worktree lock` 防 prune |
| 长路径 ~309 字符 | **失败** `Filename too long` | 即使 `core.longpaths=true`，更深路径又报 `$GIT_DIR too big`（D3）→ P1 路径应短（sibling 短目录名 + 短 task id） |

默认链接用**绝对路径**（`worktree.useRelativePaths` 默认 `false`，D1）。

命令形状（D1/D2）：

```text
git worktree add [-f] [-b|-B <new-branch>] <path> [<commit-ish>]
```

P1 设计中的 `git worktree add <path> -b <branch> <start-point>` 合法；本机实测等价写法 `git worktree add -b <branch> <path> <start-point>`。

### 常见失败（D1 + D3）

| 场景 | exit / 消息 | 含义 |
|------|-------------|------|
| 目标路径已存在且**非空** | 128 · `fatal: '<path>' already exists` | 拒建 |
| 目标路径已存在且**空目录** | 0 | 允许占用空目录 |
| `-b` 分支名已存在 | 255 · `fatal: a branch named '…' already exists` | `-b` 不覆盖；需换名、先删分支，或用 `-B`（会重置分支 tip，D1） |
| 检出已被另一 worktree 占用的分支 | 128 · `… is already used by worktree at '…'` | 同一分支不能同时 checkout 在两棵树（除非 `add -f`，D1） |
| 路径「失踪但仍注册」 | 128 · `is a missing but already registered worktree; use 'add -f' …` | 先 `prune`/`remove`，或 `add -f`（D1/D3） |
| 管理条目 **locked** 且路径失踪 | 需 `add --force` **两次**（D1） | daemon 一般不该 lock |

argv 传路径时应用**无壳列表**（PowerShell/cmd 空格）；相对路径相对的是**调用时 cwd**（须在主仓或任意已链接 worktree 内跑 git）。

---

## 2. `remove` / `--force` / 手工删 + `prune`

### 文档规则（D1）

- `remove`：仅**干净** worktree（无 tracked 修改、无 untracked）可删；脏或不干净用 `--force`；**主 worktree 不可 remove**。
- **locked** worktree：`remove` 需 `--force` **两次**（`-f -f`），或先 `unlock`。
- 手工删目录后，`$GIT_DIR/worktrees/<id>` 管理文件仍在 → `git worktree prune`（或等 `gc.worktreePruneExpire`）清掉；`list` 会标 `prunable`。

### 本机行为（D3）

| 操作 | 结果 |
|------|------|
| 干净 `remove` | OK，目录消失 |
| 脏（改过/有 untracked）无 force | 128 · `contains modified or untracked files, use --force` |
| 脏 `remove --force` | OK |
| locked 一次 `--force` | 仍拒 · `use 'remove -f -f'` |
| locked `-f -f` | OK |
| **文件被进程独占打开**时 `remove --force` | exit **255** · `error: failed to delete '…': Invalid argument`；**目录仍在**，但 **admin 条目已被摘掉** → 此后对该路径 `remove` 报 `is not a working tree`；`list` 已看不到它 |
| 独占释放后 | 须**手工删残留目录**；同路径 `add` 会因「already exists」失败，直到目录清空 |
| 手工 `Remove-Item -Recurse` 目录 | `list --porcelain` 出现 `prunable gitdir file points to non-existent location`；`prune -v` 删掉 `.git/worktrees/<id>` |
| `worktree remove` **不删分支** | 分支 `agent/…` 仍留在共享 `refs/heads` |

### Windows 特有坑（D3 + D4）

1. **文件锁 / 杀毒 / 只读属性 / agent 未退**：`remove --force` 仍可能失败（`Invalid argument` / Permission denied / Device busy）。常见持锁方：agent node 进程、cwd 仍在该树内的 shell、Defender、dev server。
2. **半成功状态（硬约束）**：失败时常见「**admin 已 prune、磁盘目录未删**」→ 不能再 `worktree remove`，必须 OS 级删目录 +（若 admin 还在）`prune`。
3. **勿在待删树内 cwd 调 remove**（D4 arbor）：Windows 无法删除进程自身 cwd。
4. 社区还见「force 仍因 untracked/`node_modules` 失败」报告（D4）；本机小脏树 `--force` 成功，但 P1 清理路径应准备 **remove --force → 失败则杀进程树 → 再 remove / 再 OS 删 → prune** 的降级链。

---

## 3. 元数据位置与崩溃后枚举

### 布局（D1 + D3）

- 主仓：`<project>/.git/`（`$GIT_COMMON_DIR`）。
- 每个 linked worktree：`<common>/.git/worktrees/<name>/`，含 `gitdir`、`HEAD`、`commondir`、`index`、可选 `locked` 等。
- 链接树根：文件 `.git`（不是目录），内容 `gitdir: <absolute-or-relative-to-admin>`。
- `<name>` 通常是路径 basename；冲突时加数字后缀；空格等会规范化（见上）。

### 崩溃后残留枚举（D1 + D3）

在主仓（或任意存活 worktree）执行：

```text
git worktree list --porcelain
# 建议脚本再加 -z：路径含换行时稳（D1）
```

Porcelain 稳定字段（D1）：每条以 `worktree <path>` 起头，空行分隔；可有 `HEAD`、`branch`、`detached`、`locked`、`prunable <reason>`、`bare`。

Daemon 崩溃后典型残留：

| 状态 | `list` 表现 | 回收 |
|------|-------------|------|
| 目录与 admin 都在 | 正常条目 | `remove` / `remove --force` |
| 目录没了、admin 在 | `prunable …` | `prune`（分支仍可能在，需另删） |
| admin 没了、目录在（Windows 半成功） | **list 不可见** | 只能扫约定父目录 + OS 删除；再 `add` 前确保路径空 |
| locked | `locked` / `locked <reason>` | unlock 或 `remove -f -f` |

P1 应用层应在 `Task.workspace`（或并列字段）记下 worktree 绝对路径 + 分支名，并与 `list --porcelain` 对账；**不能只靠**磁盘存在性。

---

## 4. 脏主工作区能否 `worktree add`？与「仅并发时隔离」

**允许。** 主仓 `README.md` 有未提交修改、或有 untracked 文件时，`git worktree add -b … <path> HEAD` 仍 exit 0（D3）。新树 checkout 的是 **commit-ish 树**，不带上主工作区脏文件。

交互风险（设计层，非 git 禁令）：

- 「仅并发时」切到 worktree：并发 agent 看到的是 **HEAD（或选定 start-point）干净快照**；用户留在主仓的 WIP **不会**进 worktree——这通常是隔离想要的，但若误以为「从当前脏树分叉」会错。
- 主仓脏 **不阻止** 并发建树；也不自动 stash。
- 共享对象库/refs：各树改同一文件靠分支隔离；未提交改动只活在各自 index/工作区。
- start-point 应显式（如主分支 tip / `HEAD`），避免依赖主仓 checkout 状态。

---

## 5. 本机版本与文档摘要

- **Version**：`git version 2.54.0.windows.1`（D2/D3）。
- **子命令**：`add` / `list` / `lock` / `move` / `prune` / `remove` / `repair` / `unlock`（D2）。
- **与 P1 相关的文档要点**（D1）：脏 remove 需 `--force`；locked 需双 force；失踪注册用 `add -f` 或 prune；porcelain 给脚本；默认绝对路径链接；`remove` 不负责删分支（实验确认 D3）。
- **配置**：`worktree.useRelativePaths`、`worktree.guessRemote`、`gc.worktreePruneExpire`、`core.longpaths`（Windows 长路径仍可能撞 `$GIT_DIR too big`，D3）。

---

## 对 P1 生命周期设计的硬约束清单

1. **创建**：用绝对短路径 + 唯一 `-b agent/<project>-<task-id>`（task id 永不复用已够唯一）；start-point 显式；argv 无壳；先确保目标路径不存在或为空；捕获「branch exists / path exists / missing-but-registered」。
2. **脏主仓可 add**：并发隔离不依赖主仓干净；新树不含主仓 WIP——产品语义按此设计，勿假设会带上脏改动。
3. **清理默认**：agent/占用方退出且 cwd 不在树内之后，`git worktree remove --force <path>`；**另删分支**（`branch -D`），因 remove 不删 ref。
4. **Windows 必须有降级**：remove 失败（锁/杀毒/半成功）→ 结束持锁进程 → 再 remove；仍失败则 **OS 递归删目录 + `git worktree prune`**；注意半成功时 list 已无条目，必须靠 `Task.workspace` 路径删盘。
5. **崩溃回收**：启动或周期性 `git worktree list --porcelain`（加 `-z`）对账 `Task` 台账；`prunable` → prune；台账有路径但 list 无且目录在 → 当半成功残留清盘；勿用过长路径。
6. **不要**依赖 `worktree lock`（除非刻意防 prune）；locked 清理要 `-f -f`。
7. **调用 cwd**：在主仓（或稳定目录）执行 add/remove，避免从待删 worktree 内部调用 remove。
