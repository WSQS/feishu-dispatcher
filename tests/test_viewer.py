"""移动端查看器 ViewerServer 的 HTTP 往返测试（真起 127.0.0.1 server + urllib 请求）。

照 test_control.py 的模式：HTTP 请求经 asyncio.to_thread 发出，避免阻塞测试 loop。
本文件只覆盖 M1 的「只读底座」（health + 鉴权 + 404 + 500）；数据接口（projects /
tree / file / diff）的测试随各自 landing 补。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

from feishu_dispatcher import __version__
from feishu_dispatcher.viewer import ViewerServer, health, list_projects


def _get(url: str, token: str | None) -> tuple[int, dict]:
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


async def _make_server(token: str = "tok-view", routes=None) -> ViewerServer:
    routes = routes if routes is not None else {("GET", "/api/health"): health}
    vs = ViewerServer(token, routes, host="127.0.0.1", port=0)
    vs.start()
    return vs


async def test_health_returns_version():
    vs = await _make_server()
    try:
        status, payload = await asyncio.to_thread(_get, vs.base_url + "/api/health", "tok-view")
        assert status == 200
        assert payload == {"ok": True, "version": __version__}
    finally:
        vs.stop()


async def test_missing_or_invalid_token_rejected():
    vs = await _make_server()
    try:
        s_missing, _ = await asyncio.to_thread(_get, vs.base_url + "/api/health", None)
        s_bad, _ = await asyncio.to_thread(_get, vs.base_url + "/api/health", "nope")
        assert s_missing == 401
        assert s_bad == 401
    finally:
        vs.stop()


async def test_unknown_route_404():
    vs = await _make_server()
    try:
        status, payload = await asyncio.to_thread(_get, vs.base_url + "/api/missing", "tok-view")
        assert status == 404
    finally:
        vs.stop()


async def test_handler_exception_becomes_500():
    async def boom(_ctx: dict) -> tuple[int, dict]:
        raise RuntimeError("kaboom")

    vs = await _make_server(routes={("GET", "/api/x"): boom})
    try:
        status, payload = await asyncio.to_thread(_get, vs.base_url + "/api/x", "tok-view")
        assert status == 500
        assert "kaboom" in payload.get("error", "")
    finally:
        vs.stop()


async def test_list_projects_returns_items():
    # ctx 注入假的 all_projects：返回一个 project dict
    from feishu_dispatcher.config import Project

    fake = {
        "demo": Project(name="demo", path="/tmp/demo"),
        "lib": Project(name="lib", path="/tmp/lib", default_agent="opencode"),
    }
    vs = ViewerServer(
        "tok-view",
        routes={("GET", "/api/projects"): list_projects},
        host="127.0.0.1",
        port=0,
        ctx={"all_projects": lambda: fake},
    )
    vs.start()
    try:
        status, payload = await asyncio.to_thread(
            _get, vs.base_url + "/api/projects", "tok-view"
        )
        assert status == 200
        names = {p["name"] for p in payload["items"]}
        assert names == {"demo", "lib"}
        # default_agent 兜底 copilot + 显式 opencode 都正确
        agents = {p["name"]: p["default_agent"] for p in payload["items"]}
        assert agents == {"demo": "copilot", "lib": "opencode"}
    finally:
        vs.stop()
