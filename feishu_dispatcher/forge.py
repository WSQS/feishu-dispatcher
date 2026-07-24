"""Forge（GitHub / GitLab）只读信息获取：给调度器查项目的 issue / PR（#56 / #57）。

设计边界（#49 的 1a 切片）：调度器是轻量 router——本模块只**读** issue/PR，
不写 forge、不碰代码。

per-project 绑定：项目可选 ``repo``（一个远端 URL）覆盖；不配则探测该项目 ``path``
下的 ``git remote get-url origin``。forge 类型按 URL host 推断（``github.com`` →
GitHub/gh，其余 → GitLab/glab）。GitHub 后端（gh）在 1a(#56)、GitLab 后端（glab）在
1b(#57) 实现，两者共用下面的统一入口与返回形状。

对外三个入口：

- :func:`resolve_forge` —— 项目 → :class:`ForgeRef`（或 None，表示无绑定）。
- :func:`list_items` —— 列 issue + PR（GitHub 走统一 issues 端点，PR 靠 ``pull_request``
  字段标 type），每条带 ``type``。
- :func:`get_item` —— 取单个 issue/PR 详情（body / 评论已裁剪控 token；PR 另含 CI 检查、
  评审结论、改动量）。

失败（命令缺失 / 未登录 / 无远端 / 超时）统一抛 :class:`ForgeError`（携可读消息），
由调用方（daemon 的调度器 handler）catch 后作为工具结果喂回 LLM。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

#: 详情正文 / 评论裁剪上限，控住工具循环喂回 LLM 的 token（独立于记忆层的裁剪）。
_BODY_CLIP = 2000
_COMMENT_CLIP = 400
#: 详情最多带回最近几条评论。
_MAX_COMMENTS = 3
#: 子进程默认超时（秒）——网络卡住时兜底，不阻死调度器。
_TIMEOUT = 20.0
#: 单次列表硬上限（防 gh api 一次拉太多撑爆上下文）。
_LIST_HARD_CAP = 100


class ForgeError(RuntimeError):
    """forge 命令失败 / 环境不满足，携带给用户/LLM 的可读消息。"""


@dataclass(frozen=True)
class ForgeRef:
    """一个项目绑定的远端仓库引用。"""

    kind: str  # "github"（1a）| "gitlab"（1b）
    slug: str  # "owner/repo"（GitHub）| "group/sub/proj"（GitLab）
    host: str  # "github.com" / 自建 host
    url: str  # 原始/规范化 URL（诊断用）


# --------------------------------------------------------------------------- #
# 子进程 / 可执行解析（纯壳，测试里 monkeypatch _run）
# --------------------------------------------------------------------------- #


def _resolve_exe(name: str) -> str | None:
    """在 PATH 上找可执行文件（Windows 补 .exe / .cmd 兜底）。"""
    exe = shutil.which(name)
    if exe:
        return exe
    if os.name == "nt":
        for suffix in (".exe", ".cmd"):
            exe = shutil.which(name + suffix)
            if exe:
                return exe
    return None


async def _run(
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = _TIMEOUT,
) -> tuple[int, str, str]:
    """跑一个子进程，返回 (returncode, stdout, stderr)。不阻塞事件循环、带超时。

    继承 daemon 自身环境（gh/glab 需要 PATH + 各自的凭据/配置目录）——这不是被
    沙箱约束的 agent 子进程。``env`` 是**叠加**在 os.environ 上的少量覆盖项（如 glab
    的 GITLAB_HOST 定位自建实例）。测试里整体 monkeypatch 掉，不打真命令。
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ, **env} if env else None,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise ForgeError(
            f"命令超时（>{timeout:.0f}s）: {argv[0]} {argv[1] if len(argv) > 1 else ''}"
        ) from exc
    return (
        proc.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


# --------------------------------------------------------------------------- #
# 仓库绑定解析
# --------------------------------------------------------------------------- #


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """把一个远端 URL 解析成 (host, slug)。支持 https / ssh(git@host:path) 形式。

    - ``https://github.com/owner/name(.git)`` → ("github.com", "owner/name")
    - ``git@github.com:owner/name.git``       → ("github.com", "owner/name")
    - ``https://git.corp/group/sub/proj``     → ("git.corp", "group/sub/proj")
    """
    url = (url or "").strip()
    if not url:
        return None
    # scp 式 ssh：git@host:owner/repo（无 :// 但有 @ 和 :）
    if "://" not in url and "@" in url and ":" in url:
        try:
            _, rest = url.split("@", 1)
            host, path = rest.split(":", 1)
        except ValueError:
            return None
    else:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path
    host = host.strip().lower()
    slug = path.strip().strip("/")
    if slug.endswith(".git"):
        slug = slug[:-4]
    slug = slug.strip("/")
    if not host or not slug or "/" not in slug:
        return None
    return host, slug


async def _detect_remote(path: str) -> str | None:
    """探测项目目录下 origin 远端的 URL（拿不到返回 None）。"""
    git = _resolve_exe("git")
    if not git:
        return None
    try:
        rc, out, _ = await _run([git, "-C", path, "remote", "get-url", "origin"])
    except ForgeError:
        return None
    if rc != 0:
        return None
    return out.strip() or None


async def resolve_forge(project) -> ForgeRef | None:
    """项目 → :class:`ForgeRef`。优先用配置的 ``repo`` URL，否则探测 git 远端。

    ``kind`` 按 host 推断：``github.com`` → github，其余 → gitlab（自建 GitLab）。
    解析不出 host/slug（非 git 仓、无 origin、URL 畸形）返回 None。
    """
    url = (getattr(project, "repo", "") or "").strip()
    if not url:
        url = await _detect_remote(str(project.path)) or ""
    if not url:
        return None
    parsed = _parse_remote_url(url)
    if parsed is None:
        return None
    host, slug = parsed
    kind = "github" if host == "github.com" else "gitlab"
    return ForgeRef(kind=kind, slug=slug, host=host, url=url)


# --------------------------------------------------------------------------- #
# 纯 shaper（把 forge 原始 JSON 归一到统一形状；可单测，不碰子进程）
# --------------------------------------------------------------------------- #


def _clip(s: str, limit: int | None) -> str:
    s = (s or "").strip()
    if limit is None or len(s) <= limit:  # limit=None → 不裁剪（供 brief 用全文）
        return s
    return s[:limit] + "…"


def _date(s) -> str:
    """ISO 时间串取日期部分（YYYY-MM-DD）；非串返回空。"""
    return s[:10] if isinstance(s, str) else ""


def _labels(raw) -> list[str]:
    out = []
    for lb in raw or []:
        if isinstance(lb, dict):
            name = lb.get("name")
            if name:
                out.append(name)
        elif isinstance(lb, str):
            out.append(lb)
    return out


def _shape_gh_list_item(it: dict) -> dict:
    """GitHub REST issues 端点的一条（issue 或 PR）→ 统一列表项。"""
    return {
        "number": it.get("number"),
        "type": "pr" if it.get("pull_request") else "issue",
        "title": it.get("title", ""),
        "state": it.get("state", ""),
        "labels": _labels(it.get("labels")),
        "updated": _date(it.get("updated_at")),
        "url": it.get("html_url", ""),
    }


def _summarize_checks(rollup) -> dict:
    """gh 的 statusCheckRollup（check runs + status contexts 混合）→ 计数摘要。"""
    passed = failed = pending = 0
    for c in rollup or []:
        s = str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper()
        if s in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            passed += 1
        elif s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            failed += 1
        else:  # PENDING / IN_PROGRESS / QUEUED / EXPECTED / 空
            pending += 1
    return {"passed": passed, "failed": failed, "pending": pending}


def _shape_gh_comment(c: dict) -> dict:
    return {
        "author": (c.get("author") or {}).get("login", ""),
        "at": _date(c.get("createdAt")),
        "body": _clip(c.get("body") or "", _COMMENT_CLIP),
    }


def _shape_gh_detail(
    kind: str, ref: ForgeRef, d: dict, *, body_limit: int | None = _BODY_CLIP
) -> dict:
    """gh issue/pr view --json → 统一详情形状。body_limit=None 取全文（供 brief）。"""
    out = {
        "repo": ref.slug,
        "kind": kind,
        "number": d.get("number"),
        "title": d.get("title", ""),
        "state": d.get("state", ""),
        "author": (d.get("author") or {}).get("login", ""),
        "labels": _labels(d.get("labels")),
        "url": d.get("url", ""),
        "created": _date(d.get("createdAt")),
        "updated": _date(d.get("updatedAt")),
        "body": _clip(d.get("body") or "", body_limit),
        "comments": [
            _shape_gh_comment(c) for c in (d.get("comments") or [])[-_MAX_COMMENTS:]
        ],
    }
    if kind == "pr":
        out["checks"] = _summarize_checks(d.get("statusCheckRollup"))
        out["review_decision"] = d.get("reviewDecision") or ""
        out["mergeable"] = d.get("mergeable") or ""
        out["is_draft"] = bool(d.get("isDraft"))
        out["changes"] = {
            "files": len(d.get("files") or []),
            "additions": d.get("additions", 0),
            "deletions": d.get("deletions", 0),
        }
    return out


# --------------------------------------------------------------------------- #
# GitHub 后端（gh）
# --------------------------------------------------------------------------- #


def _cli_error(name: str, err: str, rc: int) -> str:
    """从 CLI stderr 提炼一行可读错误（多在最后一行）。"""
    lines = [ln for ln in (err or "").splitlines() if ln.strip()]
    return _clip(lines[-1], 200) if lines else f"{name} 失败（rc={rc}）"


async def _gh_list(ref: ForgeRef, state: str, limit: int) -> list[dict]:
    gh = _resolve_exe("gh")
    if not gh:
        raise ForgeError("未找到 gh 命令（GitHub CLI 未安装或不在 PATH）。")
    per_page = max(1, min(limit, _LIST_HARD_CAP))
    # 用 REST issues 端点：一次返回 issue + PR（PR 带 pull_request 字段），正好贴
    # 「不分 kind 列全部」的语义；原生 gh issue list 会漏掉 PR。
    query = (
        f"repos/{ref.slug}/issues"
        f"?state={state}&per_page={per_page}&sort=updated&direction=desc"
    )
    rc, out, err = await _run([gh, "api", query])
    if rc != 0:
        raise ForgeError(_cli_error("gh", err, rc))
    try:
        raw = json.loads(out) if out.strip() else []
    except json.JSONDecodeError as exc:
        raise ForgeError("gh api 返回无法解析为 JSON") from exc
    if not isinstance(raw, list):
        raise ForgeError("gh api 返回非预期结构")
    return [_shape_gh_list_item(it) for it in raw][:limit]


async def _gh_get(
    ref: ForgeRef, kind: str, number: int, *, body_limit: int | None = _BODY_CLIP
) -> dict:
    gh = _resolve_exe("gh")
    if not gh:
        raise ForgeError("未找到 gh 命令（GitHub CLI 未安装或不在 PATH）。")
    sub = "pr" if kind == "pr" else "issue"
    fields = "number,title,state,author,body,labels,url,createdAt,updatedAt,comments"
    if sub == "pr":
        # PR 专属：CI 检查、评审结论、可合并性、改动量（issue 视图没这些字段）。
        fields += (
            ",statusCheckRollup,reviewDecision,mergeable,mergeStateStatus,"
            "additions,deletions,files,isDraft"
        )
    rc, out, err = await _run(
        [gh, sub, "view", str(number), "--repo", ref.slug, "--json", fields]
    )
    if rc != 0:
        raise ForgeError(_cli_error("gh", err, rc))
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ForgeError("gh view 返回无法解析为 JSON") from exc
    return _shape_gh_detail(kind, ref, data, body_limit=body_limit)


# --------------------------------------------------------------------------- #
# GitLab 后端（glab，#57）
# --------------------------------------------------------------------------- #
#
# 与 GitHub 的两处模型差异，本节吸收：
# 1. issue 与 MR 是**两套独立编号** → 列表要分别打 /issues 与 /merge_requests 两个
#    端点再合并；详情靠 kind 消歧走对应端点。
# 2. 自建实例 → 经 GITLAB_HOST env 定位（从 ref.host 取）。
# 走 `glab api`（而非 `glab issue list`）：直接命中文档化的 GitLab REST，响应形状可控，
# 且与 GitHub 侧 `gh api` 对称。字段名是 GitLab REST 的（iid/description/web_url/…）。
# 已对真实自建 GitLab（gitlab.ns-iot.com）实测：嵌套 slug 编码 / GITLAB_HOST 定位 /
# issue+MR 列表合并 / MR 详情（state/mergeable/draft/changes）均通过；仅 pipeline→checks
# 映射未撞到带 CI 的 MR（逻辑简单、单测覆盖）。评论未拉、MR 增删 GitLab 基础端点不含（见下）。

#: 统一 state（open/closed/all）→ GitLab REST 的 state 值；"all" 省略该参数（默认返回全部）。
_GITLAB_STATE = {"open": "opened", "closed": "closed"}


def _glab_env(ref: ForgeRef) -> dict[str, str]:
    """glab 子进程的 host 覆盖：自建实例经 GITLAB_HOST 定位（gitlab.com 也无妨带上）。"""
    return {"GITLAB_HOST": ref.host}


def _glab_changes_count(v) -> int:
    """GitLab 的 changes_count 可能是 int / "3" / "3+" / 缺失 → 取前导整数。"""
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        digits = "".join(c for c in v if c.isdigit())
        return int(digits) if digits else 0
    return 0


def _summarize_gitlab_pipeline(status) -> dict:
    """GitLab MR 的流水线状态（单个 status 串）→ 与 gh 对齐的 checks 计数。"""
    s = str(status or "").lower()
    if s == "success":
        return {"passed": 1, "failed": 0, "pending": 0}
    if s in ("failed", "canceled", "cancelled"):
        return {"passed": 0, "failed": 1, "pending": 0}
    if not s:
        return {"passed": 0, "failed": 0, "pending": 0}
    return {"passed": 0, "failed": 0, "pending": 1}  # running/pending/created/manual/…


def _shape_glab_list_item(it: dict, type_: str) -> dict:
    """GitLab issue/MR 列表项 → 统一列表项（labels 是纯字符串数组，_labels 已兼容）。"""
    return {
        "number": it.get("iid"),
        "type": type_,
        "title": it.get("title", ""),
        "state": it.get("state", ""),
        "labels": _labels(it.get("labels")),
        "updated": _date(it.get("updated_at")),
        "url": it.get("web_url", ""),
    }


def _shape_glab_detail(
    kind: str, ref: ForgeRef, d: dict, *, body_limit: int | None = _BODY_CLIP
) -> dict:
    """glab api 的 issue/MR 详情 → 统一详情形状。body_limit=None 取全文（供 brief）。

    评论走单独的 notes 端点（且混入系统事件），1b MVP 暂不拉（comments 留空）；
    MR 的 additions/deletions GitLab 基础端点不含（需 /changes 重端点），故只由
    changes_count 给出文件数、增删留 0。
    """
    out = {
        "repo": ref.slug,
        "kind": kind,
        "number": d.get("iid"),
        "title": d.get("title", ""),
        "state": d.get("state", ""),
        "author": (d.get("author") or {}).get("username", ""),
        "labels": _labels(d.get("labels")),
        "url": d.get("web_url", ""),
        "created": _date(d.get("created_at")),
        "updated": _date(d.get("updated_at")),
        "body": _clip(d.get("description") or "", body_limit),
        "comments": [],
    }
    if kind == "pr":
        pipeline = d.get("head_pipeline") or d.get("pipeline") or {}
        out["checks"] = _summarize_gitlab_pipeline(pipeline.get("status"))
        out["review_decision"] = ""  # GitLab 审批模型不同（approvals），MVP 留空
        out["mergeable"] = d.get("detailed_merge_status") or d.get("merge_status") or ""
        out["is_draft"] = bool(d.get("draft") or d.get("work_in_progress"))
        out["changes"] = {
            "files": _glab_changes_count(d.get("changes_count")),
            "additions": 0,
            "deletions": 0,
        }
    return out


async def _glab_api(ref: ForgeRef, path: str):
    """打一个 glab api 调用（自建实例经 GITLAB_HOST 定位），返回解析后的 JSON。"""
    glab = _resolve_exe("glab")
    if not glab:
        raise ForgeError("未找到 glab 命令（GitLab CLI 未安装或不在 PATH）。")
    rc, out, err = await _run([glab, "api", path], env=_glab_env(ref))
    if rc != 0:
        raise ForgeError(_cli_error("glab", err, rc))
    try:
        return json.loads(out) if out.strip() else []
    except json.JSONDecodeError as exc:
        raise ForgeError("glab api 返回无法解析为 JSON") from exc


async def _glab_list(ref: ForgeRef, state: str, limit: int) -> list[dict]:
    enc = quote(ref.slug, safe="")  # group/sub/proj → group%2Fsub%2Fproj
    per_page = max(1, min(limit, _LIST_HARD_CAP))
    base = f"per_page={per_page}&order_by=updated_at&sort=desc"
    st = _GITLAB_STATE.get(state)
    sq = f"&state={st}" if st else ""  # "all" → 不带 state（默认返回全部）
    issues = await _glab_api(ref, f"projects/{enc}/issues?{base}{sq}")
    mrs = await _glab_api(ref, f"projects/{enc}/merge_requests?{base}{sq}")
    items = [
        _shape_glab_list_item(it, "issue") for it in issues if isinstance(it, dict)
    ]
    items += [_shape_glab_list_item(it, "pr") for it in mrs if isinstance(it, dict)]
    # 两端点各自有序，合并后按 updated（日期粒度）重排再截断。
    items.sort(key=lambda x: x.get("updated", ""), reverse=True)
    return items[:limit]


async def _glab_get(
    ref: ForgeRef, kind: str, number: int, *, body_limit: int | None = _BODY_CLIP
) -> dict:
    enc = quote(ref.slug, safe="")
    endpoint = "merge_requests" if kind == "pr" else "issues"
    data = await _glab_api(ref, f"projects/{enc}/{endpoint}/{number}")
    if not isinstance(data, dict):
        raise ForgeError("glab api 返回非预期结构")
    return _shape_glab_detail(kind, ref, data, body_limit=body_limit)


# --------------------------------------------------------------------------- #
# 对外分发（按 ref.kind 选后端）
# --------------------------------------------------------------------------- #


async def list_items(ref: ForgeRef, *, state: str = "open", limit: int = 20) -> dict:
    """列某仓库的 issue + PR（混合、每条带 type）。返回统一结构或抛 ForgeError。"""
    if state not in ("open", "closed", "all"):
        state = "open"
    if ref.kind == "github":
        items = await _gh_list(ref, state, limit)
    elif ref.kind == "gitlab":
        items = await _glab_list(ref, state, limit)
    else:
        raise ForgeError(f"未知 forge 类型: {ref.kind}")
    return {
        "repo": ref.slug,
        "host": ref.host,
        "state": state,
        "count": len(items),
        "items": items,
    }


async def get_item(
    ref: ForgeRef, kind: str, number: int, *, body_limit: int | None = _BODY_CLIP
) -> dict:
    """取单个 issue/PR 详情。kind 消歧（GitLab 的 issue/MR 是两套编号）。

    ``body_limit=None`` 取完整正文（供派活当 brief）；默认裁到 _BODY_CLIP（供展示预览）。
    """
    if kind not in ("issue", "pr"):
        raise ForgeError(f"kind 必须为 issue 或 pr，当前为 {kind!r}")
    if ref.kind == "github":
        return await _gh_get(ref, kind, number, body_limit=body_limit)
    if ref.kind == "gitlab":
        return await _glab_get(ref, kind, number, body_limit=body_limit)
    raise ForgeError(f"未知 forge 类型: {ref.kind}")
