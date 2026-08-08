"""CI 失败 webhook 回调（#54）：daemon 内嵌的轻量 HTTP server，收 GitHub Actions
（MVP）的失败回调 → 匹配项目 → 唤醒/新建 agent 修复。

与 ``control.py`` / ``viewer.py`` 是同一个模子（daemon 进程内
``ThreadingHTTPServer`` + daemon 线程），区别在于鉴权口径：

- ``control.py``：Bearer token（task 一一映射），绑 127.0.0.1，给 agent 子进程。
- ``viewer.py``：单一全局 Bearer token，绑可达地址，只读，给手机。
- 本模块：**HMAC 签名**（GitHub 风格 ``X-Hub-Signature-256``），绑 127.0.0.1（建议经
  反向代理暴露），只收 ``POST /webhook/ci``。

HTTP handler 把「匹配项目 + 唤醒/新建」的决策 marshal 回主 event loop 执行——所有
daemon 状态（stores / sessions）都只在主 loop 单线程访问，避免竞态（与另两个 server
一致）。唤醒/新建复用 daemon 既有的 ``send_to_task``（活跃→排队，挂起→load_session
恢复后入队）与 ``spawn_agent``（无活跃 Task→新建）。

MVP 边界（见 issue #54 与 PR 说明）：仅 GitHub Actions；仅 failure/cancelled/error；
项目按 ``repository.full_name`` / ``clone_url`` 匹配已配 ``repo`` 的项目；同一
``run_id`` 1h 内只触发一次（内存 TTL 缓存）。GitLab/Gitea 解析、完整通知矩阵、反向
代理文档显式延后。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from feishu_dispatcher.config import Project

logger = logging.getLogger(__name__)

#: 路由处理器：async (body: bytes, headers: dict[str,str]) -> (http_status, dict)。
#: 在主 loop 上执行（经 dispatch marshal 回来）。
RouteHandler = Callable[[bytes, dict[str, str]], Awaitable[tuple[int, dict]]]

#: 请求体上限（GitHub webhook payload 通常 < 10KB，给足余量防异常大 body）。
_MAX_BODY = 5_000_000
#: 单个请求在主 loop 上处理的最长等待（匹配 + 唤醒都该很快；spawn 可能起进程，放宽些）
_DISPATCH_TIMEOUT = 60.0
#: 签名首部前缀（GitHub 风格：``sha256=<hex>``）
_SIG_PREFIX = "sha256="
#: 同一 CI run 的去重窗口（issue #54：TTL ~1h）
_DEDUP_TTL = 3600.0


@dataclass(frozen=True)
class CIFailure:
    """从 GitHub payload 解析出的「一次 CI 失败」抽象。

    平台无关——GitHub 解析器产出它，匹配/唤醒逻辑只认它。后续加 GitLab/Gitea 解析器
    时产出同一结构即可（延后，见模块 docstring）。

    ``run_id`` 用作去重键：同一 run 即使回调多次（重推/多事件）也只唤醒一次。
    """

    run_id: str
    project_full_name: str
    project_clone_url: str
    workflow: str
    branch: str
    conclusion: str
    html_url: str
    failure_summary: str

    @property
    def prompt(self) -> str:
        """派给 agent 的修复任务首条 prompt。"""
        return (
            f"CI 失败：{self.workflow} #{self.run_id}（{self.branch}）— {self.failure_summary}\n"
            f"详情：{self.html_url}\n请定位并修复 CI 失败。"
        )


class DedupCache:
    """``run_id`` → 见过时间戳 的内存去重表（TTL 清理）。

    同一 CI run 即使被回调多次（GitHub 重推、workflow_run + check_run 双触发）也只应
    唤醒一次。线程安全：HTTP handler 线程 ``check``、主 loop 上 ``mark`` 都会访问，
    用一把锁兜住（操作极快，锁竞争可忽略）。
    """

    def __init__(self, ttl: float = _DEDUP_TTL) -> None:
        self._ttl = ttl
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check_and_mark(self, run_id: str) -> bool:
        """``run_id`` 首次见 → 记录并返回 True（应当处理）；已见过 → False（跳过）。

        顺带清理过期项（懒清理，无需独立回收线程）。进程重启缓存丢失——可接受
        （最坏重启窗口内重触发一次，幂等的唤醒路径能兜住：活跃 session 只是再排队）。
        """
        if not run_id:
            return True
        now = time.monotonic()
        with self._lock:
            for k in list(self._seen):
                if now - self._seen[k] > self._ttl:
                    del self._seen[k]
            if run_id in self._seen:
                return False
            self._seen[run_id] = now
            return True


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """GitHub 风格 HMAC-SHA256 签名校验（``X-Hub-Signature-256: sha256=<hex>``）。

    空密钥直接拒绝（避免误把无鉴权端点开到公网）。用 ``hmac.compare_digest`` 防时序
    攻击。签名缺失/格式错 → False。
    """
    if not secret or not signature_header:
        return False
    if not signature_header.startswith(_SIG_PREFIX):
        return False
    provided = signature_header[len(_SIG_PREFIX) :].strip()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def parse_github_payload(
    event: str, payload: dict
) -> "CIFailure | None":
    """把 GitHub webhook payload 解析成 :class:`CIFailure`；非失败/非受支持 → None。

    支持 ``workflow_run`` 与 ``check_run``（``allowed_events`` 默认这两类）。只有
    conclusion 为 ``failure`` / ``cancelled`` / ``timed_out`` / ``action_required``
    才算失败（GitHub Actions 的失败口径；``success``/``skipped`` 忽略）。
    """
    if event == "workflow_run":
        run = payload.get("workflow_run") or {}
        repo = payload.get("repository") or {}
        conclusion = str(run.get("conclusion") or "")
        if conclusion not in ("failure", "cancelled", "timed_out", "action_required"):
            return None
        wf = payload.get("workflow") or {}
        branch = str(run.get("head_branch") or repo.get("default_branch") or "")
        return CIFailure(
            run_id=str(run.get("id") or ""),
            project_full_name=str(repo.get("full_name") or ""),
            project_clone_url=str(repo.get("clone_url") or ""),
            workflow=str(wf.get("name") or run.get("name") or "workflow"),
            branch=branch,
            conclusion=conclusion,
            html_url=str(run.get("html_url") or ""),
            failure_summary=f"{conclusion}: {wf.get('name') or run.get('name') or 'workflow'}",
        )
    if event == "check_run":
        check = payload.get("check_run") or {}
        repo = payload.get("repository") or {}
        conclusion = str(check.get("conclusion") or "")
        # check_run 的 conclusion 更细：只把明确的失败口径当失败
        if conclusion not in ("failure", "cancelled", "timed_out", "action_required"):
            return None
        name = str(check.get("name") or "check")
        branch = str((check.get("check_suite") or {}).get("head_branch") or "")
        return CIFailure(
            run_id=str(check.get("id") or ""),
            project_full_name=str(repo.get("full_name") or ""),
            project_clone_url=str(repo.get("clone_url") or ""),
            workflow=name,
            branch=branch,
            conclusion=conclusion,
            html_url=str(check.get("html_url") or ""),
            failure_summary=f"{conclusion}: {name}",
        )
    return None


def _normalize_repo_url(url: str) -> str:
    """规范化仓库 URL 用于比较：去 ``https://`` / ``http://``、去末尾 ``.git``、小写。

    GitHub 回调里 ``clone_url`` 形如 ``https://github.com/owner/repo.git``，而项目配置
    的 ``repo`` 可能写成 ``https://github.com/owner/repo``（无 .git）。两者都归一后比较。
    """
    s = (url or "").strip().lower()
    for scheme in ("https://", "http://", "git@"):
        if s.startswith(scheme):
            s = s[len(scheme) :]
    # git@github.com:owner/repo → github.com/owner/repo
    s = s.replace(":", "/", 1) if s.count(":") == 1 and "/" not in s.split(":")[0] else s
    if s.endswith(".git"):
        s = s[: -len(".git")]
    return s.rstrip("/")


def match_project_by_repo(
    projects: dict[str, Project], full_name: str, clone_url: str
) -> "Project | None":
    """按 ``full_name`` / ``clone_url`` 匹配已配 ``repo`` 的项目。

    匹配口径（任一命中即返回）：
    - 项目的 ``repo`` 归一化后等于 payload ``clone_url`` 归一化；
    - 项目的 ``repo`` 归一化后以 ``github.com/<full_name>`` 结尾（full_name 形如
      ``owner/repo``）。

    项目未配 ``repo``（空串）则跳过——MVP 不在此处探测 git remote（避免 webhook 热路径
    调子进程 + 破坏单测 hermeticity）；这类项目需在配置里补 ``repo`` 才能被 CI 回调命中。
    匹配到多个 → 返回第一个（issue 提到「按 path 精确匹配」，但 webhook payload 不带
    本地 path，故仅按 repo 唯一匹配；多命中属配置歧义，记 warning）。
    """
    norm_clone = _normalize_repo_url(clone_url)
    full_name_lc = (full_name or "").strip().lower()
    matches = []
    for p in projects.values():
        if not p.repo:
            continue
        norm_repo = _normalize_repo_url(p.repo)
        if norm_clone and norm_repo == norm_clone:
            matches.append(p)
            continue
        if full_name_lc and norm_repo.endswith("github.com/" + full_name_lc):
            matches.append(p)
            continue
    if len(matches) > 1:
        logger.warning(
            "CI 回调匹配到多个项目（%s），取第一个；请检查 repo 配置歧义",
            ", ".join(m.name for m in matches),
        )
    return matches[0] if matches else None


class WebhookServer:
    """CI 失败 webhook HTTP 服务。``routes`` 是 ``(METHOD, path) -> handler`` 表。

    单一路由 ``POST /webhook/ci``；HMAC 签名校验通过后才 marshal 到主 loop 执行 handler。
    handler 在主 loop 上做「匹配项目 + 唤醒/新建 agent」（复用 daemon 既有逻辑）。
    """

    def __init__(
        self,
        secret: str,
        routes: dict[tuple[str, str], RouteHandler],
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        main_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._secret = secret
        self._routes = routes
        self._loop = main_loop
        self._server = ThreadingHTTPServer((host, port), _make_handler(self))
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="webhook-http", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        logger.info("CI 失败 webhook 已启动: %s", self.base_url)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        """停 HTTP server（阻塞调用）。两步拆成独立 try：与 control.py / viewer.py 一致。"""
        try:
            self._server.shutdown()
        except Exception:
            logger.debug("webhook shutdown 异常（忽略）", exc_info=True)
        try:
            self._server.server_close()
        except Exception:
            logger.debug("webhook server_close 异常（忽略）", exc_info=True)

    def dispatch(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[int, dict]:
        """在 handler 线程里调用：签名校验 → 找路由 → marshal 到主 loop 执行。"""
        if not verify_signature(self._secret, body, headers.get("x-hub-signature-256", "")):
            return 401, {"error": "invalid or missing signature"}
        handler = self._routes.get((method, path))
        if handler is None:
            return 404, {"error": f"no route for {method} {path}"}
        try:
            coro = handler(body, headers)
            if self._loop is not None:
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
                return fut.result(timeout=_DISPATCH_TIMEOUT)
            return asyncio.new_event_loop().run_until_complete(coro)
        except Exception as exc:
            logger.exception("webhook 处理失败 %s %s", method, path)
            return 500, {"error": f"{type(exc).__name__}: {exc}"}


def _make_handler(ws: WebhookServer):
    class _Handler(BaseHTTPRequestHandler):
        # 静音默认往 stderr 打的访问日志（与 control.py / viewer.py 一致）
        def log_message(self, *args) -> None:  # noqa: D401
            return None

        def _headers_dict(self) -> dict[str, str]:
            # BaseHTTPRequestHandler 的 headers 是 email.message.Message，大小写不敏感；
            # 转成普通 dict 时 key 统一小写，方便 handler 取 X-Hub-* 头。
            return {k.lower(): v for k, v in self.headers.items()}

        def _read_body(self) -> "bytes | None":
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return b""
            if length > _MAX_BODY:
                return None
            return self.rfile.read(length)

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
                self._respond(413, {"error": "payload too large"})
                return
            status, payload = ws.dispatch("POST", self._path(), body, self._headers_dict())
            logger.info("webhook POST %s → %d", self._path(), status)
            self._respond(status, payload)

    return _Handler
