"""直接子项扫描器的单测。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from feishu_dispatcher._scanner import scan_children


def test_lists_direct_children_root(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.py").write_text("y")  # 间接子项不应出现
    entries = scan_children(tmp_path, "")
    assert entries == [
        {"name": "sub", "path": "sub", "type": "directory"},
        {"name": "a.txt", "path": "a.txt", "type": "file"},
    ]


def test_nested_rel_path_prefix(tmp_path: Path):
    (tmp_path / "main.py").write_text("x")
    (tmp_path / "docs").mkdir()
    entries = scan_children(tmp_path, "src")
    assert entries == [
        {"name": "docs", "path": "src/docs", "type": "directory"},
        {"name": "main.py", "path": "src/main.py", "type": "file"},
    ]


def test_empty_dir_returns_empty(tmp_path: Path):
    assert scan_children(tmp_path, "") == []


def test_ignore_directories_but_not_files(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "build").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / ".venv").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "build.txt").write_text("x")  # 同名文件不剪
    (tmp_path / "keep").mkdir()
    entries = scan_children(tmp_path, "")
    names = [e["name"] for e in entries]
    assert names == ["keep", "build.txt"]


def test_sort_dirs_before_files_casefold(tmp_path: Path):
    (tmp_path / "z_file").write_text("x")
    (tmp_path / "A_dir").mkdir()
    (tmp_path / "b_dir").mkdir()
    entries = scan_children(tmp_path, "")
    names = [e["name"] for e in entries]
    # 目录（A_dir、b_dir，casefold 后 a<b）恒在文件（z_file）前
    assert names == ["A_dir", "b_dir", "z_file"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 大小写敏感文件名测试")
def test_sort_case_collision_by_raw_name(tmp_path: Path):
    (tmp_path / "README.md").write_text("x")
    (tmp_path / "readme.md").write_text("x")
    entries = scan_children(tmp_path, "")
    names = [e["name"] for e in entries]
    # casefold 相同 → 按原始 name 升序（大写 README 在 readme 前）
    assert names == ["README.md", "readme.md"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 软链测试")
def test_symlink_not_followed(tmp_path: Path):
    (tmp_path / "real_dir").mkdir()
    (tmp_path / "link_dir").symlink_to(tmp_path / "real_dir", target_is_directory=True)
    entries = scan_children(tmp_path, "")
    by_name = {e["name"]: e for e in entries}
    assert by_name["link_dir"]["type"] == "file"  # symlink 不跟随 → file
    assert by_name["real_dir"]["type"] == "directory"
