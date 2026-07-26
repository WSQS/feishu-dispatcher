"""``fdx``：agent 侧 CLI —— 在 coding agent 的工作区里调用，请 daemon 代做事。

与运维用的 ``feishu-dispatcher``（启 daemon）分开：``fdx`` 是给 **agent** 用的通用控制面
入口，子命令**分组**，第一组是 ``bg``（后台任务，#68），后续会加别的方向组。

只用标准库、且身份全从环境变量拿（daemon 启 agent 时经 ``AgentSpawn.env`` 注入
``FEISHU_DISPATCHER_URL`` / ``FEISHU_DISPATCHER_TOKEN``，逐层透传到 agent 跑的 shell 命令）——
agent 无需、也拿不到自己的 task_id，token 即身份。故本模块保持零重依赖、启动快。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request


def _post(path: str, payload: dict) -> dict:
    """带 Bearer token POST 到 daemon 控制面，返回 JSON。失败抛异常。"""
    url = (os.environ.get("FEISHU_DISPATCHER_URL") or "").rstrip("/")
    token = os.environ.get("FEISHU_DISPATCHER_TOKEN") or ""
    if not url or not token:
        raise RuntimeError(
            "未在 feishu-dispatcher 的 agent 环境里运行"
            "（缺 FEISHU_DISPATCHER_URL / FEISHU_DISPATCHER_TOKEN）。"
        )
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url + path,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cmd_bg_run(args: argparse.Namespace) -> int:
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]  # argparse 有时把分隔符 -- 一起塞进 REMAINDER
    if not command:
        print("用法：fdx bg run -- <命令> [参数...]", file=sys.stderr)
        return 2
    try:
        resp = _post("/v1/bg/run", {"command": command})
    except Exception as exc:  # noqa: BLE001
        print(f"提交后台任务失败：{exc}", file=sys.stderr)
        return 1
    job_id = resp.get("job_id")
    if not job_id:
        print(f"提交后台任务失败：{resp.get('error') or resp}", file=sys.stderr)
        return 1
    print(
        f"后台任务已启动：{job_id}。它由 dispatcher 拥有并托管，"
        "完成时会自动通知你继续——**不要**轮询、不要 sleep 等待。"
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fdx", description="feishu-dispatcher agent 侧 CLI"
    )
    groups = parser.add_subparsers(dest="group", required=True)

    bg = groups.add_parser("bg", help="后台任务：起长任务、跑完自动唤回 agent")
    bg_cmds = bg.add_subparsers(dest="bg_cmd", required=True)
    run = bg_cmds.add_parser(
        "run",
        help="起一个后台任务（dispatcher 托管进程，完成时自动唤回你）",
    )
    run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="`--` 之后是要在后台跑的命令，如 `-- python train.py`",
    )
    run.set_defaults(func=_cmd_bg_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
