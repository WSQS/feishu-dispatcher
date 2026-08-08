"""workspace git 操作封装：供 tree/file/diff 接口调 git。

零依赖（用 subprocess 调系统 git，不用 GitPython）。三个操作：
- :func:`list_files`：git ls-files（+ 可选未跟踪），非仓降级 os.walk（决策 D10）
- :func:`diff_workdir`：git diff（工作区 vs HEAD，决策 D7）

所有函数在 workspace 根执行 git（cwd=ws）。失败抛 OSError/RuntimeError，handler 据此返错误。
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
    """ws 是否是 git 仓库（git rev-parse --is-inside-work-dir）。"""
    try:
        _run_git(ws, "rev-parse", "--is-inside-work-dir")
        return True
    except (OSError, RuntimeError):
        return False


def list_files(ws: Path, *, untracked: bool = False) -> list[dict]:
    """列 workspace 内的文件。

    git 仓：``git ls-files``（已跟踪）；untracked=True 再加 ``git status --porcelain`` 的 ``??``。
    非仓/无 git：降级 os.walk 列普通文件（决策 D10）。

    返回 ``[{path, type:"file", size}]``（type 固定 file；目录信息不返，前端按路径前缀推）。
    """
    if not is_git_repo(ws):
        return _walk_files(ws)
    tracked = _run_git(ws, "ls-files").splitlines()
    files = set(tracked)
    if untracked:
        for line in _run_git(ws, "status", "--porcelain").splitlines():
            if line.startswith("?? "):
                p = line[3:].strip().strip('"')
                # 未跟踪可能是目录（git 显示 "path/"），列其下文件
                full = ws / p
                if full.is_dir():
                    files.update(str(f.relative_to(ws)) for f in full.rglob("*") if f.is_file())
                else:
                    files.add(p)
    return [
        {"path": p, "type": "file", "size": (ws / p).stat().st_size if (ws / p).exists() else 0}
        for p in sorted(files)
        if p
    ]


def _walk_files(ws: Path) -> list[dict]:
    """非仓降级：os.walk 列普通文件，跳过常见忽略目录（.git/.venv/build/node_modules）。"""
    ignore = {".git", ".venv", "build", "node_modules", "__pycache__"}
    out = []
    for root, dirs, fnames in ws.walk():
        dirs[:] = [d for d in dirs if d not in ignore]
        for f in fnames:
            full = root / f
            out.append({"path": str(full.relative_to(ws)), "type": "file", "size": full.stat().st_size})
    out.sort(key=lambda x: x["path"])
    return out


def diff_workdir(ws: Path) -> list[dict]:
    """工作区 vs HEAD 的 diff（决策 D7，git diff 无参）。

    返回 ``[{path, status, patch}]``：status 取 M/A/D/R（从 --name-status）；patch 是该文件的
    diff 片段（从 git diff 提取，按文件分组）。非仓抛 RuntimeError（handler 返错误，决策 D10）。
    """
    if not is_git_repo(ws):
        raise RuntimeError("非 git 仓库，无法 diff（tree 仍可用 os.walk）")
    # name-status 拿每个文件的状态 + 路径
    status_lines = _run_git(ws, "diff", "--name-status", "HEAD").splitlines()
    if not status_lines or status_lines == [""]:
        return []
    statuses = []
    for line in status_lines:
        if not line.strip():
            continue
        # 形如 "M\tpath" / "R100\told\tnew" / "A\tpath"
        parts = line.split("\t")
        code = parts[0][0]  # M/A/D/R 的首字母
        path = parts[-1]  # R 重命名取新名
        statuses.append((code, path))
    # patch 按文件拿（避免整段 diff 难分组）
    result = []
    for code, path in statuses:
        patch = _run_git(ws, "diff", "HEAD", "--", path)
        result.append({"path": path, "status": code, "patch": patch})
    return result
