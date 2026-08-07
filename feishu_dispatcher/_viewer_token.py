"""viewer 的 bearer token 生命周期工具（#110）。

独立于 config / daemon / viewer.py —— 纯函数，供 #111 接入 config 时调用：

- 用户在 ``[viewer] token`` 手填 → 用手填的，不调本模块。
- 用户未填 → daemon 启动时调 :func:`ensure_token` 生成 + 持久化，日志打印（同
  ``--discover`` 体验）；之后重启调 :func:`load_token` 读回，token 稳定不变。

token 文件是**纯文本**（就一行 token 字符串），落在 config 同目录
``~/.feishu-dispatcher/viewer.token``（决策 Q8，不回写 config.toml）。原子写：temp +
fsync + replace + fsync_dir，照 ``store._atomic_write_json`` 的持久化模式，但不留
.bak —— token 丢了重新生成就行，不需要历史回退。
"""

from __future__ import annotations

import secrets
from pathlib import Path

#: 模块公开面：只有 :func:`ensure_token` 是对外 API。``generate_token`` / ``load_token``
#: 是它的实现细节（``__all__`` 排除 = 非公开，外部勿依赖）。测试是内部消费者，可碰非公开名。
__all__ = ["ensure_token"]

#: 生成的 token 长度（字节）；URL-safe base32 hex 编码后约 32 字符，足够抗猜测。
_TOKEN_BYTES = 20


def generate_token() -> str:
    """生成一个随机 token（URL-safe，无 ``-``/``_`` 便于复制粘贴）。"""
    return secrets.token_hex(_TOKEN_BYTES)


def ensure_token(path: Path) -> str:
    """返回 ``path`` 处的有效 token：已有则读回，没有则生成 + 原子写盘。

    幂等：重复调用不覆盖已存在的 token（除非文件被外部删掉）。供 daemon 启动调用，
    确保启动结束时一定有一个持久化的 token 可用。
    """
    existing = load_token(path)
    if existing is not None:
        return existing
    token = generate_token()
    _atomic_write_text(path, token)
    return token


def load_token(path: Path) -> str | None:
    """读 ``path`` 处的 token；文件不存在或为空返回 ``None``。"""
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _atomic_write_text(path: Path, text: str) -> None:
    """原子且持久地把 ``text`` 写到 ``path``（纯文本，非 JSON）。

    模式照 ``store._atomic_write_json``：临时文件 → flush+fsync（数据落盘）→ replace
    （原子改名）→ fsync 父目录（改名持久）。**不留 .bak**：token 重新生成代价低，
    不需要历史回退。
    """
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)
    # fsync 父目录，让 replace 的改名对掉电持久。POSIX 才有目录 fsync；Windows 跳过
    # （与 store._fsync_dir 同口径）。
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
