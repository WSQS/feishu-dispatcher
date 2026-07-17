"""lark-oapi SDK 导入兼容层（Windows + Defender 环境专用）。

背景：lark-oapi 1.7.1 的 ``lark_oapi/__init__.py`` 和
``lark_oapi/api/__init__.py`` 会 eager import **全部** API namespace
（im / aily / sheets / corehr / ... 共 57 个），每个 namespace 下又有
数百个 model 子模块。在 Windows + Defender 实时扫描下，importlib 读取
这么多小 ``.py`` 文件会触发 access violation（exit 0xC0000005），
让 ``import lark_oapi`` 直接崩进程。

本 shim 在 import lark_oapi 之前，把 ``lark_oapi`` 和 ``lark_oapi.api``
两个包对象换成「空壳」（只设 ``__path__``，不执行它们的 ``__init__``），
从而跳过那 57 个 namespace 的 eager import。我们只按需真正 import
``lark_oapi.api.im.v1``（IM 消息模型）和 ``lark_oapi.ws.pb.pbbp2_pb2``
（WebSocket frame protobuf）——这两个路径在单 namespace 加载时不会崩。

调用约定：任何要 ``import lark_oapi`` 的模块，必须在最顶部先 ``import
feishu_dispatcher._lark_compat``（顺序敏感）。重复 import 无副作用。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path


def _install() -> None:
    if "lark_oapi" in sys.modules and getattr(
        sys.modules["lark_oapi"], "__feishu_shim__", False
    ):
        return

    if "lark_oapi" in sys.modules:
        return

    base = None
    for path in sys.path:
        candidate = Path(path) / "lark_oapi" / "__init__.py"
        if candidate.exists():
            base = str((Path(path) / "lark_oapi"))
            break
    if base is None:
        return

    root = types.ModuleType("lark_oapi")
    root.__path__ = [base]
    root.__package__ = "lark_oapi"
    root.__feishu_shim__ = True  # type: ignore[attr-defined]
    sys.modules["lark_oapi"] = root

    api_pkg = types.ModuleType("lark_oapi.api")
    api_pkg.__path__ = [str(Path(base) / "api")]
    api_pkg.__package__ = "lark_oapi.api"
    api_pkg.__feishu_shim__ = True  # type: ignore[attr-defined]
    sys.modules["lark_oapi.api"] = api_pkg

    for name, sub in [
        ("lark_oapi.ws", "ws"),
        ("lark_oapi.ws.pb", str(Path("ws") / "pb")),
        ("lark_oapi.ws.pb.google", str(Path("ws") / "pb" / "google")),
        (
            "lark_oapi.ws.pb.google.protobuf",
            str(Path("ws") / "pb" / "google" / "protobuf"),
        ),
    ]:
        m = types.ModuleType(name)
        m.__path__ = [str(Path(base) / sub)]
        m.__package__ = name
        m.__feishu_shim__ = True  # type: ignore[attr-defined]
        sys.modules[name] = m


_install()