import ast
import inspect
from importlib.util import find_spec
from pathlib import Path
from typing import get_type_hints

import pytest

import feishu_dispatcher.channel as channel_module
import feishu_dispatcher.channel.agent.implementation as implementations
import feishu_dispatcher.channel.agent.implementation.feishu as feishu_module
import feishu_dispatcher.channel.agent.implementation.http as http_module
from feishu_dispatcher.agent.session_event import SessionEvent
from feishu_dispatcher.channel import Channel, ChannelMessage
from feishu_dispatcher.channel.agent import bridge
from feishu_dispatcher.channel.agent.implementation.feishu import FeishuConversationRef
from feishu_dispatcher.channel.agent.implementation.http import HttpConversationRef
from feishu_dispatcher.conversation import ConversationRef

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "feishu_dispatcher"
_BRIDGE_SUBTREE = _PACKAGE_ROOT / "channel" / "agent"
_IMPLEMENTATION_PACKAGE = "feishu_dispatcher.channel.agent.implementation"


def test_promoted_contracts_have_one_authoritative_definition() -> None:
    assert ConversationRef.__module__ == "feishu_dispatcher.conversation"
    assert Channel.__module__ == "feishu_dispatcher.channel"
    assert ChannelMessage.__module__ == "feishu_dispatcher.channel"
    assert get_type_hints(ChannelMessage)["conversation"] is ConversationRef
    assert find_spec("feishu_dispatcher.agent.conversation") is None


def test_channel_implementations_use_promoted_message_type() -> None:
    assert feishu_module.ChannelMessage is ChannelMessage
    assert http_module.ChannelMessage is ChannelMessage
    assert feishu_module.ConversationRef is ConversationRef
    assert http_module.ConversationRef is ConversationRef


def test_channel_implementation_package_does_not_reexport_contracts() -> None:
    for name in ("Channel", "ChannelMessage", "MessageHandler", "OutputStatus"):
        assert not hasattr(implementations, name)


def test_channel_keeps_session_event_in_agent_zone() -> None:
    assert SessionEvent.__module__ == "feishu_dispatcher.agent.session_event"
    assert get_type_hints(Channel.handle_session_event)["event"] is SessionEvent
    assert find_spec("feishu_dispatcher.session_event") is None


def _imported_modules(path: Path) -> set[str]:
    """返回文件 import 的模块全名；相对 import 按 PEP 328 解析为绝对名。"""
    parts = list(path.relative_to(_PACKAGE_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    package = parts if path.name == "__init__.py" else parts[:-1]
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = (node.module or "").split(".") if node.module else []
            if node.level:
                keep = max(0, len(package) - (node.level - 1))
                base = [*package[:keep], *base]
            if base:
                modules.add(".".join(base))
    return modules


def test_only_the_bridge_imports_channel_implementations() -> None:
    """域外（`feishu_dispatcher/` 内、`channel/agent/` 之外）不得 import 实现包。

    目录级边界检查，与 pyright 的符号级私有检查、契约公共面互补。`tests/`、
    `scripts/` 是白盒，按 pyproject 里 pyright `exclude` 的口径豁免。
    """
    offenders = [
        f"{path.relative_to(_PACKAGE_ROOT)} → {name}"
        for path in sorted(_PACKAGE_ROOT.rglob("*.py"))
        if _BRIDGE_SUBTREE not in path.parents
        for name in sorted(_imported_modules(path))
        if name.startswith(_IMPLEMENTATION_PACKAGE)
    ]

    assert offenders == []


def test_bridge_exposes_factories_but_no_concrete_types() -> None:
    """桥只暴露工厂；具体实现类不是桥或契约的公共名。"""
    for name in (
        "build_feishu_channel",
        "build_http_channel",
        "ensure_http_channel_token",
    ):
        assert callable(getattr(bridge, name))
    for name in (
        "_FeishuBridge",
        "_HttpChannel",
        "FeishuConversationRef",
        "HttpConversationRef",
    ):
        assert not hasattr(bridge, name)
        assert not hasattr(channel_module, name)


def test_bridge_factory_signatures_are_frozen() -> None:
    """桥签名是稳定面：改动即失败，提醒先走授权（见 AGENTS.md）。"""
    feishu = inspect.signature(bridge.build_feishu_channel).parameters
    assert list(feishu) == [
        "app_id",
        "app_secret",
        "main_loop",
        "chat_id",
        "sender_whitelist",
        "qps",
        "stream_mode",
        "throttle_window",
    ]
    http = inspect.signature(bridge.build_http_channel).parameters
    assert list(http) == [
        "token",
        "main_loop",
        "host",
        "port",
        "routes",
        "route_context",
        "session_conversation_header",
        "open_session_conversation",
        "conversation_ref_serializer",
        "throttle_window",
    ]
    assert list(inspect.signature(bridge.ensure_http_channel_token).parameters) == [
        "path"
    ]
    assert {param.kind for param in (*feishu.values(), *http.values())} == {
        inspect.Parameter.KEYWORD_ONLY
    }


def test_conversation_ref_scopes_conversation_id_by_channel() -> None:
    feishu = FeishuConversationRef("main")
    web = HttpConversationRef("main")

    assert feishu.channel_key() == "feishu"
    assert web.channel_key() == "http"
    assert feishu.to_log_string() == "feishu:main"
    assert web.to_log_string() == "http:main"
    assert feishu.conversation_id == "main"
    assert web.conversation_id == "main"
    assert feishu != web
    assert len({feishu, web}) == 2


def test_channels_have_distinct_conversation_ref_types() -> None:
    assert FeishuConversationRef is not HttpConversationRef
    assert isinstance(FeishuConversationRef("main"), ConversationRef)
    assert isinstance(HttpConversationRef("main"), ConversationRef)


def test_conversation_ref_is_an_interface() -> None:
    with pytest.raises(TypeError):
        ConversationRef()
