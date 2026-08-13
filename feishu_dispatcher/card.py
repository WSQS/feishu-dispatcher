"""interactive card 构造器（纯函数，易测）。"""

from __future__ import annotations

_STATUS_MAP = {
    "running": ("blue", "🔄"),
    "done": ("green", "✅"),
    "error": ("red", "❌"),
    "stopped": ("grey", "🛑"),
}


def build_card(title: str, status: str, body: str, footer: str = "") -> dict:
    """构造 interactive card dict（**卡片 JSON 2.0**）。status ∈ {running, done, error, stopped}。

    用卡片 2.0（``schema: "2.0"`` + ``body.elements``）的 ``markdown`` 组件渲染 body——
    2.0 富文本组件支持除 HTMLBlock 外的**全部标准 markdown**（标题、表格、代码块、列表、
    引用），旧版结构只支持代码块 + 基础格式。footer 用小字号 markdown（``text_size:
    notation``）而非 note 组件，规避 note 在 2.0 的 schema 不确定性。
    """
    color, emoji = _STATUS_MAP.get(status, ("blue", "🔄"))

    elements: list[dict] = [
        {"tag": "markdown", "content": body or "…"},
    ]
    if footer:
        elements.append({"tag": "markdown", "content": footer, "text_size": "notation"})

    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
            "template": color,
        },
        "body": {"elements": elements},
    }


def build_markdown_card(text: str) -> dict:
    """构造一张**无标题**的 interactive card，body 为单个 markdown 组件。

    用于调度器主线回复：card 2.0 的 markdown 组件支持表格/列表/代码块等全部标准
    markdown（见 :func:`build_card` 注释），而 ``msg_type=text`` / ``post`` 都不
    支持表格。不带 header，避免给每条对话回复都加一个彩色标题栏。
    """
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "wide_screen_mode": True},
        "body": {"elements": [{"tag": "markdown", "content": text or "…"}]},
    }
