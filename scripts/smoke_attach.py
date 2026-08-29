"""端到端验证 /attach：真实 opencode 外部会话 → daemon 附着接管 → load_session 恢复。

不经过飞书（用 fake bridge 组装 ``_Daemon`` 实例，照 tests 基建），验证 /attach 的核心链路：

1. 真实 opencode ``new_session``，让它记住一个秘密数字，拿 session_id，关闭（模拟外部 CLI 已停）。
2. 用 fake bridge 组装 ``_Daemon``（agent=opencode、项目=仓库根），走 ``/attach`` 附着该 session
   （先 load_session 探测、成功才建 Task、拉起时复用 load_session 恢复路径）。
3. 在新 Conversation 里回复「召回秘密数字」，验证 load_session 接回原上下文。

用法：uv run python scripts/smoke_attach.py
前置：opencode 已配好 provider（`opencode providers`）。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from feishu_dispatcher.acp_client import AcpAgent, AgentOutputChunk, AgentSpawn
from feishu_dispatcher.channel import ChannelMessage, StreamingOutput
from feishu_dispatcher.config import Config, Project
from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.daemon import _Daemon
from feishu_dispatcher.session_event import SessionEvent
from feishu_dispatcher.store import ProjectStore, SessionStore
from feishu_dispatcher.throttler import StreamThrottler

REPO_ROOT = str(Path(__file__).resolve().parent.parent)
SECRET = "4287"
AGENT = "opencode"

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


class Collector:
    def __init__(self) -> None:
        self.buf: list[str] = []

    async def __call__(self, output: AgentOutputChunk) -> None:
        self.buf.append(output.display_text)

    def take(self) -> str:
        out = "".join(self.buf)
        self.buf.clear()
        return out


class Bridge:
    """最小 Channel：记录 Conversation 内文本，不真发飞书。"""

    def __init__(self) -> None:
        self.threads: dict[str, list[str]] = {}
        self.plains: list[str] = []
        self._root_seq = 0

    def send_root_message(self, chat_id: str, text: str) -> str:
        self._root_seq += 1
        mid = f"om_root_{self._root_seq}"
        self.threads.setdefault(mid, [])
        return mid

    def reply_in_thread(self, root_message_id: str, text: str) -> str:
        self.threads.setdefault(root_message_id, []).append(text)
        return f"om_rep_{len(self.threads[root_message_id])}"

    def create_thread(self, initial_text: str) -> str:
        return self.send_root_message("oc_smoke", initial_text)

    def send_text(self, conversation: ConversationRef, text: str) -> str:
        return self.reply_in_thread(conversation.conversation_id, text)

    def handle_session_event(
        self,
        conversation_id: str,
        event: SessionEvent,
        *,
        trace_sequence: int | None = None,
    ) -> None:
        return None

    def open_output(
        self,
        conversation: ConversationRef,
        title: str,
        *,
        footer: str = "",
    ) -> StreamingOutput:
        async def send_piece(piece: str) -> None:
            await asyncio.to_thread(self.send_text, conversation, piece)

        return StreamThrottler(send_piece, window=0.01)

    def reply(self, message_id: str, text: str) -> str:
        self.plains.append(text)
        return "om_plain"

    def reply_card(self, root_message_id: str, card: dict) -> str:
        return "om_card"

    def patch_card(self, message_id: str, card: dict) -> None:
        return None

    def stop(self) -> None:
        return None

    def is_alive(self) -> bool:
        return True

    def restart(self) -> None:
        return None

    def all_text(self) -> str:
        return "\n".join("\n".join(v) for v in self.threads.values())


async def _wait_for(cond, timeout: float, what: str) -> None:
    async def _poll() -> None:
        while not cond():
            await asyncio.sleep(0.2)

    try:
        await asyncio.wait_for(_poll(), timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"等待超时：{what}")


def _attach_root(store: SessionStore) -> str | None:
    tasks = store.all()
    return tasks[0].conversation_ref.conversation_id if tasks else None


def _root_msg(text: str) -> ChannelMessage:
    return ChannelMessage(
        conversation_id="oc_smoke",
        message_id="om_att",
        thread_id=None,
        text=text,
        sender_id="ou_user",
    )


def _conversation_msg(conversation_id: str, text: str) -> ChannelMessage:
    return ChannelMessage(
        conversation_id=conversation_id,
        message_id="om_t2",
        thread_id=None,
        text=text,
        sender_id="ou_user",
    )


async def main() -> int:
    # 1. 真实 opencode 外部会话：记住秘密数字，关闭（模拟外部 CLI 已退出）
    col = Collector()
    a1 = AcpAgent(AgentSpawn(command=[AGENT, "acp"], cwd=REPO_ROOT), col)
    await a1.start()
    sid = a1.session_id
    print(f"=== phase1 external session = {sid} ===", flush=True)
    await a1.prompt(
        f"Remember this secret number for later: {SECRET}. Acknowledge briefly."
    )
    print(f"[phase1 store] {col.take()!r}", flush=True)
    await a1.aclose()
    print("=== phase1 closed (simulating external CLI exit) ===", flush=True)

    # 2. 组装 daemon（fake bridge）走 /attach：先探测、成功才建 Task、load_session 拉起
    cfg = Config(
        app_id="smoke",
        app_secret="smoke",
        chat_id="oc_smoke",
        agents={"opencode": ["opencode", "acp"]},
        projects={"demo": Project(name="demo", path=Path(REPO_ROOT))},
        stream_mode="text",
        agent_start_timeout=60.0,
    )
    store = SessionStore(None)
    bridge = Bridge()
    daemon = _Daemon(
        cfg,
        store=store,
        project_store=ProjectStore(None),
        _channels={"feishu": bridge},
        _primary_channel_key="feishu",
    )

    await daemon._handle_message(_root_msg(f"/attach demo {AGENT} {sid}"))
    await _wait_for(lambda: _attach_root(store) is not None, 120, "附着建 Task")
    root = _attach_root(store)
    task = store.by_conversation(ConversationRef("feishu", root))
    assert task is not None
    print(
        f"=== attached conversation={root} session={task.session_id} origin={task.origin} ===",
        flush=True,
    )
    await _wait_for(
        lambda: any("已附着外部会话" in t for t in bridge.threads.get(root, [])),
        120,
        "附着摘要",
    )

    # 3. Conversation 回复 → load_session 接回原上下文 → 召回秘密
    await daemon._handle_channel_message(
        "feishu",
        _conversation_msg(
            task.conversation_ref.conversation_id,
            "What is the secret number I asked you to remember? "
            "Reply with just the number.",
        ),
    )
    try:
        await _wait_for(lambda: SECRET in bridge.all_text(), 240, "召回秘密数字")
        ok = True
    except TimeoutError:
        ok = False

    recall = bridge.all_text()
    print(f"=== recall output:\n{recall}", flush=True)
    await daemon._shutdown()

    print(
        f"\n=== RESULT: attach {'SURVIVED ✅' if ok else 'LOST ❌'} ===",
        flush=True,
    )
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
