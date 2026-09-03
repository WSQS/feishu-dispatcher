"""Git worktree 工具的集成测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from feishu_dispatcher.util.git import create_worktree, delete_branch, remove_worktree


@pytest.mark.asyncio
async def test_create_worktree_uses_default_branch_and_rejects_conflict(
    tmp_path: Path,
):
    project_path = tmp_path / "demo"
    subprocess.run(
        ["git", "init", "-b", "main", str(project_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (project_path / "README.md").write_text("demo", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    workspace = tmp_path / ".fdx-worktrees" / "demo-t1"
    await create_worktree(
        repository=project_path,
        workspace=workspace,
        branch="fdx/demo/t1",
    )

    branch = subprocess.run(
        ["git", "-C", str(workspace), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "fdx/demo/t1"

    subprocess.run(
        ["git", "-C", str(project_path), "branch", "fdx/demo/t2"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(RuntimeError, match="already exists|已存在"):
        await create_worktree(
            repository=project_path,
            workspace=tmp_path / ".fdx-worktrees" / "demo-t2",
            branch="fdx/demo/t2",
        )
    assert not (tmp_path / ".fdx-worktrees" / "demo-t2").exists()


@pytest.mark.asyncio
async def test_remove_worktree_keeps_branch_until_explicitly_deleted(tmp_path: Path):
    project_path = tmp_path / "demo"
    subprocess.run(
        ["git", "init", "-b", "main", str(project_path)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (project_path / "README.md").write_text("demo", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(project_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(project_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    branch = "fdx/demo/t1"
    workspace = tmp_path / ".fdx-worktrees" / "demo-t1"
    await create_worktree(
        repository=project_path,
        workspace=workspace,
        branch=branch,
    )

    await remove_worktree(repository=project_path, workspace=workspace)

    assert not workspace.exists()
    retained_branch = subprocess.run(
        [
            "git",
            "-C",
            str(project_path),
            "show-ref",
            "--verify",
            f"refs/heads/{branch}",
        ],
        check=False,
        capture_output=True,
    )
    assert retained_branch.returncode == 0

    await delete_branch(repository=project_path, branch=branch)

    deleted_branch = subprocess.run(
        [
            "git",
            "-C",
            str(project_path),
            "show-ref",
            "--verify",
            f"refs/heads/{branch}",
        ],
        check=False,
        capture_output=True,
    )
    assert deleted_branch.returncode != 0
