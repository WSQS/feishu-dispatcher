"""目录直接子项扫描器：枚举一个目录的一层直接子项（不递归）。

供 /tree/children 使用（配合 ``_paths.resolve_tree_path``）。纯同步函数——由执行服务
放到 worker 线程跑，不占 daemon 主 loop。

语义：
- 只枚举一层，不递归、不逐文件 stat；
- 忽略目录名精确匹配 {.git, .venv, build, node_modules, __pycache__} 的目录
  （只剪目录、不剪同名文件）；
- symlink 不跟随：``is_dir(follow_symlinks=False)``，symlink 一律当 file；
- 单个子项 OSError 跳过、目标目录本身 OSError 上抛；
- 稳定排序：目录在文件前，再按 ``casefold(name)``、``name``、``path`` 升序。
"""

from __future__ import annotations

import os
from pathlib import Path

#: 默认忽略的目录名（精确匹配、只剪目录）
_IGNORE_DIRS = frozenset({".git", ".venv", "build", "node_modules", "__pycache__"})

_DIR_TYPE = "directory"
_FILE_TYPE = "file"


def _sort_key(entry: dict) -> tuple:
    return (
        0 if entry["type"] == _DIR_TYPE else 1,
        entry["name"].casefold(),
        entry["name"],
        entry["path"],
    )


def scan_children(dir_path: Path, rel_path: str) -> list[dict]:
    """枚举 ``dir_path`` 的直接子项，返回按稳定排序的条目列表。

    ``rel_path`` 是 workspace 相对路径（根为 ``""``），用于拼子项的 ``path``。
    每个条目形如 ``{"name": 子项名, "path": 相对路径, "type": "directory"|"file"}``。
    目标目录不可读 / 不存在时 OSError 上抛，由调用方映射错误码。
    """
    entries: list[dict] = []
    with os.scandir(dir_path) as it:
        for entry in it:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue  # 单条目判定失败（消失 / 无权限）→ 跳过、继续列其余
            if is_dir and entry.name in _IGNORE_DIRS:
                continue
            child_rel = entry.name if rel_path == "" else f"{rel_path}/{entry.name}"
            entries.append(
                {
                    "name": entry.name,
                    "path": child_rel,
                    "type": _DIR_TYPE if is_dir else _FILE_TYPE,
                }
            )
    entries.sort(key=_sort_key)
    return entries
