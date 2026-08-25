"""HTTP Channel WebUI 的真实 Chromium 闭环测试。"""

from __future__ import annotations

import asyncio

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from feishu_dispatcher.channel import ChannelMessage
from feishu_dispatcher.http_channel import HttpChannel

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
