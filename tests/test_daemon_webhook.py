"""daemon 侧 CI 失败 webhook 处理：``_handle_ci_webhook`` 的决策路径测试。

mock 掉 wake/spawn/notify（hermetic），验证：
- 失败 run + 匹配项目 + 有非终止 Task → 走 send_to_task（唤醒/排队）。
- 失败 run + 匹配项目 + 无 Task → 走 spawn_agent（新建）。
- 成功 run → ignored，不唤醒。
- 同一 run_id 二次回调 → 去重跳过。
- 未匹配项目 → ignored + 主线通知。
- 非允许事件 → ignored。
"""

from __future__ import annotations

import json
from pathlib import Path

from feishu_dispatcher.config import Config, Project, WebhookConfig
from feishu_dispatcher.store import TaskStore
from feishu_dispatcher.webhook import CIFailure

# 复用 test_daemon.py 的 fixture 风格；轻量构造 _Daemon（不经 run()，不起 HTTP）。
from feishu_dispatcher.daemon import _Daemon

_SECRET = "s3cr3t"
_BASE_HEADERS = {"x-github-event": "workflow_run"}


def _cfg(webhook: WebhookConfig | None = None) -> Config:
    return Config(
        app_id="a",
        app_secret="b",
        chat_id="oc_1",
        agents={"copilot": ["copilot", "--acp"]},
        projects={
            "demo": Project(
                name="demo",
                path=Path("/tmp/demo"),
                repo="https://github.com/owner/repo",
            )
        },
        webhook=webhook or WebhookConfig(port=9001, secret=_SECRET),
    )


def _daemon(webhook: WebhookConfig | None = None):
    d = _Daemon(_cfg(webhook), store=TaskStore(None))
    # 不经 run()：手动把调度入口 mock 成记录器，验证「唤醒 vs 新建」决策。
    d.sent: list[tuple[str, str]] = []  # (task_id, message) 或 ("spawn", project)
    d.notified: list[str] = []

    async def fake_send(task_id: str, message: str) -> str:
        d.sent.append((task_id, message))
        return f"sent:{task_id}"

    async def fake_spawn(project_name: str, task: str, agent: str = "", issue: int = 0, model: str = "") -> str:
        d.sent.append(("spawn", project_name))
        return f"spawned:{project_name}"

    async def fake_notify(text: str) -> None:
        d.notified.append(text)

    d._sched_send_to_task = fake_send  # type: ignore[method-assign]
    d._sched_spawn_agent = fake_spawn  # type: ignore[method-assign]
    d._notify_main = fake_notify  # type: ignore[method-assign]
    return d


def _failure(run_id: str = "42", full_name: str = "owner/repo") -> CIFailure:
    return CIFailure(
        run_id=run_id,
        project_full_name=full_name,
        project_clone_url=f"https://github.com/{full_name}.git",
        workflow="CI",
        branch="main",
        conclusion="failure",
        html_url=f"https://github.com/{full_name}/actions/runs/{run_id}",
        failure_summary="failure: CI",
    )


def _payload(conclusion: str = "failure", run_id: int = 42) -> bytes:
    return json.dumps(
        {
            "action": "completed",
            "workflow": {"name": "CI"},
            "workflow_run": {
                "id": run_id,
                "name": "CI",
                "conclusion": conclusion,
                "head_branch": "main",
                "html_url": "https://github.com/owner/repo/actions/runs/%d" % run_id,
            },
            "repository": {
                "full_name": "owner/repo",
                "clone_url": "https://github.com/owner/repo.git",
            },
        }
    ).encode()


async def test_failed_run_no_task_spawns_new_agent():
    d = _daemon()
    status, payload = await d._handle_ci_webhook(_payload("failure"), _BASE_HEADERS)
    assert status == 200 and payload["ok"] is True
    assert payload["project"] == "demo"
    assert d.sent == [("spawn", "demo")]
    assert any("demo CI 失败" in n for n in d.notified)


async def test_failed_run_with_active_task_wakes_it():
    d = _daemon()
    # 预置一个非终止 Task（project=demo）
    t = d.store.create(
        project_name="demo",
        agent_label="copilot",
        description="seed",
        thread_root_id="om_seed",
        workspace="/tmp/demo",
    )
    status, payload = await d._handle_ci_webhook(_payload("failure"), _BASE_HEADERS)
    assert status == 200 and payload["ok"] is True
    # 走唤醒（send_to_task），不新建
    assert d.sent == [(t.task_id, _failure().prompt)]
    assert ("spawn", "demo") not in d.sent


async def test_successful_run_ignored():
    d = _daemon()
    status, payload = await d._handle_ci_webhook(_payload("success"), _BASE_HEADERS)
    assert status == 200
    assert payload == {"ignored": "not a CI failure (success/skipped/unsupported)"}
    assert d.sent == [] and d.notified == []


async def test_duplicate_run_id_deduped():
    d = _daemon()
    await d._handle_ci_webhook(_payload("failure", run_id=77), _BASE_HEADERS)
    await d._handle_ci_webhook(_payload("failure", run_id=77), _BASE_HEADERS)
    # 同一 run 只唤醒一次
    assert len(d.sent) == 1


async def test_no_matching_project_notifies_and_ignores():
    d = _daemon()
    payload = json.loads(_payload("failure"))
    payload["repository"] = {
        "full_name": "other/project",
        "clone_url": "https://github.com/other/project.git",
    }
    status, resp = await d._handle_ci_webhook(
        json.dumps(payload).encode(), _BASE_HEADERS
    )
    assert status == 200
    assert resp == {"ignored": "no matching project"}
    assert any("未知项目" in n for n in d.notified)
    assert d.sent == []


async def test_disallowed_event_ignored():
    d = _daemon()
    status, payload = await d._handle_ci_webhook(
        b"{}", {**_BASE_HEADERS, "x-github-event": "push"}
    )
    assert status == 200
    assert payload["ignored"].startswith("event push not in allowed_events")
    assert d.sent == []


async def test_invalid_json_returns_400():
    d = _daemon()
    status, payload = await d._handle_ci_webhook(b"not-json{", _BASE_HEADERS)
    assert status == 400 and "error" in payload
