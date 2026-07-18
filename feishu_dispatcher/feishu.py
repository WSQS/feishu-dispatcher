"""飞书通信桥：WebSocket 收消息 + HTTP 发消息。

设计决策 #7：飞书开放平台 WebSocket 长连接，纯出站，无需公网暴露。
话题形式群（``group_message_type: "thread"``）里根消息 = 任务派发，
``reply_in_thread: true`` 创建话题 = agent 子 session，用根 message_id 路由。

实现说明：
- HTTP 发消息直接走飞书开放平台 REST API（``requests``），自取 tenant_access_token。
- WebSocket 收消息用 ``websockets`` 库 + 官方 protobuf Frame（``pbbp2``）。
  绕开 ``lark.ws.Client``：它依赖 ``EventDispatcherHandler``，后者 eager import
  全部 57 个 API namespace，在 Windows + Defender 下会 access violation 崩溃
  （详见 :mod:`feishu_dispatcher._lark_compat`）。事件 JSON 全部手写 dict 解析，
  不依赖任何 lark API model；只 import ``ws.pb``（protobuf Frame）与 ``ws.const``。
  frame/ACK/ping 语义对照官方参考实现 ``lark_oapi/ws/client.py``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests
import websockets
from lark_oapi.ws.pb import pbbp2_pb2
from lark_oapi.ws.const import (
    HEADER_BIZ_RT,
    HEADER_MESSAGE_ID,
    HEADER_SEQ,
    HEADER_SUM,
    HEADER_TYPE,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 延迟 import：event 模型属于 im.v1 namespace（单 ns，安全），但顶部 import
# 会触发 lark_oapi/__init__；shim 必须先装好。调用方 import 顺序：
#   import feishu_dispatcher._lark_compat  # noqa: F401
#   from feishu_dispatcher.feishu import ...

logger = logging.getLogger(__name__)

_FEISHU_DOMAIN = "https://open.feishu.cn"
_ENDPOINT_URI = "/callback/ws/endpoint"
_HEADER_HANDSHAKE_STATUS = "handshake-status"
_HEADER_HANDSHAKE_MSG = "handshake-msg"

# FrameType / MessageType 常量（避免 import enum 模块触发额外加载）
_FRAME_CONTROL = 0
_FRAME_DATA = 1
_MSG_EVENT = "event"
_MSG_PING = "ping"
_MSG_PONG = "pong"

# IM 消息类型白名单（P0 只处理文本）
_TEXT_MSG_TYPE = "text"


@dataclass(frozen=True)
class IncomingMessage:
    """从飞书收到的、已规整的消息。"""

    chat_id: str
    message_id: str
    #: 话题根 message_id；根消息本身时为 None。路由话题回复用它。
    thread_root_id: str | None
    text: str
    chat_type: str
    sender_id: str


class FeishuBridge:
    """飞书双向通信封装。

    - :meth:`start_background` 在后台线程启动 WebSocket 长连接
    - :meth:`send_root_message` / :meth:`reply_in_thread` 同步发送（HTTP）
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        main_loop: asyncio.AbstractEventLoop,
        on_event: Callable[[IncomingMessage], Awaitable[None]],
        *,
        chat_whitelist: str = "",
        domain: str = _FEISHU_DOMAIN,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._main_loop = main_loop
        self._on_event = on_event
        self._chat_whitelist = chat_whitelist
        self._domain = domain.rstrip("/")
        self._tenant_token: str = ""
        self._tenant_token_expires: float = 0.0
        # R14：共享 Session + 自动重试退避，防止飞书限流时丢消息
        self._session = self._build_session()
        self._ws_thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._stopping = threading.Event()
        #: ping 间隔（秒），服务端可通过 endpoint 发现响应 / pong payload 下发
        self._ping_interval: float = 120.0
        #: 分片合包缓存：message_id -> (首片到达时刻 monotonic, 分片列表)。
        #: 只在 WS 线程内读写，无需跨线程同步。
        self._frag_cache: dict[str, tuple[float, list[bytes | None]]] = {}

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_session() -> requests.Session:
        """R14：构造带重试策略的 Session。

        - total=3，backoff_factor=0.5（0.5/1/2s 退避）
        - 对 429/500/502/503 自动重试（429 默认尊重 Retry-After header）
        - 仅对 POST 重试（幂等性上飞书 IM 消息按 message 维度可接受偶发重复，
          但限流时重试比直接丢弃好——配合 daemon 层 message_id 去重兜底）
        """
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503],
            allowed_methods=["POST", "PATCH"],
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        return s

    def start_background(self) -> None:
        """在守护线程里启动 WebSocket 长连接。"""
        self._ws_thread = threading.Thread(
            target=self._ws_main, name="feishu-ws", daemon=True
        )
        self._ws_thread.start()
        logger.info("飞书 WebSocket 长连接已在后台线程启动")

    def is_alive(self) -> bool:
        """R13：WS 线程是否存活（供 daemon 看门狗周期检查）。"""
        return self._ws_thread is not None and self._ws_thread.is_alive()

    def restart(self) -> None:
        """R13：重启 WS 线程（看门狗检测到线程死亡后调用）。

        前提：调用方已确认 daemon 未在退出（_stopping 未置位）。
        清理上一次的 loop/task/thread 引用后重新 start_background。
        若旧线程还活着（异常调用场景），不做任何事。
        """
        if self._stopping.is_set():
            logger.debug("restart 被跳过：daemon 正在退出")
            return
        if self.is_alive():
            logger.debug("restart 被跳过：WS 线程仍存活")
            return
        self._ws_loop = None
        self._ws_task = None
        self._ws_thread = None
        logger.warning("飞书 WS 线程已死亡，正在重启…")
        self.start_background()

    def stop(self) -> None:
        """请求停止 WS 线程。跨线程调用安全（cancel 经 call_soon_threadsafe）。"""
        self._stopping.set()
        loop, task = self._ws_loop, self._ws_task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def _ws_main(self) -> None:
        """WS 线程入口：自带 event loop，跑 WS 连接 + 自动重连。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop
        self._ws_task = loop.create_task(self._ws_run_forever())
        try:
            loop.run_until_complete(self._ws_task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    async def _ws_run_forever(self) -> None:
        """连飞书 WS endpoint，断线自动重连。"""
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                await self._ws_connect_once()
                backoff = 1.0  # 成功连过则重置退避
            except Exception:
                logger.exception("飞书 WS 连接异常，%.1fs 后重试", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _ws_connect_once(self) -> None:
        conn_url = self._discover_ws_url()
        logger.info("飞书 WS 连接中: %s", _redact_url(conn_url))
        async with websockets.connect(conn_url, proxy=None) as ws:
            logger.info("飞书 WS 已连接")
            ping_task = asyncio.create_task(self._ping_loop(ws, conn_url))
            try:
                async for raw in ws:
                    await self._handle_frame(ws, raw)
            finally:
                ping_task.cancel()

    # ------------------------------------------------------------------ #
    # endpoint 发现 / ping
    # ------------------------------------------------------------------ #

    def _discover_ws_url(self) -> str:
        """POST /callback/ws/endpoint 拿长连接地址（与官方 SDK 一致）。"""
        resp = self._session.post(
            self._domain + _ENDPOINT_URI,
            json={"AppID": self._app_id, "AppSecret": self._app_secret},
            headers={"locale": "zh"},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"飞书 endpoint 发现失败: {body}")
        data = body["data"]
        self._apply_client_config(data.get("ClientConfig"))
        return data["URL"]

    def _apply_client_config(self, conf: dict | None) -> None:
        """应用服务端下发的连接配置（与官方 SDK 的 _configure 对应）。"""
        if not conf:
            return
        interval = conf.get("PingInterval")
        if interval:
            self._ping_interval = float(interval)
            logger.debug("飞书 WS ping 间隔更新为 %ss", interval)

    async def _ping_loop(self, ws, conn_url: str) -> None:
        """周期性发 ping 保活（service id 从 conn_url query 解析）。"""
        service_id = _service_id_from_url(conn_url) or "0"
        while True:
            try:
                frame = _new_ping_frame(int(service_id))
                await ws.send(frame.SerializeToString())
                logger.debug("飞书 WS ping 已发送")
            except Exception:
                logger.warning("飞书 WS ping 失败", exc_info=True)
            await asyncio.sleep(self._ping_interval)

    # ------------------------------------------------------------------ #
    # frame 处理
    # ------------------------------------------------------------------ #

    async def _handle_frame(self, ws, raw: bytes) -> None:
        frame = pbbp2_pb2.Frame()
        frame.ParseFromString(raw)
        headers = {h.key: h.value for h in frame.headers}
        msg_type = headers.get(HEADER_TYPE, "")

        if frame.method == _FRAME_CONTROL:
            # pong 的 payload 可能携带 ClientConfig（官方 _handle_control_frame 语义）
            if msg_type == _MSG_PONG and frame.payload:
                try:
                    self._apply_client_config(json.loads(frame.payload.decode("utf-8")))
                except Exception:
                    logger.debug("解析 pong ClientConfig 失败", exc_info=True)
            return
        if frame.method != _FRAME_DATA:
            return
        if msg_type != _MSG_EVENT:
            return

        payload = frame.payload
        # 合包（sum>1）处理：按 message_id 缓存分片
        sum_total = int(headers.get(HEADER_SUM, "1"))
        seq = int(headers.get(HEADER_SEQ, "0"))
        if sum_total > 1:
            payload = self._combine(
                headers.get(HEADER_MESSAGE_ID, ""), sum_total, seq, payload
            )
            if payload is None:
                return

        # ACK 语义对照官方实现：成功 code=200（HTTPStatus.OK），失败 500，
        # 并附 biz_rt（处理耗时 ms）header。code 不对服务端会重推事件。
        start = time.monotonic()
        resp_obj = {"code": 200}
        try:
            self._dispatch_event(payload)
        except Exception:
            logger.exception(
                "处理飞书事件失败 message_id=%s", headers.get(HEADER_MESSAGE_ID, "")
            )
            resp_obj = {"code": 500}
        ack = pbbp2_pb2.Frame()
        ack.CopyFrom(frame)
        biz_rt = ack.headers.add()
        biz_rt.key = HEADER_BIZ_RT
        biz_rt.value = str(int((time.monotonic() - start) * 1000))
        ack.payload = json.dumps(resp_obj).encode("utf-8")
        await ws.send(ack.SerializeToString())

    #: 分片缓存过期时间（秒）：超时未凑齐的分片直接丢弃
    _FRAG_TTL = 5.0

    def _combine(self, msg_id: str, total: int, seq: int, bs: bytes) -> bytes | None:
        """合包：凑齐 total 个分片返回拼接结果，否则返回 None。

        只在 WS 线程内调用。用 ``None`` 占位判断分片是否到齐（空 bytes
        也是合法分片内容），过期条目按 TTL 惰性清理。
        """
        now = time.monotonic()
        expired = [
            k for k, (ts, _) in self._frag_cache.items() if now - ts > self._FRAG_TTL
        ]
        for k in expired:
            del self._frag_cache[k]
            logger.warning("丢弃超时未凑齐的分片事件 message_id=%s", k)

        entry = self._frag_cache.get(msg_id)
        if entry is None:
            entry = (now, [None] * total)
            self._frag_cache[msg_id] = entry
        buf = entry[1]
        if 0 <= seq < len(buf):
            buf[seq] = bs
        if all(piece is not None for piece in buf):
            del self._frag_cache[msg_id]
            return b"".join(buf)  # type: ignore[arg-type]
        return None

    def _dispatch_event(self, payload: bytes) -> None:
        """解析事件 JSON，只处理 im.message.receive_v1。"""
        data = json.loads(payload.decode("utf-8"))
        header = data.get("header", {})
        event_type = header.get("event_type", "")
        if event_type != "im.message.receive_v1":
            return
        event = data.get("event", {})
        msg = self._parse_event_message(event)
        if msg is None:
            return
        if self._chat_whitelist and msg.chat_id != self._chat_whitelist:
            logger.debug("忽略非白名单群消息 chat_id=%s", msg.chat_id)
            return
        # 线程安全地交回主 loop
        fut = asyncio.run_coroutine_threadsafe(self._on_event(msg), self._main_loop)
        fut.add_done_callback(_log_future_exception)

    @staticmethod
    def _parse_event_message(event: dict) -> IncomingMessage | None:
        msg = event.get("message") or {}
        sender = event.get("sender") or {}
        # 忽略机器人自己发的消息：daemon 会 send_root_message 建话题根，若该消息被
        # 回投会触发 LLM 派发 → 死循环。多数 scope 本就不下发 bot 消息，此处防御性拦截。
        if sender.get("sender_type") == "bot":
            return None
        if msg.get("message_type") != _TEXT_MSG_TYPE:
            return None
        chat_type = msg.get("chat_type", "")
        # P0 只处理群消息（话题形式群）；单聊不支持话题，忽略。
        if chat_type != "group":
            return None
        content_raw = msg.get("content", "{}")
        try:
            content = (
                json.loads(content_raw) if isinstance(content_raw, str) else content_raw
            )
            text = content.get("text", "") if content else ""
        except json.JSONDecodeError:
            text = ""
        # 去掉 @bot / @user 前缀（飞书 text 消息里 at 表现为 @_user_N）
        import re

        text = re.sub(r"@_\w+\s*", "", text).strip()
        message_id = msg.get("message_id", "")
        root_id = msg.get("root_id")
        thread_root = root_id if root_id and root_id != message_id else None
        sender_id_obj = (sender.get("sender_id") or {}) if sender else {}
        sender_id = (
            sender_id_obj.get("open_id")
            or sender_id_obj.get("user_id")
            or sender_id_obj.get("union_id")
            or ""
        )
        return IncomingMessage(
            chat_id=msg.get("chat_id", ""),
            message_id=message_id,
            thread_root_id=thread_root,
            text=text,
            chat_type=chat_type,
            sender_id=sender_id,
        )

    # ------------------------------------------------------------------ #
    # 发消息（HTTP / REST）
    # ------------------------------------------------------------------ #

    def _get_tenant_token(self) -> str:
        """取 tenant_access_token，带过期缓存（提前 60s 刷新）。"""
        if self._tenant_token and time.time() < self._tenant_token_expires - 60:
            return self._tenant_token
        resp = self._session.post(
            self._domain + "/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"获取 tenant_access_token 失败: {body}")
        self._tenant_token = body["tenant_access_token"]
        self._tenant_token_expires = time.time() + body.get("expire", 7200)
        logger.debug("已刷新 tenant_access_token")
        return self._tenant_token

    def _im_post(self, path: str, body: dict) -> dict:
        token = self._get_tenant_token()
        resp = self._session.post(
            self._domain + path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            # 只带 code/msg，避免完整响应（含用户内容）进异常链与日志
            raise RuntimeError(
                f"飞书 IM 调用失败 path={path} "
                f"code={result.get('code')} msg={result.get('msg')}"
            )
        return result

    def send_root_message(self, chat_id: str, text: str) -> str:
        """往群里发一条根消息（= 新话题根），返回 message_id。"""
        result = self._im_post(
            "/open-apis/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )
        return result["data"]["message_id"]

    def reply_in_thread(self, root_message_id: str, text: str) -> str:
        """在话题（root_message_id）内追加回复，返回 message_id。"""
        result = self._im_post(
            f"/open-apis/im/v1/messages/{root_message_id}/reply",
            {
                "msg_type": "text",
                "content": json.dumps({"text": text}),
                "reply_in_thread": True,
            },
        )
        return result["data"]["message_id"]

    def reply_card(self, root_message_id: str, card: dict) -> str:
        """在话题内发一张 interactive 卡片，返回新消息 message_id。"""
        result = self._im_post(
            f"/open-apis/im/v1/messages/{root_message_id}/reply",
            {
                "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False),
                "reply_in_thread": True,
            },
        )
        return result["data"]["message_id"]

    def patch_card(self, message_id: str, card: dict) -> None:
        """原地更新一张已发送的卡片。"""
        self._im_patch(
            f"/open-apis/im/v1/messages/{message_id}",
            {"content": json.dumps(card, ensure_ascii=False)},
        )

    def _im_patch(self, path: str, body: dict) -> dict:
        """PATCH 飞书 REST API（卡片原地更新）。照抄 _im_post 的 token/错误处理模式。"""
        token = self._get_tenant_token()
        resp = self._session.patch(
            self._domain + path,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise RuntimeError(
                f"飞书 IM PATCH 失败 path={path} "
                f"code={result.get('code')} msg={result.get('msg')}"
            )
        return result


# ---------------------------------------------------------------------- #
# 辅助
# ---------------------------------------------------------------------- #


def _new_ping_frame(service_id: int) -> pbbp2_pb2.Frame:
    frame = pbbp2_pb2.Frame()
    header = frame.headers.add()
    header.key = HEADER_TYPE
    header.value = _MSG_PING
    frame.service = service_id
    frame.method = _FRAME_CONTROL
    frame.SeqID = 0
    frame.LogID = 0
    return frame


def _service_id_from_url(url: str) -> str | None:
    try:
        q = parse_qs(urlparse(url).query)
        return q.get("service_id", [None])[0]
    except Exception:
        return None


def _redact_url(url: str) -> str:
    """打码 conn_url 里的敏感 query 参数，用于日志。"""
    try:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}{u.path}"
    except Exception:
        return url


def _log_future_exception(fut) -> None:
    try:
        fut.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("主 loop 处理飞书消息时出错")
