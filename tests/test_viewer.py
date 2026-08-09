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
from feishu_dispatcher.config import Project
from feishu_dispatcher.viewer import (
    ViewerServer,
    file as viewer_file,
    health,
    list_projects,
    tree as viewer_tree,
)


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
    async def boom(_ctx: dict, _request: dict) -> tuple[int, dict]:
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


async def test_tree_lists_files():
    import shutil
    from pathlib import Path

    ws = Path(__file__).parent / "_ws_tree"
    ws.mkdir(exist_ok=True)
    (ws / "main.py").write_text("print(1)")
    (ws / "docs").mkdir(exist_ok=True)
    (ws / "docs" / "readme.md").write_text("# demo")
    (ws / ".git").mkdir(exist_ok=True)
    (ws / ".git" / "config").write_text("x")  # should be skipped
    fake = {"demo": Project(name="demo", path=ws)}
    vs = ViewerServer(
        "tok-view",
        routes={("GET", "/api/projects/{name}/tree"): viewer_tree},
        host="127.0.0.1",
        port=0,
        ctx={"all_projects": lambda: fake},
    )
    vs.start()
    try:
        status, payload = await asyncio.to_thread(
            _get, vs.base_url + "/api/projects/demo/tree", "tok-view"
        )
        assert status == 200
        paths = {e["path"] for e in payload["entries"]}
        assert "main.py" in paths
        assert "docs/readme.md" in paths
        assert not any(".git" in p for p in paths)  # .git skipped
    finally:
        vs.stop()
        shutil.rmtree(ws, ignore_errors=True)


async def test_tree_unknown_project_404():
    vs = ViewerServer(
        "tok-view",
        routes={("GET", "/api/projects/{name}/tree"): viewer_tree},
        host="127.0.0.1",
        port=0,
        ctx={"all_projects": lambda: {}},
    )
    vs.start()
    try:
        status, payload = await asyncio.to_thread(
            _get, vs.base_url + "/api/projects/nope/tree", "tok-view"
        )
        assert status == 404
    finally:
        vs.stop()


async def test_file_reads_text():
    import shutil
    from pathlib import Path
    from urllib.parse import quote

    ws = Path(__file__).parent / "_ws_file"
    ws.mkdir(exist_ok=True)
    # write_bytes 避免 Windows 文本模式把 \n 写成 \r\n
    (ws / "hello.py").write_bytes(b"print('hi')\n")
    fake = {"demo": Project(name="demo", path=ws)}
    vs = ViewerServer(
        "tok-view",
        routes={("GET", "/api/projects/{name}/file"): viewer_file},
        host="127.0.0.1",
        port=0,
        ctx={"all_projects": lambda: fake},
    )
    vs.start()
    try:
        url = vs.base_url + "/api/projects/demo/file?path=" + quote("hello.py")
        status, payload = await asyncio.to_thread(_get, url, "tok-view")
        assert status == 200
        assert payload == {
            "path": "hello.py",
            "rev": "work",
            "binary": False,
            "content": "print('hi')\n",
        }
    finally:
        vs.stop()
        shutil.rmtree(ws, ignore_errors=True)


async def test_file_rejects_path_traversal():
    import shutil
    from pathlib import Path
    from urllib.parse import quote

    ws = Path(__file__).parent / "_ws_file_trav"
    ws.mkdir(exist_ok=True)
    (ws / "ok.txt").write_text("x", encoding="utf-8")
    fake = {"demo": Project(name="demo", path=ws)}
    vs = ViewerServer(
        "tok-view",
        routes={("GET", "/api/projects/{name}/file"): viewer_file},
        host="127.0.0.1",
        port=0,
        ctx={"all_projects": lambda: fake},
    )
    vs.start()
    try:
        # 相对逃逸 + 绝对路径均 400（决策 D9）
        for bad in ("../ok.txt", "/etc/passwd"):
            url = vs.base_url + "/api/projects/demo/file?path=" + quote(bad)
            status, _ = await asyncio.to_thread(_get, url, "tok-view")
            assert status == 400, bad
    finally:
        vs.stop()
        shutil.rmtree(ws, ignore_errors=True)


async def test_file_binary_flag():
    import shutil
    from pathlib import Path
    from urllib.parse import quote

    ws = Path(__file__).parent / "_ws_file_bin"
    ws.mkdir(exist_ok=True)
    (ws / "blob.bin").write_bytes(b"\x00\x01\x02\xff")
    fake = {"demo": Project(name="demo", path=ws)}
    vs = ViewerServer(
        "tok-view",
        routes={("GET", "/api/projects/{name}/file"): viewer_file},
        host="127.0.0.1",
        port=0,
        ctx={"all_projects": lambda: fake},
    )
    vs.start()
    try:
        url = vs.base_url + "/api/projects/demo/file?path=" + quote("blob.bin")
        status, payload = await asyncio.to_thread(_get, url, "tok-view")
        assert status == 200
        assert payload["binary"] is True
        assert payload["content"] == ""
    finally:
        vs.stop()
        shutil.rmtree(ws, ignore_errors=True)
