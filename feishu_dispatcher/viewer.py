"""移动端查看器：daemon 内嵌的只读 HTTP 服务，给手机看 workspace 的文件树/文件/diff。

与 ``control.py`` 是同一个模子（daemon 进程内 ``ThreadingHTTPServer`` + Bearer token
鉴权），区别在于：

- ``control.py`` 绑 ``127.0.0.1`` 随机端口、写（``POST /v1/bg/run``）、给 agent 子进程的
  ``fdx`` 回调；token 按 task 一一映射，请求天然带「我是哪个 task」。
- viewer 绑**固定可达地址**（局域网 / Tailscale 网卡，手机要连）、**只读**（``GET``）、
  给手机；单一全局 token，比对相等即放行。

本模块只负责「起一个带鉴权的只读 HTTP 底座」。具体的数据接口（projects / tree / file /
diff）逐个往上加；本文件 v1 只实现 ``GET /api/health``。

安全：只读、无副作用；每个请求要求 ``Authorization: Bearer <token>``，token 与配置的
``[viewer] token`` 相等才放行，挡掉本机/同网段其它进程。绑定地址由配置决定，私有网络
（局域网 / Tailscale / ZeroTier）下不做 TLS。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from feishu_dispatcher import __version__

logger = logging.getLogger(__name__)

#: 只读查看器的路由处理器：``() -> (http_status, response_dict)``。
#: 与 control.py 不同，viewer 的数据接口大多不碰 daemon 主 loop 的状态（M1 只 health），
#: 故同步即可；将来某个接口要读 store 时再决定是否 marshal 回主 loop。
HealthHandler = Callable[[], tuple[int, dict]]


class ViewerServer:
    """只读查看器 HTTP 服务。``routes`` 是 ``(METHOD, path) -> handler`` 表。

    单一全局 ``token``；任何请求（含 ``/api/health``）都要求正确 bearer token——
    一来与 control.py 保持一致的鉴权口径，二来避免未授权方探测到「这里有个服务」。
    """

    def __init__(
        self,
        token: str,
        routes: dict[tuple[str, str], HealthHandler],
        *,
        host: str = "0.0.0.0",
        port: int = 0,
    ) -> None:
        self._token = token
        self._routes = routes
        self._server = ThreadingHTTPServer((host, port), _make_handler(self))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="viewer-http", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("移动端查看器已启动: %s", self.base_url)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        """停 HTTP server（阻塞调用）。两步拆成独立 try：``shutdown()`` 万一卡/抛也不
        该跳过 ``server_close()`` 释放监听 socket。serve_forever 跑在 daemon 线程，
        最坏情况随进程退出回收。与 control.py 的停法一致。
        """
        try:
            self._server.shutdown()
        except Exception:
            logger.debug("查看器 shutdown 异常（忽略）", exc_info=True)
        try:
            self._server.server_close()
        except Exception:
            logger.debug("查看器 server_close 异常（忽略）", exc_info=True)

    def dispatch(self, method: str, path: str, token: str) -> tuple[int, dict]:
        """在 handler 线程里调用：鉴权 → 找路由 → 执行。"""
        if token != self._token:
            return 401, {"error": "invalid or missing token"}
        handler = self._routes.get((method, path))
        if handler is None:
            return 404, {"error": f"no route for {method} {path}"}
        try:
            return handler()
        except Exception as exc:
            logger.exception("查看器处理失败 %s %s", method, path)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


def health() -> tuple[int, dict]:
    """``GET /api/health``：存活探针 + 版本，供安卓端确认连得上、对得上版本。"""
    return 200, {"ok": True, "version": __version__}


def _make_handler(vs: ViewerServer):
    class _Handler(BaseHTTPRequestHandler):
        # 静音默认往 stderr 打的访问日志（daemon 有自己的日志）
        def log_message(self, *args) -> None:  # noqa: D401
            return None

        def _token(self) -> str:
            auth = self.headers.get("Authorization", "") or ""
            return auth[len("Bearer ") :].strip() if auth.startswith("Bearer ") else ""

        def _respond(self, status: int, payload: dict) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _path(self) -> str:
            return self.path.split("?", 1)[0]

        def do_GET(self) -> None:  # noqa: N802
            status, payload = vs.dispatch("GET", self._path(), self._token())
            self._respond(status, payload)

    return _Handler
