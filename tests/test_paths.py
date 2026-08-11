"""路径穿越校验工具的单测。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from feishu_dispatcher._paths import PathTraversalError, resolve_under_root


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
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    p = resolve_under_root(tmp_path, "link.txt")
    assert p.is_relative_to(tmp_path.resolve())
