"""生成 / 清理大树数据集，供 tree 接口基线测量。

用法：
  uv run python scripts/gen_tree_dataset.py [--root DIR] [--files N]
      [--depth D] [--breadth B] [--file-size BYTES] [--clean]

默认写到临时目录下的专用名目录，不污染仓库。生成是确定性的（同参数同结构），
保证新旧接口跑在同一份数据上可比。``--clean`` 只删除数据集目录、不生成。

目录结构：breadth 叉、depth 层（共 1 + breadth + ... + breadth^depth 个目录），
N 个文件按轮转均匀铺到所有目录下（每文件 file-size 字节）。
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

_DEFAULT_DIRNAME = "feishu-dispatcher-tree-dataset"
#: 目录数上限（breadth^depth 会爆炸，兜底防手滑）
_MAX_DIRS = 200_000


def _predicted_dirs(depth: int, breadth: int) -> int:
    return sum(breadth**i for i in range(depth + 1))


def build_tree(
    root: Path, *, files: int, depth: int, breadth: int, file_size: int
) -> tuple[int, int]:
    """在 ``root`` 下构造目录树并铺满 ``files`` 个文件，返回 (目录数, 文件数)。"""
    root.mkdir(parents=True, exist_ok=True)
    dirs: list[Path] = [root]
    frontier: list[Path] = [root]
    for level in range(depth):
        nxt: list[Path] = []
        for d in frontier:
            for i in range(breadth):
                child = d / f"d{level}_{i}"
                child.mkdir(parents=True, exist_ok=True)
                dirs.append(child)
                nxt.append(child)
        frontier = nxt
    content = b"x" * file_size
    for idx in range(files):
        parent = dirs[idx % len(dirs)]
        (parent / f"f{idx}.txt").write_bytes(content)
    return len(dirs), files


def main() -> int:
    p = argparse.ArgumentParser(description="生成 / 清理大树数据集")
    p.add_argument(
        "--root",
        default=None,
        help=f"数据集根目录（默认临时目录下的 {_DEFAULT_DIRNAME}）",
    )
    p.add_argument("--files", type=int, default=10_000, help="文件总数（默认 10000）")
    p.add_argument("--depth", type=int, default=3, help="额外目录层数（默认 3）")
    p.add_argument(
        "--breadth", type=int, default=10, help="每层每目录的子目录数（默认 10）"
    )
    p.add_argument(
        "--file-size", type=int, default=1, help="每个文件的字节数（默认 1）"
    )
    p.add_argument("--clean", action="store_true", help="只删除数据集目录，不生成")
    args = p.parse_args()

    root = (
        Path(args.root) if args.root else Path(tempfile.gettempdir()) / _DEFAULT_DIRNAME
    )

    if args.clean:
        if root.exists():
            shutil.rmtree(root)
            print(f"已清理数据集目录: {root}")
        else:
            print(f"数据集目录不存在: {root}")
        return 0

    if args.files < 0 or args.depth < 0 or args.breadth < 1 or args.file_size < 0:
        print(
            "参数不合法：files>=0, depth>=0, breadth>=1, file-size>=0", file=sys.stderr
        )
        return 2
    if _predicted_dirs(args.depth, args.breadth) > _MAX_DIRS:
        print(
            f"目录数超上限（depth={args.depth}, breadth={args.breadth} 会生成 "
            f"{_predicted_dirs(args.depth, args.breadth)} 个目录 > {_MAX_DIRS}）",
            file=sys.stderr,
        )
        return 2

    if root.exists():
        shutil.rmtree(root)  # 重新生成前清掉旧数据，保证干净且确定
    n_dirs, n_files = build_tree(
        root,
        files=args.files,
        depth=args.depth,
        breadth=args.breadth,
        file_size=args.file_size,
    )
    print(f"已生成数据集: {root}")
    print(f"  目录数 = {n_dirs}")
    print(f"  文件数 = {n_files}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
