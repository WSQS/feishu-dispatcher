"""interactive card 构造器（纯函数，易测）。"""

from __future__ import annotations

_STATUS_MAP = {
    "running": ("blue", "🔄"),
    "done": ("green", "✅"),
    "error": ("red", "❌"),
    "stopped": ("grey", "🛑"),
}


def build_card(title: str, status: str, body: str, footer: str = "") -> dict:
    """构造 interactive card dict。status ∈ {running, done, error, stopped}。

    body 用飞书 ``markdown`` 组件渲染（非 ``lark_md`` 那个受限子集）——支持代码块、
    标题、表格、列表等，coding agent 的输出（大量代码块）才能正常显示。
    """
    color, emoji = _STATUS_MAP.get(status, ("blue", "🔄"))

    elements: list[dict] = [
        {"tag": "markdown", "content": body or "…"},
    ]
    if footer:
        elements.append(
            {"tag": "note", "elements": [{"tag": "lark_md", "content": footer}]}
        )

    return {
        "config": {"update_multi": True, "wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
            "template": color,
        },
        "elements": elements,
    }
