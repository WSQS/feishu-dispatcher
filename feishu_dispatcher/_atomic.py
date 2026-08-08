"""共享原子写工具：temp + fsync + replace + fsync_dir 的持久化原语。

抽自 ``store._atomic_write_json`` 与 ``_viewer_token._atomic_write_text`` 两份重复
实现（#113）。原语本身**不关心格式**：调用方负责序列化（JSON 调 ``json.dumps``，
纯文本直接传字符串），本模块只拿到已序列化的文本落盘。

统一前两处差异：
- **.bak 语义**：台账（store）要 .bak 回退（主文件损坏时降级），token 不要
  （丢了重新生成就行）。用 ``keep_bak`` 参数区分，而非一刀切。
- **fsync_dir 失败口径**：原 store 版记 warning，token 版静默 swallow。统一为
  **记 warning**（更可观测；token 路径丢一份也无所谓，warning 不影响其正确性）。
- **Windows 跳过 fsync_dir**：照现有实现——无法对目录取句柄 fsync，临时文件本身
  已 fsync 挡住主坑，目录项持久性退化为尽力而为。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _fsync_dir(path: Path) -> None:
    """fsync 目录，让其中的改名/创建对掉电持久。

    POSIX 才有该语义；Windows 无法对目录取句柄 fsync，跳过——临时文件本身已 fsync，
    挡住了「改名指向未落盘数据块」这个主要坑，目录项持久性退化为尽力而为。
    """
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        logger.warning("fsync 目录失败（忽略）: %s", path, exc_info=True)


def atomic_write(path: Path, text: str, *, keep_bak: bool = False) -> None:
    """原子且**持久**地把 ``text``（已序列化）写到 ``path``。

    写临时文件 → flush+fsync（数据真正落盘）→ [可选：把旧主文件改名成 ``.bak`` 作
    回退源，与新写互不覆盖] → replace 临时文件（原子改名）→ fsync 父目录（改名持久）。

    :param keep_bak: ``True`` 时先把旧主文件改名成 ``.bak``（台账要，损坏时回退上一份）；
        ``False`` 不留（token 重新生成代价低）。改名失败仅记 warning，不阻塞主写。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if keep_bak and path.exists():
        try:
            path.replace(path.with_name(path.name + ".bak"))
        except OSError:
            logger.warning("保留备份失败（忽略）: %s", path, exc_info=True)
    tmp.replace(path)
    _fsync_dir(path.parent)
