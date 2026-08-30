"""daemon 的本地控制面：127.0.0.1 HTTP，供 agent 侧 CLI（fdx）请求 daemon 执行操作。

第一个用途是后台任务（bg，#68），但刻意做成通用「路由表 + Bearer token 鉴权」——加别的
「方向」endpoint 只需往路由表塞一条 ``(method, path) -> handler``，CLI 那边加一个子命令。

在后台线程跑 ``ThreadingHTTPServer``（同 feishu.py 的「线程 + run_coroutine_threadsafe」
模式），HTTP handler 把实际工作 marshal 回主 event loop 执行——所有 daemon 状态
（stores / sessions）都只在主 loop 上单线程访问，避免竞态。

安全：只绑 127.0.0.1；每个请求要求 ``Authorization: Bearer <token>``，token 由 daemon 启
agent 时一次性下发并映射到 task_id，故请求天然携带「我是哪个 task」的身份，无从冒充别的
task，也挡掉本机其它进程。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Coroutine
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger(__name__)

#: 路由处理器：async (task_id, body) -> (http_status, response_dict)。在主 loop 上执行。
RouteHandler = Callable[[str, dict], Coroutine[Any, Any, tuple[int, dict]]]
#: token → task_id（无效/未知 token 返回 None）
TokenResolver = Callable[[str], "str | None"]

#: 请求体上限，防异常大 body 撑爆内存
_MAX_BODY = 1_000_000
#: 单个请求处理的最长等待（handler 只应做「登记 + 起 watcher」这类快操作）
_DISPATCH_TIMEOUT = 30.0


class ControlServer:
    """本地 HTTP 控制面。``routes`` 是 ``(METHOD, path) -> async handler`` 表。"""

    def __init__(
        self,
        main_loop: asyncio.AbstractEventLoop,
        resolve_token: TokenResolver,
        routes: dict[tuple[str, str], RouteHandler],
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self._loop = main_loop
        self._resolve = resolve_token
        self._routes = routes
        self._server = ThreadingHTTPServer((host, 0), _make_handler(self))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="control-http", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("本地控制面已启动: %s", self.base_url)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        """停 HTTP server（阻塞调用）。**调用方勿在事件循环线程上直接调它**——
        ``shutdown()`` 会阻塞等 serve_forever 确认，冻住 loop 且可能与正回等主
        loop 的 handler 线程死锁；daemon 用 ``asyncio.to_thread`` + 超时包起来。

        两步拆成独立 try：``shutdown()`` 万一卡/抛也不该跳过 ``server_close()``
        释放监听 socket。serve_forever 跑在 daemon 线程，最坏情况随进程退出回收。
        """
        try:
            self._server.shutdown()
        except Exception:
            logger.debug("控制面 shutdown 异常（忽略）", exc_info=True)
        try:
            self._server.server_close()
        except Exception:
            logger.debug("控制面 server_close 异常（忽略）", exc_info=True)

    def dispatch(
        self, method: str, path: str, token: str, body: dict
    ) -> tuple[int, dict]:
        """在 handler 线程里调用：鉴权 → 找路由 → marshal 到主 loop 执行。"""
        task_id = self._resolve(token) if token else None
        if task_id is None:
            return 401, {"error": "invalid or missing token"}
        handler = self._routes.get((method, path))
        if handler is None:
            return 404, {"error": f"no route for {method} {path}"}
        fut = asyncio.run_coroutine_threadsafe(handler(task_id, body), self._loop)
        try:
            return fut.result(timeout=_DISPATCH_TIMEOUT)
        except Exception as exc:
            logger.exception("控制面处理失败 %s %s", method, path)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


def _make_handler(cs: ControlServer):
    class _Handler(BaseHTTPRequestHandler):
        # 静音默认往 stderr 打的访问日志（daemon 有自己的日志）
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002, D401
            return None

        def _token(self) -> str:
            auth = self.headers.get("Authorization", "") or ""
            return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""

        def _read_body(self) -> dict | None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return {}
            if length > _MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return None

        def _respond(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _path(self) -> str:
            return self.path.split("?", 1)[0]

        def do_POST(self) -> None:  # noqa: N802
            body = self._read_body()
            if body is None:
                self._respond(400, {"error": "bad or too-large JSON body"})
                return
            status, payload = cs.dispatch("POST", self._path(), self._token(), body)
            self._respond(status, payload)

        def do_GET(self) -> None:  # noqa: N802
            status, payload = cs.dispatch("GET", self._path(), self._token(), {})
            self._respond(status, payload)

    return _Handler
