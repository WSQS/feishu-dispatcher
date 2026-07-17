"""LiveCard 单元测试（用 FakeBridge 记录 reply_card/patch_card 调用）。"""

import asyncio

from feishu_dispatcher.livecard import LiveCard


class FakeBridge:
    def __init__(self, reply_card_errors: int = 0, patch_card_errors: int = 0) -> None:
        self.card_replies: list[tuple[str, dict]] = []
        self.card_patches: list[tuple[str, dict]] = []
        self._reply_card_errors = reply_card_errors
        self._patch_card_errors = patch_card_errors

    def reply_card(self, root_message_id: str, card: dict) -> str:
        if self._reply_card_errors > 0:
            self._reply_card_errors -= 1
            raise RuntimeError("reply_card boom")
        self.card_replies.append((root_message_id, card))
        return f"om_card_{len(self.card_replies)}"

    def patch_card(self, message_id: str, card: dict) -> None:
        if self._patch_card_errors > 0:
            self._patch_card_errors -= 1
            raise RuntimeError("patch_card boom")
        self.card_patches.append((message_id, card))


async def test_feed_then_flush_reply_card():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("hello")
    await lc.flush()
    assert len(bridge.card_replies) == 1
    assert bridge.card_replies[0][0] == "om_root1"
    assert bridge.card_replies[0][1]["header"]["template"] == "blue"
    assert "🔄" in bridge.card_replies[0][1]["header"]["title"]["content"]
    await lc.aclose()


async def test_feed_then_flush_then_feed_again_patches():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("hello")
    await lc.flush()
    assert len(bridge.card_replies) == 1
    assert len(bridge.card_patches) == 0

    lc.feed(" world")
    await lc.flush()
    assert len(bridge.card_replies) == 1  # still one reply
    assert len(bridge.card_patches) == 1  # one patch
    assert bridge.card_patches[0][0] == "om_card_1"
    await lc.aclose()


async def test_set_status_done():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("done")
    await lc.flush()
    await lc.set_status("done")

    # last card emitted should have green/done status
    all_cards = bridge.card_replies + bridge.card_patches
    last_card = all_cards[-1][1]
    assert last_card["header"]["template"] == "green"
    assert "✅" in last_card["header"]["title"]["content"]
    await lc.aclose()


async def test_set_status_error():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("error")
    await lc.set_status("error")

    all_cards = bridge.card_replies + bridge.card_patches
    last_card = all_cards[-1][1]
    assert last_card["header"]["template"] == "red"
    assert "❌" in last_card["header"]["title"]["content"]
    await lc.aclose()


async def test_roll_on_max_body_bytes():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    # Feed enough to exceed _MAX_BODY_BYTES (25000)
    big_text = "x" * 25000
    lc.feed(big_text)
    await lc.flush()
    assert len(bridge.card_replies) == 1

    small_text = "y"
    # This should trigger roll: old card patched with footer, new card started
    lc.feed(small_text)
    await lc.flush()
    # Should have a second reply (new card) and possibly a patch for the footer
    assert len(bridge.card_replies) >= 2
    # Second card should have seq > 1 title
    assert bridge.card_replies[1][1]["header"]["title"]["content"] == "🔄 test (2)"
    await lc.aclose()


async def test_emit_bridge_error_does_not_bubble():
    bridge = FakeBridge(reply_card_errors=1)
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("hello")
    # Should not raise
    await lc.flush()
    # First reply failed, card_msg_id is still None
    # Try again - should succeed
    lc.feed(" world")
    await lc.flush()
    assert len(bridge.card_replies) == 1  # second attempt succeeded
    await lc.aclose()


async def test_feed_after_close_is_ignored():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("x")
    await lc.aclose()
    lc.feed("y")
    # Only "x" should have been emitted
    assert len(bridge.card_replies) == 1
    assert bridge.card_replies[0][1]["elements"][0]["text"]["content"] == "x"


async def test_empty_feed_ignored():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("")
    await asyncio.sleep(0.15)
    assert len(bridge.card_replies) == 0
    await lc.aclose()


async def test_debounce_merges_chunks():
    bridge = FakeBridge()
    lc = LiveCard(bridge, "om_root1", "test", window=0.05)
    lc.feed("a")
    lc.feed("b")
    lc.feed("c")
    await asyncio.sleep(0.2)
    # Should have 1 reply with merged body
    assert len(bridge.card_replies) == 1
    assert bridge.card_replies[0][1]["elements"][0]["text"]["content"] == "abc"
    await lc.aclose()
