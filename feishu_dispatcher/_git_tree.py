"""当前侧 diff-tree 收集器：列出 workspace 中仍存在的 changed files 并投影直接子项。

本模块只做同步 Git / 路径处理，不含 HTTP 或 asyncio。调用方必须把它放到有界
worker 中运行，避免阻塞 daemon 主事件循环。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ._paths import PathTraversalError, validate_tree_path

_GIT_COMMAND_TIMEOUT = 10.0
_MAX_GIT_ERROR_BYTES = 4000
_DIFF_FILTER = "ACMRTUXB"  # 排除 D：本 landing 只展示当前仍存在的路径


class GitTreeError(RuntimeError):
    """Git diff-tree 收集失败。"""


class NotGitWorkspaceError(GitTreeError):
    """project path 不是带工作区的 Git repository。"""


class GitTreeCommandTimeout(GitTreeError):
    """单条 Git 命令超过进程级时限。"""


def _decode_error(data: bytes) -> str:
    return data[:_MAX_GIT_ERROR_BYTES].decode("utf-8", errors="replace").strip()


def _run_git(
    workspace: Path,
    *args: str,
    input_data: bytes | None = None,
    check: bool = True,
    timeout: float = _GIT_COMMAND_TIMEOUT,
) -> subprocess.CompletedProcess[bytes]:
    """以 argv 方式在 ``workspace`` 执行 Git，返回 bytes 结果。"""
    argv = [
        "git",
        "--no-pager",
        "--literal-pathspecs",
        "-C",
        str(workspace),
        *args,
    ]
    env = os.environ.copy()
    env.update(
        {
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "timeout": timeout,
        "env": env,
    }
    if input_data is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = input_data
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(argv, **kwargs)
    except FileNotFoundError as exc:
        raise GitTreeError("找不到 git 可执行文件") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitTreeCommandTimeout(
            f"Git 命令超时（>{timeout:g}s）: {args[0]}"
        ) from exc
    if check and proc.returncode != 0:
        detail = _decode_error(proc.stderr) or f"exit {proc.returncode}"
        raise GitTreeError(f"git {args[0]} 失败: {detail}")
    return proc


def _baseline_tree(workspace: Path) -> str:
    head = _run_git(
        workspace,
        "rev-parse",
        "--verify",
        "--quiet",
        "HEAD^{tree}",
        check=False,
    )
    if head.returncode == 0:
        return head.stdout.strip().decode("ascii")
    if head.returncode != 1:
        detail = _decode_error(head.stderr) or f"exit {head.returncode}"
        raise GitTreeError(f"无法解析 HEAD: {detail}")
    empty = _run_git(
        workspace,
        "hash-object",
        "-t",
        "tree",
        "--stdin",
        input_data=b"",
    )
    return empty.stdout.strip().decode("ascii")


def _parse_nul_paths(data: bytes) -> list[str]:
    if not data:
        return []
    if not data.endswith(b"\0"):
        raise GitTreeError("Git NUL 路径输出不完整")
    paths: list[str] = []
    for raw in data[:-1].split(b"\0"):
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitTreeError("Git 路径不是 UTF-8，viewer 无法表示") from exc
        if not path:
            raise GitTreeError("Git 返回空路径")
        try:
            validate_tree_path(path)
        except PathTraversalError as exc:
            raise GitTreeError(f"Git 返回不可表示的路径: {path!r}") from exc
        paths.append(path)
    return paths


def _currently_exists(workspace: Path, rel_path: str) -> bool:
    """用 lstat 判断当前侧路径是否仍存在；broken symlink 也算存在。"""
    target = workspace.joinpath(*rel_path.split("/"))
    try:
        target.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        # Git 已报告当前侧路径；权限等非“不存在”错误不应静默丢掉它。
        return True
    return True


def changed_worktree_paths(workspace: Path) -> list[str]:
    """返回当前 workspace 中仍存在、相对 HEAD 变化的文件路径。

    包含 tracked 最终变化与 non-ignored untracked；排除 deleted；rename 只保留新路径；
    unborn HEAD 使用空树。结果是去重、排序后的 workspace 相对 POSIX path。
    """
    workspace = workspace.resolve()
    probe = _run_git(
        workspace,
        "rev-parse",
        "--is-inside-work-tree",
        check=False,
    )
    if probe.returncode != 0 or probe.stdout.strip() != b"true":
        raise NotGitWorkspaceError("project 不是带工作区的 Git repository")

    base = _baseline_tree(workspace)
    tracked = _run_git(
        workspace,
        "diff",
        "--name-only",
        "-z",
        "--relative",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--ignore-submodules=none",
        "--find-renames=50%",
        f"--diff-filter={_DIFF_FILTER}",
        base,
        "--",
        ".",
    )
    untracked = _run_git(
        workspace,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        ".",
    )

    paths = set(_parse_nul_paths(tracked.stdout))
    paths.update(_parse_nul_paths(untracked.stdout))
    return sorted(path for path in paths if _currently_exists(workspace, path))


def _entry_sort_key(entry: dict) -> tuple:
    return (
        0 if entry["type"] == "directory" else 1,
        entry["name"].casefold(),
        entry["name"],
        entry["path"],
    )


def project_diff_tree_children(paths: list[str], rel_path: str) -> list[dict]:
    """把 changed file paths 投影成 ``rel_path`` 的直接子项。"""
    validate_tree_path(rel_path)
    prefix = "" if rel_path == "" else f"{rel_path}/"
    entries: dict[tuple[str, str], dict] = {}
    for path in paths:
        if not path.startswith(prefix):
            continue
        remainder = path[len(prefix) :]
        name, separator, _rest = remainder.partition("/")
        if separator:
            child_path = name if rel_path == "" else f"{rel_path}/{name}"
            entry = {"name": name, "path": child_path, "type": "directory"}
        else:
            entry = {"name": name, "path": path, "type": "file"}
        entries[(entry["type"], entry["path"])] = entry
    if rel_path and not entries:
        raise FileNotFoundError(rel_path)
    return sorted(entries.values(), key=_entry_sort_key)


def diff_tree_children(workspace: Path, rel_path: str) -> list[dict]:
    """收集当前侧 changed paths，并返回指定 diff-tree 目录的直接子项。"""
    return project_diff_tree_children(changed_worktree_paths(workspace), rel_path)
