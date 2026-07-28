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
import time
import urllib.request


def _fmt_ts(ts: float) -> str:
    """epoch 秒 → 本地 `MM-DD HH:MM`；0/无 → `-`。"""
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _status_str(job: dict) -> str:
    """job 状态串：running / exited(0) / killed / timed_out。"""
    st = job.get("status") or "?"
    if job.get("timed_out"):
        return "timed_out"
    if st == "exited":
        return f"exited({job.get('exit_code')})"
    return st


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
        print("用法：fdx bg run [--timeout 秒] -- <命令> [参数...]", file=sys.stderr)
        return 2
    payload: dict = {"command": command}
    if args.timeout and args.timeout > 0:
        payload["timeout"] = args.timeout
    try:
        resp = _post("/v1/bg/run", payload)
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


def _cmd_bg_list(args: argparse.Namespace) -> int:
    try:
        resp = _post("/v1/bg/list", {})
    except Exception as exc:  # noqa: BLE001
        print(f"列出后台任务失败：{exc}", file=sys.stderr)
        return 1
    jobs = resp.get("jobs") or []
    if not jobs:
        print("（本任务暂无后台任务）")
        return 0
    for j in jobs:
        print(
            f"{j.get('job_id'):<5} {_status_str(j):<12} "
            f"起于 {_fmt_ts(j.get('created_at', 0))}  {j.get('command', '')}"
        )
    return 0


def _cmd_bg_logs(args: argparse.Namespace) -> int:
    try:
        resp = _post("/v1/bg/logs", {"id": args.id, "tail": args.tail})
    except Exception as exc:  # noqa: BLE001
        print(f"读取后台任务输出失败：{exc}", file=sys.stderr)
        return 1
    if resp.get("error"):
        print(resp["error"], file=sys.stderr)
        return 1
    print(f"[{resp.get('job_id')}] {_status_str(resp)}  {resp.get('command', '')}")
    print(f"--- 输出（末 {args.tail} 行）---")
    print(resp.get("output") or "（暂无输出）")
    return 0


def _cmd_bg_kill(args: argparse.Namespace) -> int:
    try:
        resp = _post("/v1/bg/kill", {"id": args.id})
    except Exception as exc:  # noqa: BLE001
        print(f"终止后台任务失败：{exc}", file=sys.stderr)
        return 1
    if resp.get("error"):
        print(resp["error"], file=sys.stderr)
        return 1
    if resp.get("killed"):
        print(f"已请求终止 {resp.get('job_id')}。")
    else:
        print(f"{resp.get('job_id')}：{resp.get('note') or '未在运行'}。")
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
        "--timeout",
        type=float,
        default=0.0,
        help="超时秒数（>0 时超时杀掉并汇报）；默认用 daemon 配置（通常不超时）",
    )
    run.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="`--` 之后是要在后台跑的命令，如 `-- python train.py`",
    )
    run.set_defaults(func=_cmd_bg_run)

    ls = bg_cmds.add_parser("list", help="列出本任务起的后台任务")
    ls.set_defaults(func=_cmd_bg_list)

    logs = bg_cmds.add_parser("logs", help="查看某后台任务的输出尾部（中途查进度）")
    logs.add_argument("id", help="job id，如 j3（用 `fdx bg list` 查）")
    logs.add_argument("--tail", type=int, default=50, help="末尾行数（默认 50）")
    logs.set_defaults(func=_cmd_bg_logs)

    kill = bg_cmds.add_parser("kill", help="终止一个在跑的后台任务")
    kill.add_argument("id", help="job id，如 j3")
    kill.set_defaults(func=_cmd_bg_kill)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
