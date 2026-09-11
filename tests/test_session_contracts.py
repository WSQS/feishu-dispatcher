"""Session 契约的归属与边界测试。

本轮只提升 Session Runtime 契约；SessionEvent 按治理决定留在 agent 孵化器。
"""

from importlib.util import find_spec
from typing import get_type_hints

import feishu_dispatcher.agent.session as implementations
import feishu_dispatcher.agent.session.acp_session_runtime as acp_module
import feishu_dispatcher.agent.session.tool_loop_session_runtime as tool_loop_module
import feishu_dispatcher.agent.session_event as events_module
import feishu_dispatcher.session_runtime as runtime_contract
from feishu_dispatcher.agent.session_event import SessionEvent, SessionState
from feishu_dispatcher.conversation import ConversationRef
from feishu_dispatcher.session_runtime import (
    SessionRuntime,
    TurnReceipt,
    TurnRef,
    TurnRequest,
)

#: 已提升的 Session Runtime 契约名；实现包不得重新导出。
_CONTRACT_NAMES = (
    "SessionRuntime",
    "SessionEventListener",
    "TurnRequest",
    "TurnRef",
    "TurnReceipt",
    "TurnPlacement",
)


def test_promoted_session_runtime_contract_has_one_authoritative_definition() -> None:
    assert SessionRuntime.__module__ == "feishu_dispatcher.session_runtime"
    assert TurnRequest.__module__ == "feishu_dispatcher.session_runtime"
    assert TurnRef.__module__ == "feishu_dispatcher.session_runtime"
    assert TurnReceipt.__module__ == "feishu_dispatcher.session_runtime"
    assert find_spec("feishu_dispatcher.agent.session.session_runtime") is None


def test_session_event_stays_in_agent_zone() -> None:
    assert SessionEvent.__module__ == "feishu_dispatcher.agent.session_event"
    assert find_spec("feishu_dispatcher.session_event") is None


def test_runtime_contract_references_agent_zone_event_types() -> None:
    """提升后的契约引用 agent 孵化器的事件类型——本轮记录的暂时例外（见 AGENTS.md）。

    显式断言"同一份定义"：契约侧看到的 SessionEvent / SessionState 就是 agent 孵化器
    里的那一个对象，而不是复制出来的第二份定义。
    """
    assert runtime_contract.SessionEvent is events_module.SessionEvent
    assert runtime_contract.SessionState is events_module.SessionState


def test_session_runtime_protocol_uses_session_event_types() -> None:
    """协议签名指向 agent 孵化器里的事件类型（本轮不提升事件契约）。

    ``SessionState`` 是 ``Literal`` 别名，其 ``__module__`` 恒为 ``typing``，
    故按协议里 ``state`` 属性的返回类型断言归属。
    """
    state_property = SessionRuntime.__dict__["state"]

    assert get_type_hints(state_property.fget)["return"] is SessionState


def test_session_implementation_package_does_not_reexport_contracts() -> None:
    for name in _CONTRACT_NAMES:
        assert not hasattr(implementations, name)


def test_session_implementations_use_contract_types() -> None:
    assert acp_module.SessionEvent is SessionEvent
    assert acp_module.SessionState is SessionState
    assert acp_module.ConversationRef is ConversationRef
    assert tool_loop_module.SessionEvent is SessionEvent
    assert tool_loop_module.ConversationRef is ConversationRef


def test_turn_values_reference_promoted_conversation_ref() -> None:
    assert get_type_hints(TurnRequest)["conversation"] is ConversationRef
    assert get_type_hints(TurnReceipt)["turn"] is TurnRef
