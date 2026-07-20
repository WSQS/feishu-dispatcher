"""调度器 LLM「大脑」：自然语言 → 理解 → 调用工具派发 agent（P2）。

设计边界（design.md 决策 #10）：轻量 router，只做理解/识别项目/派发/状态查询，
**不写代码、不改文件、不跑命令**——那是底层 agent 的工作。

本模块只含 provider 无关的**工具循环引擎**与**工具定义**：
- :class:`LLMClient` 是最小抽象（OpenAI 兼容的 chat + function calling 形状），
  真实实现（deepseek / GLM / openai 等）在别处注入，测试注入假 client。
- :func:`run_tool_loop` 驱动「LLM 调工具 → 执行 → 结果喂回 → 循环」直到出最终回复。
- :func:`build_scheduler_tools` 把 daemon 能力（列项目 / 派 agent / 查状态）
  包装成工具，daemon 注入实际实现。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


def _short(x: Any, limit: int = 200) -> str:
    """把任意值压成一行短字符串，用于诊断日志（截断，不打全量）。"""
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


class SchedulerMemory:
    """主线（调度器）对话记忆：(user, assistant) 成对，跨重启持久化。

    只存最终问答对（不含中间工具调用），限长保留最近 ``max_messages`` 条。
    ``path=None`` 为纯内存（测试）。原子写 + 读损坏容错，同 SessionStore。
    """

    def __init__(self, path: Path | None, *, max_messages: int = 24) -> None:
        self._path = path
        self._max = max(2, max_messages)
        self._messages: list[dict[str, str]] = []
        if path is not None and path.exists():
            self._load()

    def _load(self) -> None:
        assert self._path is not None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            msgs = [
                {"role": m["role"], "content": m["content"]}
                for m in data
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            ]
            self._messages = msgs[-self._max :]
        except Exception:
            logger.warning("调度器记忆读取失败，忽略: %s", self._path, exc_info=True)
            self._messages = []

    def _flush(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._messages, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception:
            logger.warning("调度器记忆写入失败: %s", self._path, exc_info=True)

    def history(self) -> list[dict[str, str]]:
        return list(self._messages)

    def add_exchange(self, user_message: str, assistant_reply: str) -> None:
        self._messages.append({"role": "user", "content": user_message})
        self._messages.append({"role": "assistant", "content": assistant_reply})
        if len(self._messages) > self._max:
            self._messages = self._messages[-self._max :]
        self._flush()


SYSTEM_PROMPT = """你是一个任务调度器（控制台主线的「控制塔」）。你的职责：
1. 理解用户需求，识别涉及哪些已注册项目（先用 list_projects 查看有哪些项目）。
2. 用 list_tasks 掌握当前有哪些任务及其状态；需要细节时用 get_task(task_id)。
3. 区分「新任务」与「已有任务」——这是关键：
   - 全新的工作 → spawn_agent(project, task) 新建任务（会新建一个飞书话题）。
   - 针对某个**已存在**的任务追加指令/追问/让它继续做某事 → 先 list_tasks 找到它的
     task_id，再 send_to_task(task_id, message)。**不管它此刻在跑还是已挂起
     （suspended/idle），send_to_task 都会自动把会话接回来（load_session）再发，
     无需你先手动恢复**。**绝不要为已有任务重复 spawn 新 agent**——那会丢上下文、留重复话题。
   - resume_task(task_id) 只在这两种情况用：① 只想把 agent 拉回在线、暂时不发消息；
     ② 恢复一个**已终止**（done/stopped/failed）的任务。**给挂起任务发消息不要先 resume**，
     直接 send_to_task 即可。
   - 用户确认某任务已完成、要归档 → mark_done(task_id)。
4. 回答用户关于任务状态的问题（list_tasks / get_task）。
5. 用户要**新增一个项目**（给出项目名/路径/用哪个 agent）→ register_project(name,
   default_agent, path)。三项都必填；**用户没说清用哪个 agent 就先追问**，不要瞎填。
   注册成功后才能对它 spawn_agent。

你不写代码、不改文件、不跑命令——那些是 agent 的工作，你只负责理解、派发与协调。
操作已有任务前务必先 list_tasks 确认 task_id；确认没有对应任务再考虑新建。
做完用一两句话简要告诉用户你做了什么。信息不足以确定项目/任务时，先追问澄清，不要乱建。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    """一次 LLM 应答：要么给最终文本，要么要求调用若干工具。"""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLMClient(Protocol):
    """最小 LLM 抽象（OpenAI 兼容 chat + tools 形状）。"""

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse: ...


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    handler: Callable[[dict[str, Any]], Awaitable[str]]  # 参数 -> 结果字符串


def _tool_defs(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


async def run_tool_loop(
    client: LLMClient,
    user_message: str,
    tools: list[ToolSpec],
    *,
    system_prompt: str = SYSTEM_PROMPT,
    history: list[dict[str, Any]] | None = None,
    max_iters: int = 6,
) -> str:
    """驱动 LLM 工具循环，返回给用户的最终文本。

    ``history`` 是主线（调度器）之前的 (user, assistant) 对话，插在 system 之后、
    本轮 user 之前，给调度器跨消息的上下文（追问/修正/指代）。

    每轮：LLM 应答 → 若无工具调用则结束返回其文本；否则执行每个工具、把结果
    作为 ``role=tool`` 消息喂回，继续下一轮。达到 ``max_iters`` 仍未收敛则兜底。
    工具 handler 抛异常不会中断循环——异常文本作为工具结果喂回，让 LLM 自处理。
    """
    by_name = {t.name: t for t in tools}
    defs = _tool_defs(tools)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": user_message},
    ]
    for _ in range(max_iters):
        resp = await client.chat(messages, defs)
        if not resp.tool_calls:
            # 诊断：LLM 未调任何工具直接收尾——若用户本想「派发/发消息」，这里就能
            # 看出它其实什么都没做（只回了话），是排查「说了没做」的关键信号。
            logger.info("调度器收尾（无工具调用）: %s", _short(resp.content or ""))
            return resp.content or ""
        messages.append(
            {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in resp.tool_calls
                ],
            }
        )
        for tc in resp.tool_calls:
            spec = by_name.get(tc.name)
            if spec is None:
                result = f"未知工具: {tc.name}"
            else:
                try:
                    result = await spec.handler(tc.arguments)
                except Exception as exc:  # 喂回 LLM，让它决定怎么办
                    logger.exception("调度器工具 %s 执行失败", tc.name)
                    result = f"工具 {tc.name} 执行出错: {exc}"
            # 诊断：记下 LLM 到底调了哪个工具、参数、返回什么——排查「发消息不生效」
            # 时能直接看出它有没有真的调 send_to_task、用的哪个 task_id、结果如何。
            logger.info(
                "调度器工具 %s(%s) -> %s",
                tc.name,
                _short(tc.arguments),
                _short(result),
            )
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
    return "（调度器思考步数超限，请把需求说得更具体，或用 `/run <项目> <任务>` 直接派发。）"


def build_scheduler_tools(
    *,
    list_projects: Callable[[], list[dict[str, Any]]],
    spawn_agent: Callable[[str, str], Awaitable[str]],
    list_tasks: Callable[[], list[dict[str, Any]]],
    get_task: Callable[[str], dict[str, Any] | None],
    send_to_task: Callable[[str, str], Awaitable[str]],
    resume_task: Callable[[str], Awaitable[str]],
    mark_done: Callable[[str], Awaitable[str]],
    register_project: Callable[[str, str, str], Awaitable[str]],
) -> list[ToolSpec]:
    """把 daemon 能力包装成调度器工具。查询类（list/get）同步取状态，操作类异步执行。"""

    async def _list_projects(_args: dict[str, Any]) -> str:
        return json.dumps(list_projects(), ensure_ascii=False)

    async def _register_project(args: dict[str, Any]) -> str:
        name = str(args.get("name", "")).strip()
        agent = str(args.get("default_agent", "")).strip()
        path = str(args.get("path", "")).strip()
        if not name or not agent or not path:
            return "参数不足：name、default_agent、path 三项都必填。"
        return await register_project(name, agent, path)

    async def _spawn_agent(args: dict[str, Any]) -> str:
        project = str(args.get("project", "")).strip()
        task = str(args.get("task", "")).strip()
        if not project or not task:
            return "参数不足：project 和 task 都必填。"
        return await spawn_agent(project, task)

    async def _list_tasks(_args: dict[str, Any]) -> str:
        return json.dumps(list_tasks(), ensure_ascii=False)

    async def _get_task(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            return "参数不足：task_id 必填。"
        info = get_task(task_id)
        if info is None:
            return f"未找到任务 {task_id}。"
        return json.dumps(info, ensure_ascii=False)

    async def _send_to_task(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        message = str(args.get("message", "")).strip()
        if not task_id or not message:
            return "参数不足：task_id 和 message 都必填。"
        return await send_to_task(task_id, message)

    async def _resume_task(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            return "参数不足：task_id 必填。"
        return await resume_task(task_id)

    async def _mark_done(args: dict[str, Any]) -> str:
        task_id = str(args.get("task_id", "")).strip()
        if not task_id:
            return "参数不足：task_id 必填。"
        return await mark_done(task_id)

    _task_id_param = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "任务 id（形如 t3，用 list_tasks 查）",
            }
        },
        "required": ["task_id"],
    }

    return [
        ToolSpec(
            name="list_projects",
            description="列出所有已注册项目（含项目名与默认 agent），派发前先了解有哪些项目。",
            parameters={"type": "object", "properties": {}},
            handler=_list_projects,
        ),
        ToolSpec(
            name="spawn_agent",
            description=(
                "给指定项目派发一个**新** coding agent 执行任务，会新建一个飞书话题。"
                "仅用于全新工作；要操作已有任务请改用 send_to_task。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "已注册的项目名"},
                    "task": {
                        "type": "string",
                        "description": "要 agent 做什么，清晰的自然语言描述",
                    },
                },
                "required": ["project", "task"],
            },
            handler=_spawn_agent,
        ),
        ToolSpec(
            name="list_tasks",
            description=(
                "列出所有任务（活跃 + 历史）及其 task_id/项目/状态/轮数/描述。"
                "操作已有任务前先用它确认 task_id。"
            ),
            parameters={"type": "object", "properties": {}},
            handler=_list_tasks,
        ),
        ToolSpec(
            name="get_task",
            description="查看单个任务的详情（状态、轮数、是否有可恢复会话、时间戳等）。",
            parameters=_task_id_param,
            handler=_get_task,
        ),
        ToolSpec(
            name="send_to_task",
            description=(
                "把一条消息/指令转达给**已有**任务的 agent。**不管它在跑还是已挂起"
                "（suspended/idle）都用这个**——在跑就排队执行，挂起会自动 load_session "
                "恢复后再发，**无需先 resume_task**。用于追加指令、追问、让它继续做某事；"
                "不要为此新建 agent。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "目标任务 id（用 list_tasks 查）",
                    },
                    "message": {
                        "type": "string",
                        "description": "要转达给 agent 的内容",
                    },
                },
                "required": ["task_id", "message"],
            },
            handler=_send_to_task,
        ),
        ToolSpec(
            name="resume_task",
            description=(
                "把一个任务拉回在线但**不发消息**（load_session 接回上下文）。只在两种情况"
                "用：① 只想让它上线、暂不发指令；② 恢复一个**已终止**（done/stopped/failed）"
                "的任务。**给挂起（suspended）任务发消息不必用它——直接 send_to_task 会自动"
                "恢复。**"
            ),
            parameters=_task_id_param,
            handler=_resume_task,
        ),
        ToolSpec(
            name="mark_done",
            description="把一个任务标记为完成并归档（done）。用户确认某任务做完时用。",
            parameters=_task_id_param,
            handler=_mark_done,
        ),
        ToolSpec(
            name="register_project",
            description=(
                "注册一个**新项目**，之后就能对它 spawn_agent 派发任务。"
                "三个参数都必填：name（项目名，不能含空格）、default_agent（用哪个 "
                "coding agent，必须是系统已配置的，如 copilot/opencode/claude）、"
                "path（项目在本机的绝对路径）。**若用户没说清用哪个 agent，先追问，"
                "不要自己瞎填。** 只在用户明确要新增项目时用。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "项目名（唯一、不含空格，用于 /run 与 spawn_agent）",
                    },
                    "default_agent": {
                        "type": "string",
                        "description": "默认 coding agent（须为已配置项，如 copilot/opencode/claude）",
                    },
                    "path": {
                        "type": "string",
                        "description": "项目在本机的绝对路径（须为已存在目录）",
                    },
                },
                "required": ["name", "default_agent", "path"],
            },
            handler=_register_project,
        ),
    ]
