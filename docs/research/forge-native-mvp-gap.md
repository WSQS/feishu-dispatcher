# Research: forge-native MVP 对照 master 的验收缺口审计

> Wayfinder ticket: [#221](https://github.com/WSQS/feishu-dispatcher/issues/221)
> Map: [#215](https://github.com/WSQS/feishu-dispatcher/issues/215)
> Parent epic: [#49](https://github.com/WSQS/feishu-dispatcher/issues/49)
> Audited ref: `origin/main` @ `684da9d`（2026-08-09）
> Scope basis: [#49 MVP 切片原文](https://github.com/WSQS/feishu-dispatcher/issues/49) + [#63 定稿范围](https://github.com/WSQS/feishu-dispatcher/issues/63)（merged via [#64](https://github.com/WSQS/feishu-dispatcher/pull/64) @ `205ee5f`）

## 结论

**Destination 已满足 / 尚缺 0 项可执行代码缺口。**

`#56`/`#57`（只读 forge）+ `#63`（`issue_url` + brief + 展示）已在 master 合入；对照 map Destination 与六个核对点，实现面闭合。`#49` 原文的 `pr_url` /「显示 PR」已被 `#63` 显式否决，属已裁定边界而非待补洞。

建议：

| 下游票 | 动作 |
|---|---|
| [fix: 按 MVP 缺口审计补齐…](https://github.com/WSQS/feishu-dispatcher/issues/226) | **无缺口则关**（无需改代码） |
| [docs: design.md 写入…](https://github.com/WSQS/feishu-dispatcher/issues/224) | 可立即开工（design 落盘；非代码缺口） |
| [task: 真机验收…](https://github.com/WSQS/feishu-dispatcher/issues/227) | HITL 验收仍建议跑（产品复盘，非实现缺口） |

---

## 核对点事实表

### 1. `Task.issue_url` 持久化、向后兼容 load；无 `pr_url`

| 项 | 事实 | 来源 |
|---|---|---|
| 字段 | `Task.issue_url: str = ""` | `feishu_dispatcher/store.py` `Task` |
| 白名单 | `_TASK_FIELDS` 含 `"issue_url"` | `store.py` |
| 向后兼容 load | `Task(**{k: d[k] for k in _TASK_FIELDS if k in d})` — 旧 JSON 缺键 → 默认 `""` | `TaskStore._load` |
| create | `create(..., issue_url="")` 写入并 flush | `TaskStore.create` |
| 无 `pr_url` | `git grep pr_url origin/main -- '*.py' '*.md'` → **空** | audit |
| 裁定 | 「不加 `pr_url`；PR 经 Closes #N 反查」 | [#63](https://github.com/WSQS/feishu-dispatcher/issues/63) |

**判定：满足。无缺口。**

### 2. `spawn_agent(..., issue=N)`：全文 brief、锚定、优雅退化

| 项 | 事实 | 来源 |
|---|---|---|
| 工具 schema | `spawn_agent` 参数含可选 `issue: integer` | `scheduler.py` `make_tools` |
| 透传 | `_spawn_agent` → `spawn_agent(project, task, agent, issue, model)` | `scheduler.py` |
| 取文 | `_compose_issue_brief` → `forge.get_item(..., body_limit=None)` | `daemon.py` |
| 锚定 | `store.create(..., issue_url=issue_url)` | `daemon._sched_spawn_agent` |
| 退化 | `resolve_forge` None / `ForgeError` → `(task, "", 提示)`，仍派活 | `_compose_issue_brief` |
| 测试 | `test_sched_spawn_with_issue_uses_body_as_brief` / `…_no_binding_degrades` / `test_spawn_agent_passes_issue` | `tests/test_daemon.py`, `tests/test_scheduler.py` |

**判定：满足。无缺口。**

### 3. 展示四处

| 处 | 符号 / 行为 | 来源 |
|---|---|---|
| 就绪消息 | spawn 根消息 `header` 在绑定时追加 `\nissue: {url}`；测试注释明确「就绪消息带 issue 链接」= `bridge.roots` | `_sched_spawn_agent`；`test_sched_spawn_with_issue_uses_body_as_brief` |
| 卡片 footer | `_issue_tag(sess.issue_url)` → `footer += f" · {issue_tag}"`（`· #N`） | `daemon` worker 建 channel 处；`_issue_tag` |
| `/task` | `if t.issue_url: lines.append(f"issue: {t.issue_url}")` | task 详情拼装 |
| `get_task` / `list_tasks` | 返回 dict 含 `"issue_url"` | `_sched_get_task` / `_sched_list_tasks`；`test_sched_get_task_reports_issue_url` |

说明：worker 文本「▶️ agent 已就绪」**不**带 issue——与模型后缀同型；`#63`/测试把「就绪消息」定义为 **spawn 话题根消息**，非该行。不算缺口。

**判定：满足。无缺口。**

（软注：无集成测试断言 footer 含 `· #N` 或 `/task` 行含 URL——覆盖缺口非 Destination 缺口，不建议为它单独开 executable。）

### 4. 只读 forge 双后端；项目无绑定不炸

| 项 | 事实 | 来源 |
|---|---|---|
| GitHub | `forge._gh_list` / `_gh_get` | `forge.py`（#56） |
| GitLab | `forge._glab_list` / `_glab_get` | `forge.py`（#57） |
| 统一入口 | `list_items` / `get_item` / `resolve_forge` | `forge.py` |
| 无绑定 | list 跳过并记 `skipped`；get 返回说明串；spawn issue 退化 | `_sched_list_forge` / `_sched_get_forge` / `_compose_issue_brief` |

**判定：满足。无缺口。**

### 5. 红线：无强制 issue-first

| 项 | 事实 | 来源 |
|---|---|---|
| `/run` | 用法无 `--issue`；源码无 `--issue` 解析 | `daemon.py` `_USAGE` / run 路径 |
| 普通 spawn | `issue` 默认 0；不传则 `brief=task`、`issue_url=""` | `_sched_spawn_agent` |
| 裁定 | 长对话默认；绑定是可选 promotion | [#49 comment](https://github.com/WSQS/feishu-dispatcher/issues/49#issuecomment-5043003469) |

**判定：满足。无缺口。**

### 6. `#49` 原文 `pr_url` /「显示 PR」 vs `#63`

| 文本 | 处理 |
|---|---|
| `#49` MVP：「Task 加可选 forge ref（`issue_url`/`pr_url`）→ … 显示对应 issue/PR」 | 方向草稿口径 |
| `#63` 定稿：单字段 `issue_url`；**不加 `pr_url`**；PR 经 forge `Closes #N` 反查；展示只绑 issue | 已合入 master；map Decisions so far 已索引 |

**判定：对齐说明，非代码缺口。勿为 `pr_url` /「显示 PR」新开 MVP 票。**  
归类：**Out of scope / 已裁定不做**（相对本 map Destination）。

---

## 缺口清单（相对 Destination）

| # | 缺口 | 应补？ |
|---|---|---|
| — | （无） | — |

### 非 Destination、勿误开票

| 项 | 归类 | 去向 |
|---|---|---|
| `docs/design.md` 未写 forge-native MVP（`git grep` 于 design 无 `issue_url`） | 文档落盘 | [#224](https://github.com/WSQS/feishu-dispatcher/issues/224) |
| 真机飞书验收 spawn+展示四处 | HITL | [#227](https://github.com/WSQS/feishu-dispatcher/issues/227) |
| `#49` 的 `pr_url` / 话题显示 PR | 已裁定不做 | map Out of scope；保持 `#63` |
| `/run --issue` | `#63` 划后续 | map Not yet specified（非闭合条件） |
| 中途换绑 / 1→N issue / 同步·入站·写 forge | epic 后续 | map Out of scope / fog |

---

## 建议毕业的 executable（本票不改代码）

无新代码票。对已有票：

1. **关闭** [#226](https://github.com/WSQS/feishu-dispatcher/issues/226) — resolution 指本文件「尚缺 0 项」。
2. **推进** [#224](https://github.com/WSQS/feishu-dispatcher/issues/224) — design 写入 MVP 决策与 `#63` 边界（含「无 pr_url」）。
3. **保留** [#227](https://github.com/WSQS/feishu-dispatcher/issues/227) — 真机验收。
