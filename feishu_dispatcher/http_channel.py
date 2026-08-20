"""本地 HTTP 交互 Channel：写入消息，按 Conversation 轮询输出事件。"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from . import __version__
from ._atomic import atomic_write
from .channel import ChannelMessage, MessageHandler, OutputStatus

logger = logging.getLogger(__name__)

_MAX_BODY = 1_000_000
_DEFAULT_MAX_CONVERSATIONS = 128
_DEFAULT_MAX_EVENTS = 512
_DEFAULT_MAX_TARGETS = 4096
_DEFAULT_OUTPUT_CHUNK_CHARS = 4000


class _HttpServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _HttpRequestError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, **details) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details

    def payload(self) -> dict:
        return {"error": self.code, "message": str(self), **self.details}


@dataclass
class _ConversationState:
    events: deque[dict]
    next_cursor: int = 1
    targets: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _WebAsset:
    body: bytes
    content_type: str


def _load_webui_assets() -> dict[str, _WebAsset]:
    root = files("feishu_dispatcher").joinpath("webui")
    index = _WebAsset(
        root.joinpath("index.html").read_bytes(), "text/html; charset=utf-8"
    )
    return {
        "/": index,
        "/index.html": index,
        "/webui/app.js": _WebAsset(
            root.joinpath("app.js").read_bytes(), "text/javascript; charset=utf-8"
        ),
        "/webui/style.css": _WebAsset(
            root.joinpath("style.css").read_bytes(), "text/css; charset=utf-8"
        ),
    }


def ensure_token(path: Path) -> str:
    """返回稳定 Bearer token；文件不存在或为空时生成并原子写入。"""
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    token = secrets.token_hex(20)
    atomic_write(path, token, keep_bak=False)
    return token


class HttpChannel:
    """一个 HTTP 服务实例，对应稳定 key ``http`` 的 Channel。"""

    def __init__(
        self,
        token: str,
        main_loop: asyncio.AbstractEventLoop,
        *,
        host: str = "127.0.0.1",
        port: int = 7322,
        throttle_window: float = 0.5,
        max_conversations: int = _DEFAULT_MAX_CONVERSATIONS,
        max_events: int = _DEFAULT_MAX_EVENTS,
        max_targets: int = _DEFAULT_MAX_TARGETS,
    ) -> None:
        if not token.strip():
            raise ValueError("HTTP Channel token 不能为空")
        if max_conversations <= 0:
            raise ValueError("max_conversations 必须大于 0")
        if max_events <= 0:
            raise ValueError("max_events 必须大于 0")
        if max_targets <= 0:
            raise ValueError("max_targets 必须大于 0")
        self._token = token
        self._loop = main_loop
        self._host = host
        self._port = port
        self._throttle_window = max(0.0, throttle_window)
        self._max_conversations = max_conversations
        self._max_events = max_events
        self._max_targets = max_targets
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._conversations: dict[str, _ConversationState] = {}
        self._target_conversations: dict[str, str] = {}
        self._on_message: MessageHandler | None = None
        self._webui_assets = _load_webui_assets()
        self._server: _HttpServer | None = self._build_server()
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        server = self._server
        if server is None:
            raise RuntimeError("HTTP Channel 尚未监听")
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self, on_message: MessageHandler) -> None:
        """注册入站处理器并启动 HTTP 监听线程。"""
        with self._lifecycle_lock:
            if self.is_alive():
                return
            self._on_message = on_message
            if self._server is None:
                self._server = self._build_server()
            server = self._server
            self._thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="http-channel",
                daemon=True,
            )
            try:
                self._thread.start()
            except BaseException:
                self._thread = None
                self._server = None
                self._on_message = None
                try:
                    server.server_close()
                except Exception:
                    logger.debug("HTTP Channel 启动回滚关闭异常（忽略）", exc_info=True)
                raise
        logger.info("HTTP Channel 已启动: %s", self.base_url)

    def stop(self) -> None:
        """停止监听并释放端口；可重复调用。"""
        with self._lifecycle_lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        if thread is not None and thread.is_alive():
            try:
                server.shutdown()
            except Exception:
                logger.debug("HTTP Channel shutdown 异常（忽略）", exc_info=True)
            thread.join(timeout=2.0)
        try:
            server.server_close()
        except Exception:
            logger.debug("HTTP Channel server_close 异常（忽略）", exc_info=True)

    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def restart(self) -> None:
        """监听线程死亡后重建 server；活着时不重复启动。"""
        if self.is_alive():
            return
        on_message = self._on_message
        if on_message is None:
            raise RuntimeError("HTTP Channel 尚未 start，无法 restart")
        self.stop()
        self.start(on_message)

    def create_thread(self, conversation_id: str, initial_text: str) -> str:
        conversation_id = self._clean_identity(conversation_id, "conversation_id")
        thread_id = self._new_target(conversation_id, "thread")
        self._append_event(
            conversation_id,
            "thread.created",
            thread_id=thread_id,
            text=initial_text,
        )
        return thread_id

    def send_text(self, conversation_id: str, text: str) -> str:
        conversation_id = self._clean_identity(conversation_id, "conversation_id")
        message_id = self._new_target(conversation_id, "message")
        self._append_event(
            conversation_id,
            "message.created",
            message_id=message_id,
            text=text,
            threaded=False,
        )
        return message_id

    def reply_text(
        self,
        target_id: str,
        text: str,
        *,
        threaded: bool = False,
    ) -> str:
        conversation_id = self._conversation_for_target(target_id)
        message_id = self._new_target(conversation_id, "message")
        self._append_event(
            conversation_id,
            "message.created",
            message_id=message_id,
            target_id=target_id,
            text=text,
            threaded=threaded,
        )
        return message_id

    def open_output(
        self,
        target_id: str,
        title: str,
        *,
        footer: str = "",
    ) -> _HttpStreamingOutput:
        conversation_id = self._conversation_for_target(target_id)
        return _HttpStreamingOutput(
            self,
            conversation_id,
            target_id,
            title,
            footer=footer,
            window=self._throttle_window,
        )

    def _build_server(self) -> _HttpServer:
        return _HttpServer((self._host, self._port), _make_handler(self))

    def _authorized(self, token: str) -> bool:
        return bool(token) and secrets.compare_digest(token, self._token)

    def _dispatch_health(self, token: str) -> tuple[int, dict]:
        if not self._authorized(token):
            return 401, {"error": "invalid_token"}
        return 200, {"ok": True, "channel": "http", "version": __version__}

    def _dispatch_message(self, token: str, body: object) -> tuple[int, dict]:
        if not self._authorized(token):
            return 401, {"error": "invalid_token"}
        if self._on_message is None or not self._loop.is_running():
            return 503, {"error": "channel_unavailable"}
        try:
            message = self._parse_message(body)
            if message.thread_id is not None:
                self._require_target(message.conversation_id, message.thread_id)
            self._claim_targets(message.conversation_id, [message.message_id])
            future = asyncio.run_coroutine_threadsafe(
                self._on_message(message), self._loop
            )
            future.add_done_callback(self._log_handler_result)
        except _HttpRequestError as exc:
            return exc.status, exc.payload()
        except RuntimeError as exc:
            logger.warning("HTTP Channel 消息投递失败", exc_info=True)
            return 503, {"error": "channel_unavailable", "message": str(exc)}
        return 202, {"accepted": True}

    def _dispatch_events(self, token: str, query: str) -> tuple[int, dict]:
        if not self._authorized(token):
            return 401, {"error": "invalid_token"}
        try:
            params = parse_qs(query, keep_blank_values=True)
            conversation_id = self._clean_identity(
                (params.get("conversation_id") or [""])[0], "conversation_id"
            )
            raw_after = (params.get("after") or ["0"])[0]
            try:
                after = int(raw_after)
            except (TypeError, ValueError) as exc:
                raise _HttpRequestError(
                    400, "invalid_cursor", "after 必须是非负整数"
                ) from exc
            if after < 0:
                raise _HttpRequestError(400, "invalid_cursor", "after 必须是非负整数")
            return 200, self._events_after(conversation_id, after)
        except _HttpRequestError as exc:
            return exc.status, exc.payload()

    @staticmethod
    def _parse_message(body: object) -> ChannelMessage:
        if not isinstance(body, dict):
            raise _HttpRequestError(400, "invalid_request", "请求体必须是 JSON object")
        conversation_id = HttpChannel._clean_identity(
            body.get("conversation_id"), "conversation_id"
        )
        message_id = HttpChannel._clean_identity(body.get("message_id"), "message_id")
        sender_id = HttpChannel._clean_identity(body.get("sender_id"), "sender_id")
        text = body.get("text")
        if not isinstance(text, str):
            raise _HttpRequestError(400, "invalid_request", "text 必须是字符串")
        raw_thread_id = body.get("thread_id")
        thread_id = None
        if raw_thread_id is not None:
            thread_id = HttpChannel._clean_identity(raw_thread_id, "thread_id")
        return ChannelMessage(
            conversation_id=conversation_id,
            message_id=message_id,
            thread_id=thread_id,
            text=text,
            sender_id=sender_id,
        )

    @staticmethod
    def _clean_identity(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise _HttpRequestError(
                400, "invalid_request", f"{field_name} 必须是非空字符串"
            )
        return value.strip()

    def _claim_targets(self, conversation_id: str, targets: list[str]) -> None:
        unique_targets = list(dict.fromkeys(targets))
        with self._state_lock:
            state = self._conversations.get(conversation_id)
            if state is None and len(self._conversations) >= self._max_conversations:
                raise _HttpRequestError(
                    429,
                    "conversation_capacity",
                    "HTTP Channel Conversation 数量已达上限",
                )
            for target_id in unique_targets:
                owner = self._target_conversations.get(target_id)
                if owner is not None and owner != conversation_id:
                    raise _HttpRequestError(
                        409,
                        "target_conflict",
                        "target 已属于其它 Conversation",
                        target_id=target_id,
                    )
            existing_targets = state.targets if state is not None else set()
            additions = [
                target for target in unique_targets if target not in existing_targets
            ]
            if len(existing_targets) + len(additions) > self._max_targets:
                raise _HttpRequestError(
                    429,
                    "target_capacity",
                    "该 Conversation 的 target 数量已达上限",
                )
            if state is None:
                state = _ConversationState(events=deque(maxlen=self._max_events))
                self._conversations[conversation_id] = state
            for target_id in additions:
                state.targets.add(target_id)
                self._target_conversations[target_id] = conversation_id

    def _new_target(self, conversation_id: str, kind: str) -> str:
        while True:
            target_id = f"http-{kind}-{uuid4().hex}"
            with self._state_lock:
                exists = target_id in self._target_conversations
            if not exists:
                self._claim_targets(conversation_id, [target_id])
                return target_id

    def _conversation_for_target(self, target_id: str) -> str:
        target_id = self._clean_identity(target_id, "target_id")
        with self._state_lock:
            conversation_id = self._target_conversations.get(target_id)
        if conversation_id is None:
            raise ValueError(f"未知 HTTP Channel target: {target_id}")
        return conversation_id

    def _require_target(self, conversation_id: str, target_id: str) -> None:
        with self._state_lock:
            owner = self._target_conversations.get(target_id)
        if owner is None:
            raise _HttpRequestError(
                404,
                "unknown_target",
                "thread_id 不是已知 target",
                target_id=target_id,
            )
        if owner != conversation_id:
            raise _HttpRequestError(
                409,
                "target_conflict",
                "thread_id 属于其它 Conversation",
                target_id=target_id,
            )

    def _append_event(self, conversation_id: str, event_type: str, **payload) -> int:
        with self._state_lock:
            state = self._conversations.get(conversation_id)
            if state is None:
                raise RuntimeError(f"未知 HTTP Channel Conversation: {conversation_id}")
            cursor = state.next_cursor
            state.next_cursor += 1
            state.events.append({"cursor": cursor, "type": event_type, **payload})
            return cursor

    def _events_after(self, conversation_id: str, after: int) -> dict:
        with self._state_lock:
            state = self._conversations.get(conversation_id)
            if state is None:
                raise _HttpRequestError(
                    404, "unknown_conversation", "Conversation 不存在"
                )
            latest_cursor = state.next_cursor - 1
            if after > latest_cursor:
                raise _HttpRequestError(
                    409,
                    "cursor_invalid",
                    "after 超过当前最新 cursor",
                    latest_cursor=latest_cursor,
                )
            oldest_cursor = state.events[0]["cursor"] if state.events else 0
            if state.events and after < oldest_cursor - 1:
                raise _HttpRequestError(
                    409,
                    "cursor_expired",
                    "after 对应的事件已被淘汰",
                    oldest_cursor=oldest_cursor,
                    latest_cursor=latest_cursor,
                )
            events = [dict(event) for event in state.events if event["cursor"] > after]
            return {
                "conversation_id": conversation_id,
                "events": events,
                "next_cursor": latest_cursor,
                "oldest_cursor": oldest_cursor,
            }

    @staticmethod
    def _log_handler_result(future) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("HTTP Channel 入站消息处理失败")


class _HttpStreamingOutput:
    """把流式文本节流为 Conversation-scoped 事件。"""

    def __init__(
        self,
        channel: HttpChannel,
        conversation_id: str,
        target_id: str,
        title: str,
        *,
        footer: str,
        window: float,
    ) -> None:
        self._channel = channel
        self._conversation_id = conversation_id
        self._target_id = target_id
        self._output_id = channel._new_target(conversation_id, "output")
        self._footer = footer
        self._status: OutputStatus = "running"
        self._window = window
        self._chunks: list[str] = []
        self._meta_dirty = False
        self._closed = False
        self._pending = asyncio.Event()
        self._force = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        channel._append_event(
            conversation_id,
            "output.started",
            output_id=self._output_id,
            target_id=target_id,
            title=title,
            footer=footer,
            status=self._status,
        )

    def feed(self, text: str) -> None:
        if self._closed or not text:
            return
        self._chunks.append(text)
        self._pending.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    def set_footer(self, footer: str) -> None:
        if self._closed or footer == self._footer:
            return
        self._footer = footer
        self._meta_dirty = True

    async def flush(self) -> None:
        self._force.set()
        await self._drain()

    async def set_status(self, status: OutputStatus) -> None:
        if self._closed:
            return
        self._status = status
        self._meta_dirty = True
        await self.flush()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending.set()
        self._force.set()
        if self._task is not None:
            await self._task
        await self._drain()

    async def _run(self) -> None:
        while not self._closed:
            await self._pending.wait()
            if self._closed:
                break
            try:
                await asyncio.wait_for(self._force.wait(), timeout=self._window)
            except asyncio.TimeoutError:
                pass
            self._force.clear()
            self._pending.clear()
            await self._drain()

    async def _drain(self) -> None:
        async with self._flush_lock:
            if self._chunks:
                text = "".join(self._chunks)
                self._chunks.clear()
                for start in range(0, len(text), _DEFAULT_OUTPUT_CHUNK_CHARS):
                    self._channel._append_event(
                        self._conversation_id,
                        "output.delta",
                        output_id=self._output_id,
                        text=text[start : start + _DEFAULT_OUTPUT_CHUNK_CHARS],
                    )
            if self._meta_dirty:
                self._meta_dirty = False
                self._channel._append_event(
                    self._conversation_id,
                    "output.updated",
                    output_id=self._output_id,
                    footer=self._footer,
                    status=self._status,
                )


def _make_handler(channel: HttpChannel):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # noqa: D401
            return None

        def _token(self) -> str:
            auth = self.headers.get("Authorization", "") or ""
            return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""

        def _respond(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._respond_bytes(
                status, data, content_type="application/json; charset=utf-8"
            )

        def _respond_bytes(
            self,
            status: int,
            data: bytes,
            *,
            content_type: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _respond_asset(self, asset: _WebAsset) -> None:
            self._respond_bytes(
                200,
                asset.body,
                content_type=asset.content_type,
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": (
                        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                        "form-action 'self'; frame-ancestors 'none'; object-src 'none'; "
                        "script-src 'self'; style-src 'self'"
                    ),
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        def _split(self) -> tuple[str, str]:
            path, _, query = self.path.partition("?")
            return path, query

        def _read_body(self) -> object | None:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                return None
            if length <= 0 or length > _MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return None

        def do_GET(self) -> None:  # noqa: N802
            path, query = self._split()
            asset = channel._webui_assets.get(path)
            if asset is not None:
                logger.info("http-channel GET %s → 200", path)
                self._respond_asset(asset)
                return
            if path == "/api/channel/health":
                status, payload = channel._dispatch_health(self._token())
            elif path == "/api/channel/events":
                status, payload = channel._dispatch_events(self._token(), query)
            else:
                status, payload = 404, {"error": "not_found"}
            logger.info("http-channel GET %s → %d", path, status)
            self._respond(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            path, _query = self._split()
            if path != "/api/channel/messages":
                self._respond(404, {"error": "not_found"})
                return
            body = self._read_body()
            if body is None:
                self._respond(400, {"error": "invalid_request"})
                return
            status, payload = channel._dispatch_message(self._token(), body)
            logger.info("http-channel POST %s → %d", path, status)
            self._respond(status, payload)

    return _Handler
