"""build_card 单元测试。"""

from feishu_dispatcher.card import build_card


def test_build_card_running():
    card = build_card("test", "running", "hello")
    assert card["header"]["template"] == "blue"
    assert "🔄" in card["header"]["title"]["content"]
    assert card["elements"][0]["text"]["content"] == "hello"


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
    assert card["elements"][0]["text"]["content"] == "…"


def test_build_card_with_footer():
    card = build_card("test", "running", "body", footer="footer text")
    assert len(card["elements"]) == 2
    assert card["elements"][1]["tag"] == "note"
    assert card["elements"][1]["elements"][0]["content"] == "footer text"


def test_build_card_without_footer():
    card = build_card("test", "running", "body")
    assert len(card["elements"]) == 1


def test_build_card_config():
    card = build_card("test", "running", "body")
    assert card["config"]["update_multi"] is True
    assert card["config"]["wide_screen_mode"] is True
