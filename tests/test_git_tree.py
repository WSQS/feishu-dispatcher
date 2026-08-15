"""当前侧 diff-tree Git 收集与虚拟直接子项投影测试。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from feishu_dispatcher._git_tree import (
    NotGitWorkspaceError,
    changed_worktree_paths,
    project_diff_tree_children,
)


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit_all(repo: Path, message: str = "base") -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)


def test_project_direct_children_and_sorting():
    paths = [
        "src/model.py",
        "README.md",
        "docs/guide.md",
        "src/api/main.py",
    ]
    assert project_diff_tree_children(paths, "") == [
        {"name": "docs", "path": "docs", "type": "directory"},
        {"name": "src", "path": "src", "type": "directory"},
        {"name": "README.md", "path": "README.md", "type": "file"},
    ]
    assert project_diff_tree_children(paths, "src") == [
        {"name": "api", "path": "src/api", "type": "directory"},
        {"name": "model.py", "path": "src/model.py", "type": "file"},
    ]
    with pytest.raises(FileNotFoundError):
        project_diff_tree_children(paths, "missing")


def test_collects_current_side_changes_and_excludes_deleted(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("v1\n", encoding="utf-8")
    (repo / "deleted.txt").write_text("gone\n", encoding="utf-8")
    (repo / "old.txt").write_text("same\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "src" / "app.py").write_text("v2\n", encoding="utf-8")
    (repo / "deleted.txt").unlink()
    (repo / "old.txt").rename(repo / "new.txt")  # 未 staged rename：仍只保留当前新路径
    (repo / "untracked.py").write_text("new\n", encoding="utf-8")
    (repo / "ignored.log").write_text("ignored\n", encoding="utf-8")

    assert changed_worktree_paths(repo) == [
        "new.txt",
        "src/app.py",
        "untracked.py",
    ]


def test_uses_final_worktree_not_intermediate_index_state(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "file.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    assert changed_worktree_paths(repo) == ["file.txt"]  # staged-only 仍是当前侧变化

    _git(repo, "restore", "--worktree", "--source=HEAD", "file.txt")
    assert changed_worktree_paths(repo) == []  # index 中间态不覆盖最终 worktree


def test_unborn_head_uses_empty_tree_and_git_ignore(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("no\n", encoding="utf-8")

    assert changed_worktree_paths(repo) == [".gitignore", "a.txt"]


def test_nested_workspace_is_scoped_and_paths_are_relative(tmp_path: Path):
    repo = tmp_path / "repo"
    app = repo / "app"
    sibling = repo / "sibling"
    _init_repo(repo)
    app.mkdir()
    sibling.mkdir()
    (app / "inside.txt").write_text("v1\n", encoding="utf-8")
    (sibling / "outside.txt").write_text("v1\n", encoding="utf-8")
    _commit_all(repo)

    (app / "inside.txt").write_text("v2\n", encoding="utf-8")
    (app / "new.txt").write_text("new\n", encoding="utf-8")
    (sibling / "outside.txt").write_text("v2\n", encoding="utf-8")
    (sibling / "new.txt").write_text("new\n", encoding="utf-8")

    assert changed_worktree_paths(app) == ["inside.txt", "new.txt"]


def test_file_replaced_by_directory_only_shows_current_files(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "docs").write_text("old file\n", encoding="utf-8")
    _commit_all(repo)

    (repo / "docs").unlink()
    (repo / "docs").mkdir()
    (repo / "docs" / "readme.md").write_text("new file\n", encoding="utf-8")

    paths = changed_worktree_paths(repo)
    assert paths == ["docs/readme.md"]
    assert project_diff_tree_children(paths, "") == [
        {"name": "docs", "path": "docs", "type": "directory"}
    ]


def test_conflicted_file_appears_at_current_path(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "conflict.txt").write_text("base\n", encoding="utf-8")
    _commit_all(repo)
    base_branch = _git(repo, "branch", "--show-current").stdout.strip().decode()

    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "conflict.txt").write_text("side\n", encoding="utf-8")
    _commit_all(repo, "side")
    _git(repo, "checkout", "-q", base_branch)
    (repo / "conflict.txt").write_text("main\n", encoding="utf-8")
    _commit_all(repo, "main")
    merge = _git(repo, "merge", "side", check=False)
    assert merge.returncode != 0

    assert changed_worktree_paths(repo) == ["conflict.txt"]


def test_non_git_workspace_rejected(tmp_path: Path):
    with pytest.raises(NotGitWorkspaceError, match="不是带工作区"):
        changed_worktree_paths(tmp_path)
