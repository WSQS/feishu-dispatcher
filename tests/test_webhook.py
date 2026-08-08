"""CI 失败 webhook（#54）测试。

两类：
1. 纯函数——HMAC 校验、GitHub payload 解析、项目匹配、run_id 去重（无需起 server）。
2. HTTP 往返（照 test_control.py / test_viewer.py）：真起 127.0.0.1 server + urllib
   请求经 asyncio.to_thread 发出，避免阻塞测试 loop。handler 经 marshal 回主 loop 执行。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import urllib.error
import urllib.request

from feishu_dispatcher.config import Project
from feishu_dispatcher.webhook import (
    DedupCache,
    WebhookServer,
    match_project_by_repo,
    parse_github_payload,
    verify_signature,
)

# --------------------------------------------------------------------------- #
# HMAC 签名校验
# --------------------------------------------------------------------------- #


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_valid():
    body = b'{"hello":"world"}'
    assert verify_signature("s3cr3t", body, _sig("s3cr3t", body)) is True


def test_verify_signature_invalid():
    body = b'{"hello":"world"}'
    assert verify_signature("s3cr3t", body, _sig("other", body)) is False
    # 篡改 body
    assert verify_signature("s3cr3t", b'{"x":1}', _sig("s3cr3t", body)) is False


def test_verify_signature_missing_or_empty_secret():
    body = b"{}"
    # 空密钥 → 一律拒绝（避免无鉴权端点开公网）
    assert verify_signature("", body, _sig("s3cr3t", body)) is False
    # 缺签名头
    assert verify_signature("s3cr3t", body, "") is False
    # 格式错（无 sha256= 前缀）
    assert verify_signature("s3cr3t", body, "deadbeef") is False


# --------------------------------------------------------------------------- #
# GitHub payload 解析
# --------------------------------------------------------------------------- #


def _workflow_run(conclusion: str, run_id: int = 42, branch: str = "main") -> dict:
    return {
        "action": "completed",
        "workflow": {"name": "CI"},
        "workflow_run": {
            "id": run_id,
            "name": "CI",
            "conclusion": conclusion,
            "head_branch": branch,
            "html_url": "https://github.com/owner/repo/actions/runs/42",
        },
        "repository": {
            "full_name": "owner/repo",
            "clone_url": "https://github.com/owner/repo.git",
        },
    }


def test_parse_failed_workflow_run():
    f = parse_github_payload("workflow_run", _workflow_run("failure"))
    assert f is not None
    assert f.run_id == "42"
    assert f.workflow == "CI"
    assert f.branch == "main"
    assert f.conclusion == "failure"
    assert f.project_full_name == "owner/repo"
    assert f.project_clone_url == "https://github.com/owner/repo.git"
    assert "CI 失败" in f.prompt and "#42" in f.prompt


def test_parse_successful_workflow_run_ignored():
    # success / skipped 不算失败 → None
    assert parse_github_payload("workflow_run", _workflow_run("success")) is None
    assert parse_github_payload("workflow_run", _workflow_run("skipped")) is None


def test_parse_cancelled_and_timed_out_are_failures():
    for c in ("cancelled", "timed_out", "action_required"):
        assert parse_github_payload("workflow_run", _workflow_run(c)) is not None


def test_parse_check_run_failure():
    payload = {
        "action": "completed",
        "check_run": {
            "id": 99,
            "name": "tests",
            "conclusion": "failure",
            "html_url": "https://github.com/owner/repo/runs/99",
            "check_suite": {"head_branch": "dev"},
        },
        "repository": {
            "full_name": "owner/repo",
            "clone_url": "https://github.com/owner/repo.git",
        },
    }
    f = parse_github_payload("check_run", payload)
    assert f is not None
    assert f.run_id == "99"
    assert f.workflow == "tests"
    assert f.branch == "dev"


def test_parse_unsupported_event_returns_none():
    assert parse_github_payload("ping", {}) is None
    assert parse_github_payload("push", {}) is None


# --------------------------------------------------------------------------- #
# 项目匹配
# --------------------------------------------------------------------------- #


def test_match_by_clone_url():
    projects = {
        "demo": Project(
            name="demo",
            path="/tmp/demo",
            repo="https://github.com/owner/repo",
        )
    }
    # 配置无 .git、回调带 .git，归一化后应命中
    m = match_project_by_repo(projects, "owner/repo", "https://github.com/owner/repo.git")
    assert m is not None and m.name == "demo"


def test_match_by_full_name_when_repo_url_differs():
    projects = {
        "demo": Project(
            name="demo",
            path="/tmp/demo",
            repo="https://github.com/owner/repo",
        )
    }
    # clone_url 完全不同，但 full_name 命中 github.com/owner/repo
    m = match_project_by_repo(projects, "owner/repo", "https://gh.example.com/x")
    assert m is not None and m.name == "demo"


def test_match_no_match_returns_none():
    projects = {
        "demo": Project(name="demo", path="/tmp/demo", repo="https://github.com/other/x")
    }
    assert match_project_by_repo(projects, "owner/repo", "https://github.com/owner/repo.git") is None


def test_match_skips_projects_without_repo():
    # 未配 repo 的项目不参与匹配（MVP 不在此探测 git remote）
    projects = {"demo": Project(name="demo", path="/tmp/demo")}
    assert match_project_by_repo(projects, "owner/repo", "https://github.com/owner/repo.git") is None


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #


def test_dedup_first_seen_processes_second_skipped():
    cache = DedupCache()
    assert cache.check_and_mark("run-1") is True  # 首次
    assert cache.check_and_mark("run-1") is False  # 重复 → 跳过
    assert cache.check_and_mark("run-2") is True  # 不同 run 仍处理


def test_dedup_ttl_expiry(tmp_path):
    cache = DedupCache(ttl=0.01)
    assert cache.check_and_mark("run-1") is True

    async def _wait():
        await asyncio.sleep(0.05)

    asyncio.run(_wait())
    # 过期后再次出现应当作新 run 处理
    assert cache.check_and_mark("run-1") is True


def test_dedup_empty_run_id_always_processes():
    cache = DedupCache()
    assert cache.check_and_mark("") is True
    assert cache.check_and_mark("") is True  # 空 id 不去重


# --------------------------------------------------------------------------- #
# HTTP 往返（照 test_control.py / test_viewer.py）
# --------------------------------------------------------------------------- #


def _post(url: str, body: bytes, signature: str | None, event: str = "workflow_run"):
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


async def _make_server(secret: str = "s3cr3t", routes=None) -> WebhookServer:
    loop = asyncio.get_running_loop()
    ws = WebhookServer(
        secret, routes or {}, host="127.0.0.1", port=0, main_loop=loop
    )
    ws.start()
    return ws


async def test_invalid_signature_rejected_401():
    seen: list = []

    async def handler(body: bytes, headers: dict):
        seen.append(body)
        return 200, {"ok": True}

    ws = await _make_server(routes={("POST", "/webhook/ci"): handler})
    try:
        body = b'{"x":1}'
        s_bad, _ = await asyncio.to_thread(_post, ws.base_url + "/webhook/ci", body, "sha256=deadbeef")
        assert s_bad == 401
        s_missing, _ = await asyncio.to_thread(_post, ws.base_url + "/webhook/ci", body, None)
        assert s_missing == 401
        assert seen == []  # handler 未被调用
    finally:
        ws.stop()


async def test_valid_signature_routes_to_handler():
    seen: list = []

    async def handler(body: bytes, headers: dict):
        seen.append((body, headers.get("x-github-event")))
        return 200, {"ok": True}

    ws = await _make_server(routes={("POST", "/webhook/ci"): handler})
    try:
        body = b'{"workflow_run":{"id":1}}'
        sig = _sig("s3cr3t", body)
        status, payload = await asyncio.to_thread(
            _post, ws.base_url + "/webhook/ci", body, sig
        )
        assert status == 200
        assert payload == {"ok": True}
        assert seen[0][1] == "workflow_run"
    finally:
        ws.stop()


async def test_unknown_route_404():
    ws = await _make_server(routes={})
    try:
        body = b"{}"
        status, _ = await asyncio.to_thread(
            _post, ws.base_url + "/webhook/missing", body, _sig("s3cr3t", body)
        )
        assert status == 404
    finally:
        ws.stop()


async def test_handler_exception_becomes_500():
    async def boom(body: bytes, headers: dict):
        raise RuntimeError("kaboom")

    ws = await _make_server(routes={("POST", "/webhook/ci"): boom})
    try:
        body = b"{}"
        status, payload = await asyncio.to_thread(
            _post, ws.base_url + "/webhook/ci", body, _sig("s3cr3t", body)
        )
        assert status == 500
        assert "kaboom" in payload.get("error", "")
    finally:
        ws.stop()


# --------------------------------------------------------------------------- #
# 端到端：失败 run → handler 被调；成功 run → handler 仍被调（dispatcher 不预过滤）
# 失败/成功的预过滤在 daemon._handle_ci_webhook 里做（见 test_daemon_webhook.py）。
# --------------------------------------------------------------------------- #


async def test_failed_run_delivers_parsed_failure_to_handler():
    """签名校验通过后，handler 收到原始 body；解析在 handler 内做（同 daemon 接线）。"""
    delivered: list = []

    async def handler(body: bytes, headers: dict):
        event = headers.get("x-github-event", "")
        failure = parse_github_payload(event, json.loads(body.decode()))
        delivered.append(failure)
        return 200, {"ok": True}

    ws = await _make_server(routes={("POST", "/webhook/ci"): handler})
    try:
        body = json.dumps(_workflow_run("failure")).encode()
        await asyncio.to_thread(_post, ws.base_url + "/webhook/ci", body, _sig("s3cr3t", body))
        assert len(delivered) == 1 and delivered[0] is not None
        assert delivered[0].run_id == "42"
    finally:
        ws.stop()
