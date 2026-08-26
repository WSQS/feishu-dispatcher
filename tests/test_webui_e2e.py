"""HTTP Channel WebUI 的真实 Chromium 闭环测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from feishu_dispatcher.channel import ChannelMessage
from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.http_channel import HttpChannel
from feishu_dispatcher.session_event import (
    SessionErrorOccurred,
    SessionEvent,
    SessionInputAccepted,
    session_event_to_dict,
)

pytestmark = pytest.mark.webui_e2e


async def _wait_for_status(page, text: str) -> None:
    await page.wait_for_function(
        """expected => document.querySelector("#status")
            ?.textContent.includes(expected)""",
        arg=text,
    )


async def _launch_browser(playwright):
    try:
        return await playwright.chromium.launch(headless=True)
    except PlaywrightError as chromium_error:
        try:
            return await playwright.chromium.launch(channel="msedge", headless=True)
        except PlaywrightError as edge_error:
            if "Executable doesn't exist" not in str(chromium_error):
                raise chromium_error
            if "Executable doesn't exist" not in str(edge_error):
                raise edge_error
            pytest.skip(
                "缺少 Chromium 浏览器；运行 `uv run playwright install chromium` 后重试"
            )


def _trace_record(
    sequence: int,
    session_id: str,
    text: str,
    *,
    error: bool = False,
) -> dict:
    event = SessionEvent(
        event_id=f"event-{session_id}-{sequence}",
        session_id=session_id,
        turn_id=None if error else f"turn-{sequence}",
        occurred_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        body=(
            SessionErrorOccurred(phase="turn", message=text)
            if error
            else SessionInputAccepted(
                text=text,
                source=ConversationRef("feishu", f"thread-{session_id}"),
            )
        ),
    )
    return {"sequence": sequence, "event": session_event_to_dict(event)}


@pytest.mark.asyncio
async def test_webui_browser_help_refresh_cursor_and_token_storage():
    loop = asyncio.get_running_loop()
    token = "tok-browser-e2e"

    async def list_tasks(_context: dict, _request: dict) -> tuple[int, dict]:
        return 200, {
            "tasks": [
                {
                    "task_id": "dispatcher",
                    "kind": "dispatcher",
                    "description": "Dispatcher",
                    "status": "active",
                    "active": True,
                }
            ]
        }

    async def list_projects(_context: dict, _request: dict) -> tuple[int, dict]:
        return 200, {"items": []}

    channel = HttpChannel(
        token,
        loop,
        host="127.0.0.1",
        port=0,
        routes={
            ("GET", "/api/tasks"): list_tasks,
            ("GET", "/api/projects"): list_projects,
        },
    )

    async def handle(message: ChannelMessage) -> None:
        if message.text == "/help":
            channel.reply_text(message.message_id, "browser help reply")

    channel.start(handle)
    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(playwright)
            try:
                page = await browser.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                await page.goto(channel.base_url)
                await page.wait_for_function(
                    """() => document.querySelector("#conversation-id")
                        ?.textContent.trim().length > 0"""
                )
                conversation_id = (
                    await page.locator("#conversation-id").text_content()
                ).strip()
                assert conversation_id.startswith("webui-conversation-")

                await page.locator("#connection-settings > summary").click()
                await page.locator("#token").fill("wrong-token")
                await page.locator("#connect").click()
                await _wait_for_status(page, "invalid_token")

                await page.locator("#token").fill(token)
                await page.locator("#connect").click()
                await _wait_for_status(page, "已连接")
                await page.locator("#message").fill("/help")
                await page.locator("#send").click()
                await page.get_by_text("browser help reply", exact=True).wait_for()

                cursor = int((await page.locator("#cursor").text_content()).strip())
                assert cursor > 0
                assert (
                    await page.evaluate(
                        """secret => [localStorage, sessionStorage].some(
                            storage => Array.from(
                                { length: storage.length },
                                (_, index) => storage.getItem(storage.key(index)),
                            ).includes(secret),
                        )""",
                        token,
                    )
                    is False
                )

                await page.reload()
                await page.wait_for_function(
                    """() => document.querySelector("#conversation-id")
                        ?.textContent.trim().length > 0"""
                )
                assert (
                    await page.locator("#conversation-id").text_content()
                ).strip() == conversation_id
                assert (
                    int((await page.locator("#cursor").text_content()).strip())
                    == cursor
                )
                assert await page.locator("#token").input_value() == ""

                await page.locator("#connection-settings > summary").click()
                await page.locator("#token").fill(token)
                await page.locator("#connect").click()
                await _wait_for_status(page, "已连接")
                await page.wait_for_timeout(900)
                assert (
                    await page.get_by_text("browser help reply", exact=True).count()
                    == 0
                )
                assert page_errors == []
            finally:
                await browser.close()
    finally:
        await asyncio.to_thread(channel.stop)


@pytest.mark.asyncio
async def test_webui_browser_task_history_pagination_dedup_and_switch_isolation():
    loop = asyncio.get_running_loop()
    token = "tok-browser-history-e2e"
    history_requests: list[tuple[str, str | None]] = []
    slow_history_started = asyncio.Event()
    release_slow_history = asyncio.Event()
    slow_history_finished = asyncio.Event()
    task_a_recent_requests = 0
    message_target: asyncio.Future[str] = loop.create_future()

    task_a_records = [
        _trace_record(
            sequence,
            "task-a",
            "duplicate-history-event"
            if sequence == 105
            else f"task-a-history-{sequence}",
            error=sequence == 105,
        )
        for sequence in range(1, 106)
    ]
    task_b_records = [_trace_record(1, "task-b", "task-b-history-1")]
    late_task_a_record = _trace_record(106, "task-a", "late-task-a-event")

    async def list_tasks(_context: dict, _request: dict) -> tuple[int, dict]:
        return 200, {
            "tasks": [
                {
                    "task_id": "dispatcher",
                    "kind": "dispatcher",
                    "description": "Dispatcher",
                    "status": "active",
                    "active": True,
                },
                {
                    "task_id": "task-a",
                    "project": "project-a",
                    "agent": "codex",
                    "description": "History task A",
                    "status": "done",
                    "turns": 2,
                    "issue_url": None,
                    "kind": "agent",
                    "active": False,
                },
                {
                    "task_id": "task-b",
                    "project": "project-b",
                    "agent": "codex",
                    "description": "History task B",
                    "status": "done",
                    "turns": 1,
                    "issue_url": None,
                    "kind": "agent",
                    "active": False,
                },
            ]
        }

    async def list_projects(_context: dict, _request: dict) -> tuple[int, dict]:
        return 200, {"items": []}

    async def list_task_events(_context: dict, request: dict) -> tuple[int, dict]:
        nonlocal task_a_recent_requests
        task_id = request["segments"]["task_id"]
        before = request["query"].get("before")
        history_requests.append((task_id, before))
        if task_id == "task-a" and before is None:
            task_a_recent_requests += 1
            if task_a_recent_requests == 2:
                slow_history_started.set()
                await release_slow_history.wait()
                slow_history_finished.set()
                return 200, {
                    "task_id": task_id,
                    "events": [late_task_a_record],
                    "oldest_sequence": 1,
                    "latest_sequence": 106,
                }
            records = task_a_records[5:]
            return 200, {
                "task_id": task_id,
                "events": records,
                "oldest_sequence": 1,
                "latest_sequence": 105,
            }
        if task_id == "task-a" and before == "6":
            return 200, {
                "task_id": task_id,
                "events": task_a_records[:5],
                "oldest_sequence": 1,
                "latest_sequence": 105,
            }
        if task_id == "task-b" and before is None:
            return 200, {
                "task_id": task_id,
                "events": task_b_records,
                "oldest_sequence": 1,
                "latest_sequence": 1,
            }
        return 400, {"error": "unexpected_history_request"}

    channel = HttpChannel(
        token,
        loop,
        host="127.0.0.1",
        port=0,
        routes={
            ("GET", "/api/tasks"): list_tasks,
            ("GET", "/api/tasks/{task_id}/events"): list_task_events,
            ("GET", "/api/projects"): list_projects,
        },
    )

    async def handle(message: ChannelMessage) -> None:
        if not message_target.done():
            message_target.set_result(message.message_id)

    channel.start(handle)
    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(playwright)
            try:
                page = await browser.new_page()
                page_errors: list[str] = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                await page.goto(channel.base_url)
                await page.locator("#connection-settings > summary").click()
                await page.locator("#token").fill(token)
                await page.locator("#connect").click()
                await _wait_for_status(page, "已连接")

                await page.locator("#message").fill("start history polling")
                await page.locator("#send").click()
                target_id = await asyncio.wait_for(message_target, timeout=3)

                task_a_button = page.locator(
                    ".task-item", has_text="task-a · project-a"
                )
                await task_a_button.click()
                await _wait_for_status(page, "已打开 task-a · project-a 的历史")
                task_a_timeline = page.locator('.task-timeline[data-task-id="task-a"]')
                await task_a_timeline.get_by_text(
                    "task-a-history-6", exact=True
                ).wait_for()
                assert await task_a_timeline.locator(".event").count() == 100

                await task_a_timeline.evaluate(
                    "(timeline) => { timeline.scrollTop = 0; }"
                )
                await task_a_timeline.get_by_text(
                    "task-a-history-1", exact=True
                ).wait_for()
                assert await task_a_timeline.locator(".event").count() == 105
                assert ("task-a", "6") in history_requests

                conversation_id = (
                    await page.locator("#conversation-id").text_content()
                ).strip()
                duplicate_cursor = channel._append_event(
                    conversation_id,
                    "session.event",
                    event=task_a_records[-1]["event"],
                    trace_sequence=105,
                    target_id=target_id,
                )
                await page.wait_for_function(
                    """expected => Number(
                        document.querySelector("#cursor")?.textContent,
                    ) >= expected""",
                    arg=duplicate_cursor,
                )
                assert (
                    await task_a_timeline.get_by_text(
                        "duplicate-history-event", exact=True
                    ).count()
                    == 1
                )
                assert (
                    await task_a_timeline.get_by_text(
                        "session.error.occurred", exact=True
                    ).count()
                    == 0
                )

                await task_a_button.click()
                await asyncio.wait_for(slow_history_started.wait(), timeout=3)
                task_b_button = page.locator(
                    ".task-item", has_text="task-b · project-b"
                )
                await task_b_button.click()
                await _wait_for_status(page, "已打开 task-b · project-b 的历史")
                task_b_timeline = page.locator('.task-timeline[data-task-id="task-b"]')
                await task_b_timeline.get_by_text(
                    "task-b-history-1", exact=True
                ).wait_for()
                assert await page.locator("#message").is_disabled()
                assert await page.locator("#send").is_disabled()

                release_slow_history.set()
                await asyncio.wait_for(slow_history_finished.wait(), timeout=3)
                await page.wait_for_timeout(200)
                assert (
                    await page.locator("#current-task").text_content()
                ).strip() == "task-b · project-b"
                assert (
                    await page.get_by_text("late-task-a-event", exact=True).count() == 0
                )
                assert page_errors == []
            finally:
                release_slow_history.set()
                await browser.close()
    finally:
        await asyncio.to_thread(channel.stop)
