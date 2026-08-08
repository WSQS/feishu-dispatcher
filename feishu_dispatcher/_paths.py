"""路径穿越校验工具：供 viewer 的 file/tree/diff 接口校验请求 path。

判定：把请求 path resolve 后，检查是否落在 workspace 根之内（决策 D9）。拒绝：
- 含 ``..`` 的路径
- 绝对路径（请求 path 必须相对 workspace）
- 经软链逃逸到 workspace 外的路径（resolve 展开软链）

设计：纯函数，无副作用；失败抛 :class:`PathTraversalError`（调用方据此返 400）。
"""

from __future__ import annotations

from pathlib import Path


class PathTraversalError(ValueError):
    """请求 path 逃出 workspace 根（含 ``..`` / 绝对路径 / 软链逃逸）。"""


def resolve_under_root(workspace: Path, requested: str) -> Path:
    """把 ``requested`` 解析成 workspace 内的绝对路径；逃出根抛 :class:`PathTraversalError`。

    - ``requested`` 必须是**相对**路径（绝对路径直接拒，避免调用方混淆基准）。
    - 含 ``..`` 不一定非法（如 ``a/../b`` 仍在根内），最终以 resolve 后是否在根内为准。
    - 软链：resolve 展开软链，若解析结果在 workspace 根外则拒（防软链逃逸）。

    返回 resolve 后的绝对路径（在 workspace 内，安全可读）。
    """
    if not requested:
        raise PathTraversalError("path 不能为空")
    # 绝对路径直接拒（请求语义应是「相对 workspace」）
    req_path = Path(requested)
    if req_path.is_absolute():
        raise PathTraversalError(f"拒绝绝对路径: {requested}")

    root = workspace.resolve()
    resolved = (root / req_path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise PathTraversalError(
            f"path 逃出 workspace 根: {requested} → {resolved}（根: {root}）"
        ) from e
    return resolved
