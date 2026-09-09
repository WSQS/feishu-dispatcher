"""Git 命令与通用 worktree 生命周期工具。"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from pathlib import Path

from ..acp_client import resolve_executable


async def _git_output(
    project_path: Path,
    args: Sequence[str],
    *,
    check: bool = True,
) -> str:
    """在项目目录执行 git，返回 stdout；失败时保留 stderr 诊断。"""
    executable = resolve_executable("git")
    proc = await asyncio.create_subprocess_exec(
        executable,
        "-C",
        str(project_path),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    out = stdout.decode("utf-8", "replace").strip()
    err = stderr.decode("utf-8", "replace").strip()
    if check and proc.returncode:
        detail = err or out or f"exit code {proc.returncode}"
        raise RuntimeError(f"git {' '.join(args)} 失败：{detail}")
    return out


async def create_worktree(
    *,
    repository: Path,
    workspace: Path,
    branch: str,
) -> None:
    """在仓库中创建 worktree，失败时清理已生成的目录和分支。"""
    if not repository.is_dir():
        raise RuntimeError(f"仓库路径不是目录：{repository}")
    await _git_output(repository, ["rev-parse", "--show-toplevel"])
    if workspace.exists():
        raise RuntimeError(f"worktree 路径已存在：{workspace}")

    start_point = await _git_output(
        repository,
        ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if not start_point:
        start_point = (
            await _git_output(
                repository,
                ["branch", "--show-current"],
                check=False,
            )
            or "HEAD"
        )
    branch_exists = bool(
        await _git_output(
            repository,
            ["show-ref", "--verify", f"refs/heads/{branch}"],
            check=False,
        )
    )
    if branch_exists:
        raise RuntimeError(f"worktree 分支已存在：{branch}")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    try:
        await _git_output(
            repository,
            ["worktree", "add", "-b", branch, str(workspace), start_point],
        )
    except Exception as exc:
        try:
            await remove_worktree(
                repository=repository,
                workspace=workspace,
            )
            await delete_branch(
                repository=repository,
                branch=branch,
            )
        except Exception as cleanup_exc:
            raise RuntimeError(f"{exc}；失败补偿也未完成：{cleanup_exc}") from exc
        raise


async def remove_worktree(
    *,
    repository: Path,
    workspace: Path,
) -> None:
    """删除 worktree 并修剪元数据，不处理关联分支。"""
    await _git_output(
        repository,
        ["worktree", "remove", "--force", str(workspace)],
        check=False,
    )
    if workspace.exists():
        shutil.rmtree(workspace)
    await _git_output(repository, ["worktree", "prune"])


async def delete_branch(
    *,
    repository: Path,
    branch: str,
) -> None:
    """删除存在的本地分支。"""
    if await _git_output(
        repository,
        ["show-ref", "--verify", f"refs/heads/{branch}"],
        check=False,
    ):
        await _git_output(repository, ["branch", "-D", branch])
