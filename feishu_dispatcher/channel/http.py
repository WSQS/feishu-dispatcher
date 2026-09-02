"""HTTP 交互 Channel：承载 WebUI、应用 API 与 Conversation 消息事件。"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs
from uuid import uuid4

from .. import __version__
from .._atomic import atomic_write
from ..conversation import ConversationRef
from ..session_event import (
    AgentOutputDelta,
    AgentOutputFinished,
    AgentOutputStarted,
    AgentPlanUpdated,
    ConversationRefSerializer,
    SessionEvent,
    SessionInputAccepted,
    ToolCallObserved,
    session_event_to_dict,
)
from . import ChannelMessage, MessageHandler, OutputStatus


@dataclass(frozen=True)
class HttpConversationRef:
    """HTTP Channel 持有的具体会话引用。"""

    conversation_id: str

    def channel_key(self) -> str:
        return "http"

    def to_log_string(self) -> str:
        return f"http:{self.conversation_id}"


logger = logging.getLogger("feishu_dispatcher.http_channel")

_MAX_BODY = 1_000_000
_DEFAULT_MAX_CONVERSATIONS = 128
_DEFAULT_MAX_EVENTS = 512
_DEFAULT_MAX_TARGETS = 4096
_DISPATCH_TIMEOUT = 30.0

RouteHandler = Callable[[dict, dict], Coroutine[Any, Any, tuple[int, dict]]]
SessionConversationHeaderProvider = Callable[[str], str]
SessionConversationOpener = Callable[[str, ConversationRef], str | None]
HttpBodyReader = Callable[[], object | None]

_CREATE_TASK_CONVERSATION_ROUTE = (
    "POST",
    "/api/tasks/{task_id}/conversations",
)


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


@dataclass(frozen=True)
class HttpResponse:
    """HTTP Channel 对请求适配层返回的完整响应。"""

    status: int
    body: bytes
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HttpRequest:
    """请求适配层传给 HTTP Channel 的规范化请求。"""

    method: str
    path: str
    query: str
    token: str
    read_body: HttpBodyReader | None = None


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
        "/webui/api.js": _WebAsset(
            root.joinpath("api.js").read_bytes(), "text/javascript; charset=utf-8"
        ),
        "/webui/storage.js": _WebAsset(
            root.joinpath("storage.js").read_bytes(), "text/javascript; charset=utf-8"
        ),
        "/webui/tasks.js": _WebAsset(
            root.joinpath("tasks.js").read_bytes(), "text/javascript; charset=utf-8"
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
        host: str = "0.0.0.0",
        port: int = 7322,
        routes: dict[tuple[str, str], RouteHandler] | None = None,
        route_context: dict | None = None,
        session_conversation_header: SessionConversationHeaderProvider | None = None,
        open_session_conversation: SessionConversationOpener | None = None,
        conversation_ref_serializer: ConversationRefSerializer | None = None,
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
        if (session_conversation_header is None) != (open_session_conversation is None):
            raise ValueError("Session Conversation 标题与打开回调必须同时提供")
        self._token = token
        self._loop = main_loop
        self._host = host
        self._port = port
        self._routes = dict(routes or {})
        if session_conversation_header is not None:
            if _CREATE_TASK_CONVERSATION_ROUTE in self._routes:
                raise ValueError("Task Conversation 路由不能重复注册")
            self._routes[_CREATE_TASK_CONVERSATION_ROUTE] = (
                self._create_task_conversation
            )
        self._route_context = dict(route_context or {})
        self._session_conversation_header = session_conversation_header
        self._open_session_conversation = open_session_conversation
        self._conversation_ref_serializer = conversation_ref_serializer
        self._max_conversations = max_conversations
        self._max_events = max_events
        self._max_targets = max_targets
        self._instance_id = uuid4().hex
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._conversations: dict[str, _ConversationState] = {}
        self._target_conversations: dict[str, str] = {}
        self._pending_outputs: dict[str, deque[_HttpStreamingOutput]] = {}
        self._active_outputs: dict[tuple[str, str, str], _HttpStreamingOutput] = {}
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

    def serialize_conversation_ref(
        self,
        conversation: ConversationRef,
    ) -> dict[str, object]:
        conversation = self._require_http_conversation(conversation)
        conversation_id = conversation.conversation_id.strip()
        if not conversation_id:
            raise ValueError("HTTP ConversationRef 不能为空")
        return {"conversation_id": conversation_id}

    @staticmethod
    def _require_http_conversation(
        conversation: ConversationRef,
    ) -> HttpConversationRef:
        if not isinstance(conversation, HttpConversationRef):
            raise ValueError("ConversationRef 不属于 HTTP Channel")
        return conversation

    def deserialize_conversation_ref(
        self,
        payload: dict[str, object],
    ) -> HttpConversationRef:
        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("HTTP ConversationRef payload 无效")
        return HttpConversationRef(conversation_id.strip())

    def create_thread(self, initial_text: str) -> HttpConversationRef:
        conversation_id = f"http-conversation-{uuid4().hex}"
        conversation = HttpConversationRef(conversation_id)
        self._claim_targets(conversation_id, [])
        message_id = self._new_target(conversation_id, "message")
        self._append_event(
            conversation_id,
            "conversation.created",
            message_id=message_id,
            text=initial_text,
        )
        return conversation

    async def _create_task_conversation(
        self, _context: dict, request: dict
    ) -> tuple[int, dict]:
        session_id = request["segments"]["task_id"]
        header_provider = self._session_conversation_header
        opener = self._open_session_conversation
        if header_provider is None or opener is None:
            return 503, {"error": "channel_unavailable"}
        try:
            header = header_provider(session_id)
        except ValueError:
            return 404, {"error": "task_not_found", "task_id": session_id}

        conversation = self.create_thread(header)
        try:
            terminal_status = opener(session_id, conversation)
        except ValueError:
            self._rollback_conversation(conversation)
            return 404, {"error": "task_not_found", "task_id": session_id}
        except Exception:
            self._rollback_conversation(conversation)
            logger.exception("HTTP Channel 绑定 Session Conversation 失败")
            return 503, {"error": "channel_unavailable"}
        if terminal_status is not None:
            self._rollback_conversation(conversation)
            return 409, {
                "error": "task_terminal",
                "task_id": session_id,
                "status": terminal_status,
            }
        return 201, {
            "task_id": session_id,
            "conversation_id": conversation.conversation_id,
        }

    def send_text(self, conversation: ConversationRef, text: str) -> str:
        conversation_id = self._clean_identity(
            self._require_http_conversation(conversation).conversation_id,
            "conversation_id",
        )
        self._claim_targets(conversation_id, [])
        message_id = self._new_target(conversation_id, "message")
        self._append_event(
            conversation_id,
            "message.created",
            message_id=message_id,
            text=text,
        )
        return message_id

    def handle_session_event(
        self,
        conversation: ConversationRef,
        event: SessionEvent,
        *,
        trace_sequence: int | None = None,
    ) -> None:
        """把 Session 领域事件投影为 HTTP Conversation 事件。"""
        body = event.body
        if not isinstance(
            body,
            (
                SessionInputAccepted,
                AgentOutputStarted,
                AgentOutputDelta,
                AgentPlanUpdated,
                AgentOutputFinished,
                ToolCallObserved,
            ),
        ):
            raise ValueError(f"暂不支持的 SessionEvent body: {type(body).__name__}")
        conversation_id = self._clean_identity(
            self._require_http_conversation(conversation).conversation_id,
            "conversation_id",
        )
        self._claim_targets(conversation_id, [])
        owner = conversation_id
        presentation: dict[str, object] | None = None
        if isinstance(body, AgentOutputStarted):
            output = self._start_session_output(owner, event)
            if output is not None:
                presentation = output.started_presentation()
        elif isinstance(body, AgentOutputDelta):
            output = self._active_output(owner, event)
            if output is not None:
                presentation = output.delta_presentation(body)
        elif isinstance(body, AgentPlanUpdated):
            output = self._active_output(owner, event)
            if output is not None:
                presentation = output.plan_presentation(body)
        elif isinstance(body, AgentOutputFinished):
            output = self._active_output(owner, event)
            if output is not None:
                presentation = output.finished_presentation(body)
        elif isinstance(body, ToolCallObserved):
            output = self._active_output(owner, event)
            if output is not None:
                presentation = output.tool_call_presentation(body)
        payload: dict[str, object] = {
            "event": session_event_to_dict(
                event,
                conversation_ref_serializer=self._conversation_ref_serializer,
            )
        }
        if trace_sequence is not None:
            payload["trace_sequence"] = trace_sequence
        if presentation is not None:
            payload["presentation"] = presentation
        self._append_event(
            owner,
            "session.event",
            **payload,
        )
        if not isinstance(body, SessionInputAccepted):
            return
        if not body.text:
            return
        source = body.source.channel_key() if body.source is not None else "unknown"
        self.send_text(
            HttpConversationRef(conversation_id),
            f"↪️ 同步自 {source}：{body.text}",
        )

    def open_output(
        self,
        conversation: ConversationRef,
        title: str,
        *,
        footer: str = "",
    ) -> _HttpStreamingOutput:
        conversation_id = self._clean_identity(
            self._require_http_conversation(conversation).conversation_id,
            "conversation_id",
        )
        self._claim_targets(conversation_id, [])
        output = _HttpStreamingOutput(
            self._new_target(conversation_id, "output"),
            conversation_id,
            title,
            footer=footer,
            on_close=self._unregister_output,
        )
        self._register_output(output)
        return output

    def _register_output(self, output: _HttpStreamingOutput) -> None:
        with self._state_lock:
            self._pending_outputs.setdefault(output.conversation_id, deque()).append(
                output
            )

    def _unregister_output(self, output: _HttpStreamingOutput) -> None:
        with self._state_lock:
            pending = self._pending_outputs.get(output.conversation_id)
            if pending is not None:
                self._pending_outputs[output.conversation_id] = deque(
                    item for item in pending if item is not output
                )
                if not self._pending_outputs[output.conversation_id]:
                    del self._pending_outputs[output.conversation_id]
            for key, active in list(self._active_outputs.items()):
                if active is output:
                    del self._active_outputs[key]

    def _start_session_output(
        self,
        conversation_id: str,
        event: SessionEvent,
    ) -> _HttpStreamingOutput | None:
        if event.turn_id is None:
            return None
        with self._state_lock:
            pending = self._pending_outputs.get(conversation_id)
            if not pending:
                return None
            output = pending.popleft()
            if not pending:
                del self._pending_outputs[conversation_id]
            self._active_outputs[(conversation_id, event.session_id, event.turn_id)] = (
                output
            )
            return output

    def _active_output(
        self,
        conversation_id: str,
        event: SessionEvent,
    ) -> _HttpStreamingOutput | None:
        if event.turn_id is None:
            return None
        with self._state_lock:
            return self._active_outputs.get(
                (conversation_id, event.session_id, event.turn_id)
            )

    def _build_server(self) -> _HttpServer:
        return _HttpServer((self._host, self._port), _make_handler(self))

    def dispatch_http_request(self, request: HttpRequest) -> HttpResponse:
        """处理一个 HTTP 请求，供每请求 Handler 使用。"""
        if request.method == "GET":
            asset = self._webui_assets.get(request.path)
            if asset is not None:
                logger.info("http-channel GET %s → 200", request.path)
                return HttpResponse(
                    200,
                    asset.body,
                    asset.content_type,
                    {
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
            if request.path == "/api/channel/health":
                status, payload = self._dispatch_health(request.token)
            elif request.path == "/api/channel/events":
                status, payload = self._dispatch_events(request.token, request.query)
            elif request.path.startswith("/api/"):
                status, payload = self._dispatch_route(
                    request.token, "GET", request.path, request.query
                )
            else:
                status, payload = 404, {"error": "not_found"}
            logger.info("http-channel GET %s → %d", request.path, status)
            return self._json_response(status, payload)

        if request.method == "POST":
            if request.path != "/api/channel/messages":
                if not request.path.startswith("/api/"):
                    return self._json_response(404, {"error": "not_found"})
                if not self._authorized(request.token):
                    return self._json_response(401, {"error": "invalid_token"})
                body = request.read_body() if request.read_body is not None else None
                status, payload = self._dispatch_route(
                    request.token, "POST", request.path, request.query, body
                )
                logger.info("http-channel POST %s → %d", request.path, status)
                return self._json_response(status, payload)
            if not self._authorized(request.token):
                return self._json_response(401, {"error": "invalid_token"})
            body = request.read_body() if request.read_body is not None else None
            if body is None:
                return self._json_response(400, {"error": "invalid_request"})
            status, payload = self._dispatch_message(request.token, body)
            logger.info("http-channel POST %s → %d", request.path, status)
            return self._json_response(status, payload)

        return self._json_response(404, {"error": "not_found"})

    @staticmethod
    def _json_response(status: int, payload: dict) -> HttpResponse:
        return HttpResponse(
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _authorized(self, token: str) -> bool:
        return bool(token) and secrets.compare_digest(token, self._token)

    def _dispatch_health(self, token: str) -> tuple[int, dict]:
        if not self._authorized(token):
            return 401, {"error": "invalid_token"}
        return 200, {
            "ok": True,
            "channel": "http",
            "version": __version__,
            "instance_id": self._instance_id,
        }

    def _dispatch_message(self, token: str, body: object) -> tuple[int, dict]:
        if not self._authorized(token):
            return 401, {"error": "invalid_token"}
        handler = self._on_message
        if handler is None or not self._loop.is_running():
            return 503, {"error": "channel_unavailable"}
        try:
            message = self._parse_message(body)
            self._claim_targets(
                self._require_http_conversation(message.conversation).conversation_id,
                [message.message_id],
            )

            async def dispatch() -> None:
                await handler(message)

            future = asyncio.run_coroutine_threadsafe(dispatch(), self._loop)
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
            return 200, {
                "instance_id": self._instance_id,
                **self._events_after(conversation_id, after),
            }
        except _HttpRequestError as exc:
            return exc.status, {
                "instance_id": self._instance_id,
                **exc.payload(),
            }

    def _dispatch_route(
        self,
        token: str,
        method: str,
        path: str,
        query: str,
        body: object | None = None,
    ) -> tuple[int, dict]:
        if not self._authorized(token):
            return 401, {"error": "invalid_token"}
        match = _match_route(self._routes, method, path)
        if match is None:
            return 404, {"error": "not_found"}
        if not self._loop.is_running():
            return 503, {"error": "channel_unavailable"}
        handler, segments = match
        request = {
            "path": path,
            "query": _parse_query(query),
            "segments": segments,
        }
        if method == "POST":
            request["body"] = body
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(
                handler(self._route_context, request), self._loop
            )
            return future.result(timeout=_DISPATCH_TIMEOUT)
        except _HttpRequestError as exc:
            return exc.status, exc.payload()
        except Exception as exc:
            if future is not None and not future.done():
                future.cancel()
            logger.exception("HTTP Channel 路由处理失败 %s %s", method, path)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}

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
        conversation = HttpConversationRef(conversation_id)
        return ChannelMessage(
            conversation=conversation,
            message_id=message_id,
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

    def _rollback_conversation(self, conversation: ConversationRef) -> None:
        conversation_id = self._require_http_conversation(conversation).conversation_id
        with self._state_lock:
            state = self._conversations.pop(conversation_id, None)
            if state is not None:
                for target_id in state.targets:
                    if self._target_conversations.get(target_id) == conversation_id:
                        del self._target_conversations[target_id]
            self._pending_outputs.pop(conversation_id, None)
            for key in [
                key for key in self._active_outputs if key[0] == conversation_id
            ]:
                del self._active_outputs[key]

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
    """登记一个回合的 HTTP 展示元数据，由 SessionEvent 驱动实际事件。"""

    def __init__(
        self,
        output_id: str,
        conversation_id: str,
        title: str,
        *,
        footer: str,
        on_close: Callable[["_HttpStreamingOutput"], None],
    ) -> None:
        self._output_id = output_id
        self.conversation_id = conversation_id
        self._footer = footer
        self._on_close = on_close
        self._closed = False
        self._title = title
        self._message_text = ""
        self._last_stream: str | None = None

    def feed(self, text: str) -> None:
        return None

    def set_footer(self, footer: str) -> None:
        if self._closed:
            return
        self._footer = footer

    async def flush(self) -> None:
        return None

    async def set_status(self, status: OutputStatus) -> None:
        return None

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._on_close(self)

    def started_presentation(self) -> dict[str, object]:
        return {
            "output_id": self._output_id,
            "title": self._title,
            "footer": self._footer,
            "status": "running",
        }

    def delta_presentation(self, event: AgentOutputDelta) -> dict[str, object]:
        display_text = event.text
        if event.stream == "thought" and self._last_stream != "thought":
            display_text = f"💭 {event.text}"
        elif event.stream == "message" and self._last_stream == "thought":
            display_text = f"\n{event.text}"
        self._last_stream = event.stream
        if event.stream == "message":
            self._message_text += event.text
        return {
            "output_id": self._output_id,
            "text": display_text,
        }

    def plan_presentation(self, event: AgentPlanUpdated) -> dict[str, object]:
        marks = {"pending": "⬜", "in_progress": "🔄", "completed": "☑️"}
        self._last_stream = "activity"
        return {
            "output_id": self._output_id,
            "text": "\n📋 计划:\n"
            + "\n".join(
                f"{marks[entry.status]} {entry.content}" for entry in event.entries
            )
            + "\n",
        }

    def finished_presentation(self, event: AgentOutputFinished) -> dict[str, object]:
        if event.message != self._message_text:
            suffix = (
                event.message[len(self._message_text) :]
                if event.message.startswith(self._message_text)
                else event.message
            )
            self._message_text = event.message
        else:
            suffix = ""
        status = cast(
            OutputStatus,
            {
                "completed": "done",
                "cancelled": "stopped",
                "failed": "error",
            }[event.outcome],
        )
        return {
            "output_id": self._output_id,
            "text": suffix,
            "footer": self._footer,
            "status": status,
        }

    def tool_call_presentation(self, event: ToolCallObserved) -> dict[str, object]:
        if (
            event.status == "started"
            and event.kind in {"execute", "other"}
            and not event.detail
        ):
            text = ""
        else:
            icon = {"started": "🔧", "completed": "✅", "failed": "❌"}[event.status]
            text = f"{icon} {event.title}"
            if event.detail and event.detail != event.title:
                text += f": {event.detail}"
            text = f"\n{text}\n"
        self._last_stream = "activity"
        return {
            "output_id": self._output_id,
            "text": text,
        }


def _match_route(
    routes: dict[tuple[str, str], RouteHandler], method: str, path: str
) -> tuple[RouteHandler, dict[str, str]] | None:
    exact = routes.get((method, path))
    if exact is not None:
        return exact, {}
    path_parts = path.strip("/").split("/")
    for (route_method, template), handler in routes.items():
        if route_method != method or "{" not in template:
            continue
        template_parts = template.strip("/").split("/")
        if len(template_parts) != len(path_parts):
            continue
        segments: dict[str, str] = {}
        for template_part, path_part in zip(template_parts, path_parts, strict=True):
            if template_part.startswith("{") and template_part.endswith("}"):
                segments[template_part[1:-1]] = path_part
            elif template_part != path_part:
                break
        else:
            return handler, segments
    return None


def _parse_query(query: str) -> dict[str, str]:
    if not query:
        return {}
    pairs = parse_qs(query, keep_blank_values=True)
    return {key: values[0] for key, values in pairs.items()}


def _make_handler(channel: HttpChannel):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002, D401
            return None

        def _token(self) -> str:
            auth = self.headers.get("Authorization", "") or ""
            return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""

        def _respond(self, response: HttpResponse) -> None:
            self._respond_bytes(
                response.status,
                response.body,
                content_type=response.content_type,
                headers=response.headers,
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
            response = channel.dispatch_http_request(
                HttpRequest("GET", path, query, self._token())
            )
            self._respond(response)

        def do_POST(self) -> None:  # noqa: N802
            path, query = self._split()
            response = channel.dispatch_http_request(
                HttpRequest(
                    "POST",
                    path,
                    query,
                    self._token(),
                    read_body=self._read_body,
                )
            )
            self._respond(response)

    return _Handler
