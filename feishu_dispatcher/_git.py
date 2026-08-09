"""workspace git 操作封装：供 viewer 的 diff（及后续 tree）接口调 git。

零依赖（subprocess 调系统 git）。本 landing 只暴露 :func:`diff_workdir`
（工作区 vs HEAD，决策 D7）；非仓抛 RuntimeError，由 handler 报错（决策 D10）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run_git(ws: Path, *args: str) -> str:
    """在 ws 跑 git，返回 stdout（解码 utf-8）。失败抛 RuntimeError（含 stderr）。"""
    proc = subprocess.run(
        ["git", "-C", str(ws), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} 失败（rc={proc.returncode}）: {proc.stderr.strip()}")
    return proc.stdout


def is_git_repo(ws: Path) -> bool:
    """ws 是否是 git 仓库（git rev-parse --is-inside-work-tree）。"""
    try:
        _run_git(ws, "rev-parse", "--is-inside-work-tree")
        return True
    except (OSError, RuntimeError):
        return False


def diff_workdir(ws: Path) -> list[dict]:
    """工作区 vs HEAD 的 diff（决策 D7，``git diff HEAD``）。

    返回 ``[{path, status, patch}]``：status 取 M/A/D/R（从 --name-status）；
    patch 是该文件的 diff 片段。非仓抛 RuntimeError（handler 返错误，决策 D10）。
    """
    if not is_git_repo(ws):
        raise RuntimeError("非 git 仓库，无法 diff（tree 仍可用 os.walk）")
    status_lines = _run_git(ws, "diff", "--name-status", "HEAD").splitlines()
    statuses: list[tuple[str, str]] = []
    for line in status_lines:
        if not line.strip():
            continue
        # 形如 "M\tpath" / "R100\told\tnew" / "A\tpath"
        parts = line.split("\t")
        code = parts[0][0]
        path = parts[-1]
        statuses.append((code, path))
    result = []
    for code, path in statuses:
        patch = _run_git(ws, "diff", "HEAD", "--", path)
        result.append({"path": path, "status": code, "patch": patch})
    return result
