"""把 SessionEvent 的业务事实转换成 Channel 共用展示元数据。"""

from __future__ import annotations

from ..session_event import AgentOutputMetadata


def format_agent_output_title(metadata: AgentOutputMetadata | None) -> str:
    """生成一轮 Agent 输出的标题。"""
    if metadata is None:
        return "Agent"
    return f"{metadata.project_name} · {metadata.agent_label}"


def format_agent_output_footer(
    metadata: AgentOutputMetadata | None,
    *,
    usage_tokens: int | None = None,
) -> str:
    """生成一轮 Agent 输出的 footer。"""
    parts: list[str] = []
    if metadata is not None:
        parts.append(metadata.project_name)
        if metadata.model:
            parts.append(f"模型：{metadata.model}")
        issue_tag = _issue_tag(metadata.issue_url)
        if issue_tag:
            parts.append(issue_tag)
    if usage_tokens is not None:
        parts.append(format_usage_tokens(usage_tokens))
    return " · ".join(parts)


def format_usage_tokens(tokens: int) -> str:
    """把 token 数压成人读的小字。"""
    for unit, divisor in (("M", 1_000_000), ("k", 1000)):
        if tokens >= divisor:
            value = f"{tokens / divisor:.1f}".rstrip("0").rstrip(".")
            return f"~{value}{unit} tok"
    return f"~{tokens} tok"


def _issue_tag(issue_url: str) -> str:
    if not issue_url:
        return ""
    tail = issue_url.rstrip("/").rsplit("/", 1)[-1]
    return f"#{tail}" if tail.isdigit() else ""
