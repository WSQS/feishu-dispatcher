import asyncio

from feishu_dispatcher.throttler import StreamThrottler


class Recorder:
    def __init__(self, fail_times: int = 0) -> None:
        self.batches: list[str] = []
        self.fail_times = fail_times

    async def __call__(self, text: str) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        self.batches.append(text)


async def test_merges_chunks_within_window():
    rec = Recorder()
    t = StreamThrottler(rec, window=0.05)
    t.feed("a")
    t.feed("b")
    t.feed("c")
    await asyncio.sleep(0.2)
    assert rec.batches == ["abc"]
    await t.aclose()


async def test_separate_windows_get_separate_batches():
    rec = Recorder()
    t = StreamThrottler(rec, window=0.05)
    t.feed("a")
    await asyncio.sleep(0.2)
    t.feed("b")
    await asyncio.sleep(0.2)
    assert rec.batches == ["a", "b"]
    await t.aclose()


async def test_flush_sends_immediately():
    rec = Recorder()
    t = StreamThrottler(rec, window=60)
    t.feed("a")
    await t.flush()
    assert rec.batches == ["a"]
    await t.aclose()


async def test_aclose_drains_pending():
    rec = Recorder()
    t = StreamThrottler(rec, window=60)
    t.feed("tail")
    await t.aclose()
    assert rec.batches == ["tail"]


async def test_long_batch_split_by_max_chars():
    rec = Recorder()
    t = StreamThrottler(rec, window=60, max_chars=3)
    t.feed("abcdefgh")
    await t.flush()
    assert rec.batches == ["abc", "def", "gh"]
    await t.aclose()


async def test_sink_error_drops_batch_but_loop_survives():
    rec = Recorder(fail_times=1)
    t = StreamThrottler(rec, window=0.05)
    t.feed("lost")
    await asyncio.sleep(0.2)
    t.feed("kept")
    await asyncio.sleep(0.2)
    assert rec.batches == ["kept"]
    await t.aclose()


async def test_feed_after_close_is_ignored():
    rec = Recorder()
    t = StreamThrottler(rec, window=0.05)
    t.feed("x")
    await t.aclose()
    t.feed("y")
    await asyncio.sleep(0.1)
    assert rec.batches == ["x"]


async def test_empty_feed_never_calls_sink():
    rec = Recorder()
    t = StreamThrottler(rec, window=0.05)
    t.feed("")
    await asyncio.sleep(0.15)
    assert rec.batches == []
    await t.aclose()