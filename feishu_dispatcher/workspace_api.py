"""Workspace 文件树与文件读取 API handler。"""

from __future__ import annotations

from pathlib import Path

from feishu_dispatcher import __version__
from feishu_dispatcher._paths import (
    PathTraversalError,
    resolve_tree_path,
    resolve_under_root,
)

#: 单个文件预览的内容上限，避免 daemon 与移动端为大文件分配过多内存
_MAX_FILE_BYTES = 1_000_000


async def health(_ctx: dict, _request: dict) -> tuple[int, dict]:
    """``GET /api/health``：存活探针 + 版本，供安卓端确认连得上、对得上版本。

    不读 store（ctx 不用），但统一 async 签名（dispatch 全走 marshal）。
    """
    return 200, {"ok": True, "version": __version__}


async def list_projects(ctx: dict, _request: dict) -> tuple[int, dict]:
    """``GET /api/projects``：列出所有 project（合并 config 种子 + 运行时注册，决策 Q5）。

    ctx 须含 ``all_projects``：一个返回 ``dict[str, Project]`` 的可调用（daemon 的
    ``_all_projects``）。在主 loop 上执行（经 dispatch marshal），store 访问安全。
    """
    all_projects = ctx.get("all_projects")
    if all_projects is None:
        return 500, {"error": "all_projects 未注入 ctx"}
    items = [
        {"name": p.name, "path": str(p.path), "default_agent": p.default_agent}
        for p in all_projects().values()
    ]
    return 200, {"items": items}


async def tree_children(ctx: dict, request: dict) -> tuple[int, dict]:
    """``GET /api/projects/{name}/tree/children?path=``：列指定目录的直接子项（按目录加载）。

    经 ``ctx["scan_executor"]`` 在 worker 线程跑，不占主 loop。``path`` 是必填 query 键，
    空串 = workspace 根。错误：语法/逃逸 → 400，不存在 → 404，是文件 → 400，无权限 → 403。
    """
    name = request["segments"]["name"]
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):
        return ws
    query = request["query"]
    if "path" not in query:
        return 400, {"error": "missing path parameter"}
    path = query["path"]
    try:
        dir_path = resolve_tree_path(ws, path)
    except PathTraversalError as exc:
        return 400, {"error": str(exc)}
    executor = ctx.get("scan_executor")
    if executor is None:
        return 500, {"error": "scan_executor not in ctx"}
    try:
        entries = await executor.run(dir_path, path)
    except FileNotFoundError:
        return 404, {"error": f"not found: {path}"}
    except NotADirectoryError:
        return 400, {"error": f"not a directory: {path}"}
    except PermissionError:
        return 403, {"error": f"permission denied: {path}"}
    return 200, {"path": path, "entries": entries}


async def file(ctx: dict, request: dict) -> tuple[int, dict]:
    """``GET /api/projects/{name}/file?path=&rev=work``：读工作区文件内容。

    返回 ``{path, rev, binary, content}``。``binary=true`` 时 ``content`` 为空串（客户端
    提示不可预览）；文本按 UTF-8 解码，失败亦当 binary。路径过 ``resolve_under_root``。
    """
    name = request["segments"]["name"]
    rel = request["query"].get("path", "")
    rev = request["query"].get("rev", "work")
    if rev != "work":
        return 400, {"error": f"unsupported rev: {rev} (v1 仅 work)"}
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):
        return ws
    try:
        resolved = resolve_under_root(ws, rel)
    except PathTraversalError as exc:
        return 400, {"error": str(exc)}
    if not resolved.is_file():
        return 404, {"error": f"not a file: {rel}"}
    with resolved.open("rb") as stream:
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        return 413, {"error": f"file too large (max {_MAX_FILE_BYTES} bytes)"}
    # 空文件当文本；含 NUL 或 UTF-8 解不开 → binary
    if data and (b"\x00" in data[:8192]):
        return 200, {"path": rel, "rev": rev, "binary": True, "content": ""}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return 200, {"path": rel, "rev": rev, "binary": True, "content": ""}
    return 200, {"path": rel, "rev": rev, "binary": False, "content": text}


def _resolve_workspace(ctx: dict, name: str) -> "Path | tuple[int, dict]":
    """按 project name 查 workspace 路径；project 不存在返回错误响应 tuple。"""
    all_projects = ctx.get("all_projects")
    if all_projects is None:
        return 500, {"error": "all_projects not in ctx"}
    p = all_projects().get(name)
    if p is None:
        return 404, {"error": f"unknown project: {name}"}
    return p.path
