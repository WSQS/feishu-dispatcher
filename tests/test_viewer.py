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

import shutil
import subprocess
import tempfile
from pathlib import Path

from feishu_dispatcher import __version__
from feishu_dispatcher.config import Project
from feishu_dispatcher.viewer import ViewerServer, diff as viewer_diff, health, list_projects
from feishu_dispatcher.viewer import tree as viewer_tree


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


def _init_git_repo(ws: Path) -> None:
    """在 ws 建最小 git repo：提交 main.py，再改它（产生工作区 diff）。"""
    ws.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(ws), "config", "user.name", "t"], check=True)
    (ws / "main.py").write_text("print('v1')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(ws), "add", "."], check=True)
    subprocess.run(["git", "-C", str(ws), "commit", "-q", "-m", "init"], check=True)
    (ws / "main.py").write_text("print('v2')\n", encoding="utf-8")


async def test_tree_lists_files():
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


async def test_diff_returns_workdir_vs_head():
    ws = Path(__file__).parent / "_ws_diff"
    _init_git_repo(ws)
    try:
        vs = ViewerServer(
            "tok-view",
            routes={("GET", "/api/projects/{name}/diff"): viewer_diff},
            host="127.0.0.1",
            port=0,
            ctx={"all_projects": lambda: {"demo": Project(name="demo", path=ws)}},
        )
        vs.start()
        try:
            status, payload = await asyncio.to_thread(
                _get, vs.base_url + "/api/projects/demo/diff", "tok-view"
            )
            assert status == 200
            files = {f["path"]: f for f in payload["files"]}
            assert "main.py" in files
            assert files["main.py"]["status"] == "M"
            assert "v1" in files["main.py"]["patch"]
            assert "v2" in files["main.py"]["patch"]
        finally:
            vs.stop()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


async def test_diff_non_git_returns_500():
    # 必须落在仓库外：tests/ 在 git worktree 内，git -C 会误判为仓内路径。
    ws = Path(tempfile.mkdtemp(prefix="fdx-diff-nongit-"))
    (ws / "main.py").write_text("x\n", encoding="utf-8")
    try:
        vs = ViewerServer(
            "tok-view",
            routes={("GET", "/api/projects/{name}/diff"): viewer_diff},
            host="127.0.0.1",
            port=0,
            ctx={"all_projects": lambda: {"demo": Project(name="demo", path=ws)}},
        )
        vs.start()
        try:
            status, payload = await asyncio.to_thread(
                _get, vs.base_url + "/api/projects/demo/diff", "tok-view"
            )
            assert status == 500
            assert "error" in payload
        finally:
            vs.stop()
    finally:
        shutil.rmtree(ws, ignore_errors=True)


async def test_diff_unknown_project_404():
    vs = ViewerServer(
        "tok-view",
        routes={("GET", "/api/projects/{name}/diff"): viewer_diff},
        host="127.0.0.1",
        port=0,
        ctx={"all_projects": lambda: {}},
    )
    vs.start()
    try:
        status, _payload = await asyncio.to_thread(
            _get, vs.base_url + "/api/projects/nope/diff", "tok-view"
        )
        assert status == 404
    finally:
        vs.stop()
