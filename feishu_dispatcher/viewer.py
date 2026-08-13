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
from urllib.parse import parse_qs

from pathlib import Path

from feishu_dispatcher import __version__
from feishu_dispatcher._paths import PathTraversalError, resolve_tree_path, resolve_under_root

logger = logging.getLogger(__name__)

#: 路由处理器：async (ctx, request) -> (status, dict)，在主 loop 上执行（经 dispatch marshal 回来）。
#: - ctx：daemon 上下文（跨请求不变）。
#: - request：{"path": str, "query": dict[str,str], "segments": dict[str,str]}（每次请求不同）。
RouteHandler = Callable[[dict, dict], Awaitable[tuple[int, dict]]]

#: 单个请求在主 loop 上处理的最长等待（store 读/git 调用都该很快）
_DISPATCH_TIMEOUT = 30.0
#: 单个文件预览的内容上限，避免 daemon 与移动端为大文件分配过多内存
_MAX_FILE_BYTES = 1_000_000


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
        """鉴权 → 模板匹配路由 → marshal 到主 loop 执行 async handler。

        handler 是 async (ctx, request) -> (status, dict)。query 解析成 dict；
        路由模板含 {name} 占位符，匹配后提取进 request.segments。
        """
        if token != self._token:
            return 401, {"error": "invalid or missing token"}
        match = _match_route(self._routes, method, path)
        if match is None:
            return 404, {"error": f"no route for {method} {path}"}
        handler, segments = match
        request = {"path": path, "query": _parse_query(query), "segments": segments}
        try:
            coro = handler(self._ctx, request)
            if self._loop is not None:
                # 照 control.py：marshal 回主 loop，等结果（handler 只做快操作）
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
                return fut.result(timeout=_DISPATCH_TIMEOUT)
            # 无主 loop（单测）：用一个临时 loop 跑（仅测试路径）
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
    """``GET /api/projects/{name}/tree``：列 project workspace 的文件树（os.walk）。"""
    name = request["segments"]["name"]
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):
        return ws
    ignore = {".git", ".venv", "build", "node_modules", "__pycache__"}
    entries = []
    for root, dirs, fnames in ws.walk():
        dirs[:] = [d for d in dirs if d not in ignore]
        for f in fnames:
            full = root / f
            entries.append({"path": full.relative_to(ws).as_posix(), "type": "file", "size": full.stat().st_size})
    entries.sort(key=lambda x: x["path"])
    return 200, {"entries": entries}


async def tree_children(ctx: dict, request: dict) -> tuple[int, dict]:
    """``GET /api/projects/{name}/tree/children?path=``：列指定目录的直接子项（按目录加载）。

    经 ``ctx["scan_executor"]`` 在 worker 线程跑，不占主 loop。``path`` 是必填 query 键，
    空串 = workspace 根。错误：语法/逃逸 → 400，不存在 → 404，是文件 → 400，无权限 → 403。
    """
    name = request["segments"]["name"]
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):
        return ws
    query = request["query"]
    if "path" not in query:
        return 400, {"error": "missing path parameter"}
    path = query["path"]
    try:
        dir_path = resolve_tree_path(ws, path)
    except PathTraversalError as exc:
        return 400, {"error": str(exc)}
    executor = ctx.get("scan_executor")
    if executor is None:
        return 500, {"error": "scan_executor not in ctx"}
    try:
        entries = await executor.run(dir_path, path)
    except FileNotFoundError:
        return 404, {"error": f"not found: {path}"}
    except NotADirectoryError:
        return 400, {"error": f"not a directory: {path}"}
    except PermissionError:
        return 403, {"error": f"permission denied: {path}"}
    return 200, {"path": path, "entries": entries}


async def file(ctx: dict, request: dict) -> tuple[int, dict]:
    """``GET /api/projects/{name}/file?path=&rev=work``：读工作区文件内容。

    返回 ``{path, rev, binary, content}``。``binary=true`` 时 ``content`` 为空串（客户端
    提示不可预览）；文本按 UTF-8 解码，失败亦当 binary。路径过 ``resolve_under_root``。
    """
    name = request["segments"]["name"]
    rel = request["query"].get("path", "")
    rev = request["query"].get("rev", "work")
    if rev != "work":
        return 400, {"error": f"unsupported rev: {rev} (v1 仅 work)"}
    ws = _resolve_workspace(ctx, name)
    if isinstance(ws, tuple):
        return ws
    try:
        resolved = resolve_under_root(ws, rel)
    except PathTraversalError as exc:
        return 400, {"error": str(exc)}
    if not resolved.is_file():
        return 404, {"error": f"not a file: {rel}"}
    with resolved.open("rb") as stream:
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        return 413, {"error": f"file too large (max {_MAX_FILE_BYTES} bytes)"}
    # 空文件当文本；含 NUL 或 UTF-8 解不开 → binary
    if data and (b"\x00" in data[:8192]):
        return 200, {"path": rel, "rev": rev, "binary": True, "content": ""}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return 200, {"path": rel, "rev": rev, "binary": True, "content": ""}
    return 200, {"path": rel, "rev": rev, "binary": False, "content": text}


def _resolve_workspace(ctx: dict, name: str) -> "Path | tuple[int, dict]":
    """按 project name 查 workspace 路径；project 不存在返回错误响应 tuple。"""
    all_projects = ctx.get("all_projects")
    if all_projects is None:
        return 500, {"error": "all_projects not in ctx"}
    p = all_projects().get(name)
    if p is None:
        return 404, {"error": f"unknown project: {name}"}
    return p.path


def _match_route(
    routes: dict[tuple[str, str], RouteHandler], method: str, path: str
) -> "tuple[RouteHandler, dict[str, str]] | None":
    """匹配 (method, path) 到路由（含 {name} 占位符的模板路由）。返回 (handler, segments) 或 None。

    精确匹配优先于模板匹配。
    """
    exact = routes.get((method, path))
    if exact is not None:
        return exact, {}
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
    """``a=b&c=d`` → ``{a:b, c:d}``（取每个 key 第一个值，单值场景够用）。"""
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
            """返回 (path, query) — path 不含 ?，query 是 ? 之后的串。"""
            p, _, q = self.path.partition("?")
            return p, q

        def do_GET(self) -> None:  # noqa: N802
            path, query = self._split()
            status, payload = vs.dispatch("GET", path, query, self._token())
            logger.info("viewer GET %s → %d", path, status)
            self._respond(status, payload)

    return _Handler
