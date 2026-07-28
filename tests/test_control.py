"""本地控制面 ControlServer 的 HTTP 往返测试（真起 127.0.0.1 server + urllib 请求）。

注意：HTTP 请求是阻塞调用，且 handler 经 run_coroutine_threadsafe 回主 loop 执行——
故请求必须放到线程里（asyncio.to_thread），否则会阻塞主 loop、marshaled 协程无法运行
而死锁。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from feishu_dispatcher.control import ControlServer


def _post(url: str, payload: dict, token: str | None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


async def _make_server(routes, tokens):
    loop = asyncio.get_running_loop()
    cs = ControlServer(loop, resolve_token=tokens.get, routes=routes)
    cs.start()
    return cs


async def test_valid_token_routes_to_handler():
    seen: list = []

    async def handler(task_id: str, body: dict):
        seen.append((task_id, body))
        return 200, {"echo": body.get("v"), "task": task_id}

    cs = await _make_server({("POST", "/v1/thing"): handler}, {"tok-abc": "t7"})
    try:
        status, payload = await asyncio.to_thread(
            _post, cs.base_url + "/v1/thing", {"v": 42}, "tok-abc"
        )
        assert status == 200
        assert payload == {"echo": 42, "task": "t7"}
        assert seen == [("t7", {"v": 42})]
    finally:
        cs.stop()


async def test_missing_or_invalid_token_rejected():
    async def handler(task_id: str, body: dict):
        return 200, {"ok": True}

    cs = await _make_server({("POST", "/v1/thing"): handler}, {"good": "t1"})
    try:
        s_missing, _ = await asyncio.to_thread(
            _post, cs.base_url + "/v1/thing", {}, None
        )
        s_bad, _ = await asyncio.to_thread(_post, cs.base_url + "/v1/thing", {}, "nope")
        assert s_missing == 401
        assert s_bad == 401
    finally:
        cs.stop()


async def test_unknown_route_404():
    cs = await _make_server({}, {"good": "t1"})
    try:
        status, payload = await asyncio.to_thread(
            _post, cs.base_url + "/v1/missing", {}, "good"
        )
        assert status == 404
    finally:
        cs.stop()


async def test_handler_exception_becomes_500():
    async def boom(task_id: str, body: dict):
        raise RuntimeError("kaboom")

    cs = await _make_server({("POST", "/v1/x"): boom}, {"good": "t1"})
    try:
        status, payload = await asyncio.to_thread(
            _post, cs.base_url + "/v1/x", {}, "good"
        )
        assert status == 500
        assert "kaboom" in payload.get("error", "")
    finally:
        cs.stop()


async def test_fdx_cli_bg_run_end_to_end():
    """真跑 fdx（agent_cli）子进程 → 真 HTTP → ControlServer 路由，验证 CLI 二进制。

    子进程是独立进程，其阻塞 urllib 调用不占测试 loop；我们经 to_thread 等它，
    loop 空出来跑被 marshal 回来的 handler。
    """
    seen: list = []

    async def bg_run(task_id: str, body: dict):
        seen.append((task_id, body.get("command")))
        return 200, {"job_id": "j99", "status": "running"}

    cs = await _make_server({("POST", "/v1/bg/run"): bg_run}, {"tok-xyz": "t3"})
    env = {
        **os.environ,
        "FEISHU_DISPATCHER_URL": cs.base_url,
        "FEISHU_DISPATCHER_TOKEN": "tok-xyz",
    }
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "feishu_dispatcher.agent_cli",
                "bg",
                "run",
                "--",
                "python",
                "-c",
                "print(1)",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "j99" in proc.stdout
        assert seen == [("t3", ["python", "-c", "print(1)"])]
    finally:
        cs.stop()


async def test_fdx_cli_bg_logs_end_to_end():
    """真跑 `fdx bg logs j3 --tail 5` 子进程 → HTTP → 校验参数解析与输出打印。"""
    seen: list = []

    async def bg_logs(task_id: str, body: dict):
        seen.append((task_id, body))
        return 200, {
            "job_id": "j3",
            "status": "running",
            "exit_code": None,
            "command": "python train.py",
            "output": "epoch 1\nepoch 2",
        }

    cs = await _make_server({("POST", "/v1/bg/logs"): bg_logs}, {"tok-xyz": "t3"})
    env = {
        **os.environ,
        "FEISHU_DISPATCHER_URL": cs.base_url,
        "FEISHU_DISPATCHER_TOKEN": "tok-xyz",
    }
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            [
                sys.executable,
                "-m",
                "feishu_dispatcher.agent_cli",
                "bg",
                "logs",
                "j3",
                "--tail",
                "5",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        assert "epoch 2" in proc.stdout  # 输出被打印
        assert seen == [("t3", {"id": "j3", "tail": 5})]  # 参数正确解析并送达
    finally:
        cs.stop()
