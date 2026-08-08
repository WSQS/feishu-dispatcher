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

import asyncio
import json
import logging
import threading
from collections.abc import Awaitable, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from feishu_dispatcher import __version__
from feishu_dispatcher._git import diff_workdir, list_files
from feishu_dispatcher._paths import PathTraversalError, resolve_under_root

logger = logging.getLogger(__name__)

#: 路由处理器：async (ctx, request) -> (status, dict)，在主 loop 上执行（经 dispatch marshal 回来）。
#: - ctx：daemon 上下文（stores、_all_projects、resolve_project 等），handler 用来读状态。
#: - request：{"path": str, "query": dict[str,str], "segments": dict[str,str]}。query 是 URL
#:   查询参数；segments 是路由路径里的占位符值（dispatch 按 route 模板提取，见 _match_route）。
RouteHandler = Callable[[dict, dict], Awaitable[tuple[int, dict]]]

#: 单个请求在主 loop 上处理的最长等待（store 读/git 调用都该很快）
_DISPATCH_TIMEOUT = 30.0


class ViewerServer:
    """只读查看器 HTTP 服务。``routes`` 是 ``(METHOD, path) -> handler`` 表。

    单一全局 ``token``；任何请求（含 ``/api/health``）都要求正确 bearer token——
    一来与 control.py 保持一致的鉴权口径，二来避免未授权方探测到「这里有个服务」。
    """

    def __init__(
        self,
        token: str,
        routes: dict[tuple[str, str], RouteHandler],
        *,
        host: str = "0.0.0.0",
        port: int = 0,
        main_loop: asyncio.AbstractEventLoop | None = None,
        ctx: dict | None = None,
    ) -> None:
        self._token = token
        self._routes = routes
        #: daemon 主 loop 引用；handler 经 run_coroutine_threadsafe marshal 回主 loop 执行
        #: （决策 Q4，stores 只在主 loop 单线程访问）。默认 None 让底座单测不必构造 loop。
        self._loop = main_loop
        #: daemon 上下文（stores、_all_projects 等），注入给 handler 用。底座单测可不传。
        self._ctx = ctx or {}
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

    def dispatch(self, method: str, path: str, query: str, token: str) -> tuple[int, dict]:
        """在 handler 线程里调用：鉴权 → 模板匹配路由 → marshal 到主 loop 执行 async handler。

        - query 是原始查询串（``?a=b&c=d`` 之后的），解析成 dict 传给 handler。
        - 路由模板含 ``{name}`` 占位符（如 ``/api/projects/{name}/tree``），匹配后提取进
          request.segments。
        """
        if token != self._token:
            return 401, {"error": "invalid or missing token"}
        match = _match_route(self._routes, method, path)
        if match is None:
            return 404, {"error": f"no route for {method} {path}"}
        handler, segments = match
        request = {
            "path": path,
            "query": _parse_query(query),
            "segments": segments,
        }
        try:
            coro = handler(self._ctx, request)
            if self._loop is not None:
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
                return fut.result(timeout=_DISPATCH_TIMEOUT)
            return asyncio.new_event_loop().run_until_complete(coro)
        except Exception as exc:
            logger.exception("查看器处理失败 %s %s", method, path)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


async def health(_ctx: dict, _request: dict) -> tuple[int, dict]:
    """``GET /api/health``：存活探针 + 版本，供安卓端确认连得上、对得上版本。

    不读 store（ctx 不用），但统一 async 签名（dispatch 全走 marshal）。
    """
    return 200, {"ok": True, "version": __version__}


async def list_projects(ctx: dict, _request: dict) -> tuple[int, dict]:
    """``GET /api/projects``：列出所有 project（合并 config 种子 + 运行时注册，决策 Q5）。

    ctx 须含 ``all_projects``：一个返回 ``dict[str, Project]`` 的可调用（daemon 的
    ``_all_projects``）。在主 loop 上执行（经 dispatch marshal），store 访问安全。
    """
    all_projects = ctx.get("all_projects")
    if all_projects is None:
        return 500, {"error": "all_projects 未注入 ctx"}
    items = [
        {"name": p.name, "path": str(p.path), "default_agent": p.default_agent}
        for p in all_projects().values()
    ]
    return 200, {"items": items}


async def tree(ctx: dict, request: dict) -> tuple[int, dict]:
    """``GET /api/projects/{name}/tree?untracked=0``：列 project workspace 的文件树。

    git 仓用 git ls-files（untracked=1 再加 git status ??）；非仓降级 os.walk（决策 D10）。
    """
    name = request["segments"]["name"]
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):  # 错误响应 (status, dict)
        return ws
    untracked = request["query"].get("untracked", "0") == "1"
    try:
        entries = list_files(ws, untracked=untracked)
    except Exception as exc:
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 200, {"entries": entries}


async def file(ctx: dict, request: dict) -> tuple[int, dict]:
    """``GET /api/projects/{name}/file?path=<rel>``：读 workspace 内某文件内容（决策 D8/Q5）。

    path 经 resolve_under_root 校验（决策 D9，拒越界）。binary 检测靠扩展名黑名单（够用）。
    """
    name = request["segments"]["name"]
    rel = request["query"].get("path", "")
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):
        return ws
    try:
        abs_path = resolve_under_root(ws, rel)
    except PathTraversalError as e:
        return 400, {"error": str(e)}
    if not abs_path.is_file():
        return 404, {"error": f"不是文件或不存在: {rel}"}
    binary_exts = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".so", ".dll", ".class"}
    is_binary = abs_path.suffix.lower() in binary_exts
    if is_binary:
        return 200, {"path": rel, "binary": True}
    return 200, {"path": rel, "binary": False, "text": abs_path.read_text(encoding="utf-8")}


async def diff(ctx: dict, request: dict) -> tuple[int, dict]:
    """``GET /api/projects/{name}/diff``：工作区 vs HEAD 的 diff（决策 D7，原样给，Q7）。"""
    name = request["segments"]["name"]
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):
        return ws
    try:
        files = diff_workdir(ws)
    except Exception as exc:
        return 500, {"error": f"{type(exc).__name__}: {exc}"}
    return 200, {"files": files}


def _resolve_workspace(ctx: dict, name: str) -> "Path | tuple[int, dict]":
    """按 project name 查 workspace 路径；project 不存在返回错误响应 tuple。"""
    all_projects = ctx.get("all_projects")
    if all_projects is None:
        return 500, {"error": "all_projects 未注入 ctx"}
    p = all_projects().get(name)
    if p is None:
        return 404, {"error": f"未知 project: {name}"}
    return p.path


def _match_route(
    routes: dict[tuple[str, str], RouteHandler], method: str, path: str
) -> "tuple[RouteHandler, dict[str, str]] | None":
    """匹配 (method, path) 到路由模板（含 {name} 占位符）。返回 (handler, segments) 或 None。

    精确匹配优先于模板匹配（/api/health 不会被 /api/{x} 吞掉）。
    """
    # 先试精确
    exact = routes.get((method, path))
    if exact is not None:
        return exact, {}
    # 模板匹配
    path_parts = path.strip("/").split("/")
    for (m, tmpl), handler in routes.items():
        if m != method or "{" not in tmpl:
            continue
        tmpl_parts = tmpl.strip("/").split("/")
        if len(tmpl_parts) != len(path_parts):
            continue
        segments: dict[str, str] = {}
        ok = True
        for tp, pp in zip(tmpl_parts, path_parts, strict=True):
            if tp.startswith("{") and tp.endswith("}"):
                segments[tp[1:-1]] = pp
            elif tp != pp:
                ok = False
                break
        if ok:
            return handler, segments
    return None


def _parse_query(query: str) -> dict[str, str]:
    """``a=b&c=d`` → ``{a:b, c:d}``（取每个 key 的第一个值，单值场景够用）。"""
    if not query:
        return {}
    pairs = parse_qs(query, keep_blank_values=True)
    return {k: v[0] for k, v in pairs.items()}


def _make_handler(vs: ViewerServer):
    class _Handler(BaseHTTPRequestHandler):
        # 静音 BaseHTTPRequestHandler 默认往 stderr 打的访问日志；
        # 改用 logger.info 在 do_GET 里记（格式统一、走 daemon 日志通道）。
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

        def _split(self) -> tuple[str, str]:
            """返回 (path, query) —— path 不含 ?，query 是 ? 之后的串（不含 ?）。"""
            p, _, q = self.path.partition("?")
            return p, q

        def do_GET(self) -> None:  # noqa: N802
            path, query = self._split()
            status, payload = vs.dispatch("GET", path, query, self._token())
            logger.info("viewer GET %s → %d", path, status)
            self._respond(status, payload)

    return _Handler
