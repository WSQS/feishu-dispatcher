"""单实例锁：保证一个状态目录同一时刻只有一个 daemon 在跑（#81）。

基于 OS 文件锁（Windows ``msvcrt.locking`` / POSIX ``fcntl.flock``），非阻塞获取。
关键性质：锁由**持有进程**占用，进程退出（正常退出 / 崩溃 / 被 kill）时由 OS 自动释放，
不会留下需要手工清理的僵尸锁——这正是多 daemon 事故里最需要的（旧进程死了锁就没了，
新进程能起；旧进程没死锁就在，新进程明确报错而不是默默共用状态目录踩坏台账）。

lock 文件里写持锁进程 pid（纯诊断用，锁本身不依赖它），获取失败时读出来报给用户。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

#: Windows 上锁一个远离文件头的哨兵字节（可超出 EOF）。Windows 的字节区锁会让
#: 被锁区间对其它句柄不可读，故把锁放在高偏移处，让文件头的 pid 文本仍能被
#: 竞争者的另一句柄读到用于报错。POSIX 用 flock（劝告锁，不影响读），无需偏移。
_WIN_LOCK_OFFSET = 1024


def _lock(fh) -> None:
    """对已打开文件加非阻塞独占锁；已被别的进程持有则抛 OSError。"""
    if os.name == "nt":
        import msvcrt

        fh.seek(_WIN_LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(_WIN_LOCK_OFFSET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class SingleInstanceLock:
    """状态目录级单实例锁。用法：``acquire()`` → 跑 daemon → ``release()``。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._fh = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> str | None:
        """获取锁。成功返回 ``None``；已被占用返回持锁进程 pid 字符串（供报错）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # a+ 不截断已有内容，先能读到旧 pid；获取失败时用它报错。
        fh = open(self._path, "a+", encoding="utf-8")
        try:
            _lock(fh)
        except OSError:
            holder = self._read_pid(fh)
            fh.close()
            return holder or "unknown"
        # 拿到锁：写入自己的 pid（持锁进程可写自己锁定的区域）。
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
        except OSError:
            logger.debug("写入 lock 文件 pid 失败（忽略，锁已获取）", exc_info=True)
        self._fh = fh
        return None

    def release(self) -> None:
        """释放锁（幂等）。正常退出时调用；崩溃/被杀时 OS 也会自动释放。"""
        if self._fh is None:
            return
        try:
            _unlock(self._fh)
        except OSError:
            logger.debug("解锁异常（忽略）", exc_info=True)
        try:
            self._fh.close()
        except OSError:
            logger.debug("关闭 lock 文件异常（忽略）", exc_info=True)
        self._fh = None

    @staticmethod
    def _read_pid(fh) -> str:
        try:
            fh.seek(0)
            return fh.read().strip()
        except OSError:
            return ""
