"""路径穿越校验工具的单测。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from feishu_dispatcher._paths import (
    PathTraversalError,
    resolve_tree_path,
    resolve_under_root,
    validate_tree_path,
)


def test_normal_subpath_resolves_under_root(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.py").write_text("x")
    p = resolve_under_root(tmp_path, "sub/f.py")
    assert p == (tmp_path / "sub" / "f.py").resolve()
    assert p.is_relative_to(tmp_path.resolve())


def test_dotdot_staying_inside_root_allowed(tmp_path: Path):
    # a/../b 仍在根内 → 放行（.. 本身不非法，看 resolve 结果）
    (tmp_path / "b").mkdir()
    p = resolve_under_root(tmp_path, "a/../b")
    assert p == (tmp_path / "b").resolve()


def test_dotdot_escaping_root_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="逃出"):
        resolve_under_root(tmp_path, "../../etc/passwd")


def test_absolute_path_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="绝对路径"):
        resolve_under_root(tmp_path, str(tmp_path / "file.txt"))


def test_empty_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="不能为空"):
        resolve_under_root(tmp_path, "")


@pytest.mark.skipif(os.name == "nt", reason="POSIX 软链测试")
def test_symlink_escape_rejected(tmp_path: Path):
    # 在 workspace 内建软链指向外部，resolve 应展开并判定逃出
    outside = tmp_path.parent / "outside_secret"
    outside.write_text("secret")
    try:
        (tmp_path / "evil").symlink_to(outside)
        with pytest.raises(PathTraversalError, match="逃出"):
            resolve_under_root(tmp_path, "evil")
    finally:
        outside.unlink(missing_ok=True)


def test_symlink_into_root_allowed(tmp_path: Path):
    # 软链指向 workspace 内部 → resolve 仍在根内 → 放行
    (tmp_path / "real.txt").write_text("x")
    try:
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    except OSError as exc:
        if os.name == "nt" and exc.winerror == 1314:
            pytest.skip("Windows 当前用户无创建符号链接权限")
        raise
    p = resolve_under_root(tmp_path, "link.txt")
    assert p.is_relative_to(tmp_path.resolve())


# ---- /tree/children 的路径语法 ---- #


def test_tree_path_lexical_validator_accepts_virtual_path():
    # 不访问文件系统：diff-tree 可校验尚未 materialize 的目录字符串。
    validate_tree_path("deleted/nested")
    validate_tree_path("")


def test_tree_path_lexical_validator_rejects_dotdot():
    with pytest.raises(PathTraversalError, match="语法"):
        validate_tree_path("a/../b")


def test_tree_root_resolves_to_workspace(tmp_path: Path):
    assert resolve_tree_path(tmp_path, "") == tmp_path.resolve()


def test_tree_normal_subdir_resolves(tmp_path: Path):
    (tmp_path / "src" / "main").mkdir(parents=True)
    p = resolve_tree_path(tmp_path, "src/main")
    assert p == (tmp_path / "src" / "main").resolve()
    assert p.is_relative_to(tmp_path.resolve())


def test_tree_backslash_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="反斜杠"):
        resolve_tree_path(tmp_path, "src\\main")


def test_tree_dot_segment_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="语法"):
        resolve_tree_path(tmp_path, "./src")


def test_tree_dotdot_segment_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="语法"):
        resolve_tree_path(tmp_path, "a/../b")


def test_tree_trailing_slash_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="语法"):
        resolve_tree_path(tmp_path, "src/")


def test_tree_leading_slash_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="语法"):
        resolve_tree_path(tmp_path, "/src")


def test_tree_double_slash_rejected(tmp_path: Path):
    with pytest.raises(PathTraversalError, match="语法"):
        resolve_tree_path(tmp_path, "src//a")


@pytest.mark.skipif(os.name == "nt", reason="POSIX 软链测试")
def test_tree_symlink_escape_rejected(tmp_path: Path):
    outside = tmp_path.parent / "outside_secret"
    outside.write_text("secret")
    try:
        (tmp_path / "evil").symlink_to(outside)
        with pytest.raises(PathTraversalError, match="逃出"):
            resolve_tree_path(tmp_path, "evil")
    finally:
        outside.unlink(missing_ok=True)
