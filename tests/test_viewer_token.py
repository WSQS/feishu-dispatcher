"""viewer token 生命周期工具的单元测试（#110）。

覆盖：生成（随机性/长度）、原子写纯文本、读回一致、ensure 幂等不覆盖、load 不存在返回 None。
"""

from __future__ import annotations

from pathlib import Path

from feishu_dispatcher._viewer_token import (
    ensure_token,
    generate_token,
    load_token,
    _TOKEN_BYTES,
)


def test_generate_token_random_and_length():
    a = generate_token()
    b = generate_token()
    assert a != b  # 随机性
    # token_hex 每字节 2 字符
    assert len(a) == _TOKEN_BYTES * 2
    assert all(c in "0123456789abcdef" for c in a)  # URL-safe hex，无符号


def test_ensure_token_creates_and_persists(tmp_path: Path):
    path = tmp_path / "viewer.token"
    token = ensure_token(path)
    assert isinstance(token, str) and token
    # 文件是纯文本，内容就是 token（无 JSON 包裹、无多余空白）
    assert path.read_text(encoding="utf-8").strip() == token


def test_ensure_token_idempotent_does_not_overwrite(tmp_path: Path):
    path = tmp_path / "viewer.token"
    first = ensure_token(path)
    second = ensure_token(path)
    assert first == second  # 已存在则读回，不重新生成
    assert path.read_text(encoding="utf-8").strip() == first


def test_load_token_returns_none_when_missing(tmp_path: Path):
    assert load_token(tmp_path / "nope.token") is None


def test_load_token_returns_none_when_empty(tmp_path: Path):
    path = tmp_path / "viewer.token"
    path.write_text("   \n", encoding="utf-8")
    assert load_token(path) is None


def test_load_token_reads_back(tmp_path: Path):
    path = tmp_path / "viewer.token"
    token = ensure_token(path)
    assert load_token(path) == token


def test_atomic_write_no_tmp_left(tmp_path: Path):
    """原子写后不应残留 .tmp 文件。"""
    path = tmp_path / "viewer.token"
    ensure_token(path)
    assert not (tmp_path / "viewer.token.tmp").exists()
