"""Channel 的 Governed Bridge：唯一依赖具体实现的地方。

外部（daemon 等）经这里的工厂拿到 :class:`feishu_dispatcher.channel.Channel`，
不直接接触实现类；实现本身在 :mod:`feishu_dispatcher.channel.agent.implementation`
（候选区）。本文件的工厂签名是稳定面——改动需明确授权。

实现类以私有名声明（``_FeishuBridge`` / ``_HttpChannel``），桥内对这几处引用做
行级 pyright 豁免：于是任何**其它**模块的越界引用都会被静态检查拦下，而豁免点
集中留在本文件里、可审计。
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING

from feishu_dispatcher.channel import Channel

if TYPE_CHECKING:
    from .implementation.http import (
        ConversationRefSerializer,
        RouteHandler,
        SessionConversationHeaderProvider,
        SessionConversationOpener,
    )


def build_feishu_channel(
    *,
    app_id: str,
    app_secret: str,
    main_loop: asyncio.AbstractEventLoop,
    chat_id: str,
    sender_whitelist: Collection[str] = (),
    qps: float = 5.0,
    stream_mode: str = "card",
    throttle_window: float = 0.5,
) -> Channel:
    """构造飞书 Channel（WebSocket 收消息 + REST 发消息）。"""
    # 延迟 import：飞书实现只认 lark_oapi 的加载顺序（见实现文件头部说明）。
    from .implementation.feishu import (
        _FeishuBridge,  # pyright: ignore[reportPrivateUsage]
    )

    return _FeishuBridge(
        app_id=app_id,
        app_secret=app_secret,
        main_loop=main_loop,
        chat_whitelist=chat_id,
        sender_whitelist=sender_whitelist,
        qps=qps,
        stream_mode=stream_mode,
        throttle_window=throttle_window,
    )


def build_http_channel(
    *,
    token: str,
    main_loop: asyncio.AbstractEventLoop,
    host: str = "0.0.0.0",
    port: int = 7322,
    routes: dict[tuple[str, str], RouteHandler] | None = None,
    route_context: dict | None = None,
    session_conversation_header: SessionConversationHeaderProvider | None = None,
    open_session_conversation: SessionConversationOpener | None = None,
    conversation_ref_serializer: ConversationRefSerializer | None = None,
    throttle_window: float = 0.5,
) -> Channel:
    """构造 HTTP Channel（WebUI / 应用 API / Conversation 消息事件）。"""
    from .implementation.http import _HttpChannel  # pyright: ignore[reportPrivateUsage]

    return _HttpChannel(
        token,
        main_loop,
        host=host,
        port=port,
        routes=routes,
        route_context=route_context,
        session_conversation_header=session_conversation_header,
        open_session_conversation=open_session_conversation,
        conversation_ref_serializer=conversation_ref_serializer,
        throttle_window=throttle_window,
    )


def ensure_http_channel_token(path: Path) -> str:
    """返回 HTTP Channel 的稳定 Bearer token（不存在时生成并原子落盘）。"""
    from .implementation.http import ensure_token

    return ensure_token(path)
