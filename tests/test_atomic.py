"""共享原子写工具 ``_atomic.atomic_write`` 的单元测试。

覆盖两种 ``keep_bak`` 模式的可观测行为：写后内容一致、不残留 ``.tmp``、``.bak`` 留否
符合参数、对临时文件 fsync（持久化保证）。原语不关心格式，故测试用纯文本。
"""

from __future__ import annotations

import os
from pathlib import Path

from feishu_dispatcher._atomic import atomic_write


def test_atomic_write_writes_content_and_leaves_no_tmp(tmp_path: Path):
    p = tmp_path / "f.txt"
    atomic_write(p, "hello-世界", keep_bak=False)
    assert p.read_text(encoding="utf-8") == "hello-世界"
    assert not (tmp_path / "f.txt.tmp").exists()  # 临时文件已 replace 走


def test_atomic_write_no_bak_when_disabled(tmp_path: Path):
    p = tmp_path / "f.txt"
    atomic_write(p, "v1", keep_bak=False)
    atomic_write(p, "v2", keep_bak=False)  # 二次写也不留 .bak
    assert p.read_text(encoding="utf-8") == "v2"
    assert not (tmp_path / "f.txt.bak").exists()


def test_atomic_write_keeps_bak_of_previous_version(tmp_path: Path):
    """keep_bak=True 时上一份主文件降级为 .bak（= 上一份好数据）。"""
    p = tmp_path / "f.txt"
    atomic_write(p, "v1", keep_bak=True)  # 首次：无旧主文件 → 无 .bak
    assert not (tmp_path / "f.txt.bak").exists()
    atomic_write(p, "v2", keep_bak=True)  # 二次：v1 降级为 .bak
    assert (tmp_path / "f.txt.bak").read_text(encoding="utf-8") == "v1"
    assert p.read_text(encoding="utf-8") == "v2"


def test_atomic_write_fsyncs_data(tmp_path: Path, monkeypatch):
    """落盘时对临时文件 fsync（把数据真正刷到盘，防掉电后原子改名指向未写入的块）。"""
    calls: list[int] = []
    real_fsync = os.fsync

    def spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    import feishu_dispatcher._atomic as atomic_mod

    monkeypatch.setattr(atomic_mod.os, "fsync", spy)
    atomic_write(tmp_path / "f.txt", "x", keep_bak=False)
    assert calls  # 至少 fsync 了临时文件
