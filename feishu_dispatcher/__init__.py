"""feishu-dispatcher — 飞书驱动的个人 coding agent 调度器."""

# 必须最先 import：在 import lark_oapi 之前装好兼容 shim，
# 绕开其 eager all-namespace import（Windows + Defender 下会崩溃）。
# 详见 _lark_compat 模块文档。
from . import _lark_compat  # noqa: F401

__version__ = "0.0.1"