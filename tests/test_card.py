"""build_card 单元测试（卡片 JSON 2.0）。"""

from feishu_dispatcher.card import build_card


def _body(card):
    return card["body"]["elements"]


def test_build_card_is_v2_schema():
    card = build_card("test", "running", "hello")
    assert card["schema"] == "2.0"
    assert "elements" in card["body"]  # 2.0：元素在 body 下


def test_build_card_running():
    card = build_card("test", "running", "hello")
    assert card["header"]["template"] == "blue"
    assert "🔄" in card["header"]["title"]["content"]
    # body 走 markdown 组件（2.0 支持标题/表格/代码块），content 直接挂在元素上
    assert _body(card)[0]["tag"] == "markdown"
    assert _body(card)[0]["content"] == "hello"


def test_build_card_done():
    card = build_card("test", "done", "done body")
    assert card["header"]["template"] == "green"
    assert "✅" in card["header"]["title"]["content"]


def test_build_card_error():
    card = build_card("test", "error", "error body")
    assert card["header"]["template"] == "red"
    assert "❌" in card["header"]["title"]["content"]


def test_build_card_stopped():
    card = build_card("test", "stopped", "stopped body")
    assert card["header"]["template"] == "grey"
    assert "🛑" in card["header"]["title"]["content"]


def test_build_card_unknown_status_defaults_to_blue():
    card = build_card("test", "unknown", "body")
    assert card["header"]["template"] == "blue"
    assert "🔄" in card["header"]["title"]["content"]


def test_build_card_empty_body_placeholder():
    card = build_card("test", "running", "")
    assert _body(card)[0]["content"] == "…"


def test_build_card_with_footer():
    card = build_card("test", "running", "body", footer="footer text")
    assert len(_body(card)) == 2
    # footer 用小字号 markdown（notation），不再是 note 组件
    assert _body(card)[1]["tag"] == "markdown"
    assert _body(card)[1]["text_size"] == "notation"
    assert _body(card)[1]["content"] == "footer text"


def test_build_card_without_footer():
    card = build_card("test", "running", "body")
    assert len(_body(card)) == 1


def test_build_card_config():
    card = build_card("test", "running", "body")
    assert card["config"]["update_multi"] is True
    assert card["config"]["wide_screen_mode"] is True
