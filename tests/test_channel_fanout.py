"""FanoutStreamingOutput 单元测试。"""

from feishu_dispatcher.channel.fanout import FanoutStreamingOutput
from tests.conversation_fakes import ConversationRefFactory as ConversationRef


async def test_fanout_streaming_output_isolates_target_failures(caplog):
    class RecordingOutput:
        def __init__(self) -> None:
            self.text = ""
            self.footer = ""
            self.flush_count = 0
            self.statuses: list[str] = []
            self.closed = False

        def feed(self, text: str) -> None:
            self.text += text

        def set_footer(self, footer: str) -> None:
            self.footer = footer

        async def flush(self) -> None:
            self.flush_count += 1

        async def set_status(self, status: str) -> None:
            self.statuses.append(status)

        async def aclose(self) -> None:
            self.closed = True

    class BrokenOutput:
        def feed(self, text: str) -> None:  # noqa: ARG002
            raise RuntimeError("feed boom")

        def set_footer(self, footer: str) -> None:  # noqa: ARG002
            raise RuntimeError("footer boom")

        async def flush(self) -> None:
            raise RuntimeError("flush boom")

        async def set_status(self, status: str) -> None:  # noqa: ARG002
            raise RuntimeError("status boom")

        async def aclose(self) -> None:
            raise RuntimeError("close boom")

    first = RecordingOutput()
    second = RecordingOutput()
    output = FanoutStreamingOutput(
        [
            (ConversationRef("feishu", "thread-a"), first),
            (ConversationRef("web", "thread-b"), second),
            (ConversationRef("broken", "thread-c"), BrokenOutput()),
        ]
    )

    with caplog.at_level("ERROR"):
        output.feed("hello")
        output.set_footer("footer")
        await output.flush()
        await output.set_status("done")
        await output.aclose()

    for target in (first, second):
        assert target.text == "hello"
        assert target.footer == "footer"
        assert target.flush_count == 1
        assert target.statuses == ["done"]
        assert target.closed
    assert "conversation=broken:thread-c" in caplog.text
