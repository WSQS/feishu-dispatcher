"""Channel 的飞书与 HTTP 实现包（候选实现）。

公共契约定义于 :mod:`feishu_dispatcher.channel`。外部不直接 import 本包，经
:mod:`feishu_dispatcher.channel.agent.bridge` 的工厂获取实现。
"""

from __future__ import annotations

# 本包内的模块在顶部就 import lark_oapi，必须先装 shim（见 _lark_compat 的调用
# 约定）。父包 __init__ 一定先于子模块执行，故在此统一安装，子模块无需各自处理。
from feishu_dispatcher.agent import _lark_compat  # noqa: F401
