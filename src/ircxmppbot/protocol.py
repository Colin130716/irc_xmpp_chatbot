"""server↔client 协议：JSON lines 编解码与消息构造器。"""

from __future__ import annotations

import json

VALID_TYPES = frozenset(
    {
        "auth",
        "auth_ok",
        "command",
        "command_result",
        "permission_update",
        "shellop_proposal",
        "vote",
        "runcmd",
        "runcmd_result",
        "status",
    }
)


class ProtocolError(Exception):
    """协议层错误。"""


def _validate(msg: dict) -> None:
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError(f"消息必须为含 type 的 dict: {msg!r}")
    if msg["type"] not in VALID_TYPES:
        raise ProtocolError(f"未知消息类型: {msg['type']!r}")


def encode_msg(msg: dict) -> bytes:
    """将消息编码为 JSON line（UTF-8，以 \\n 结尾）。"""
    _validate(msg)
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_msg(line: str) -> dict:
    """解析单行 JSON 消息。"""
    line = line.strip()
    if not line:
        raise ProtocolError("空行")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"JSON 解析失败: {e}") from e
    _validate(msg)
    return msg


def make_auth(token: str, client_name: str, client_type: str, bot_name: str) -> dict:
    return {
        "type": "auth",
        "token": token,
        "client_name": client_name,
        "client_type": client_type,
        "bot_name": bot_name,
    }


def make_command(
    cmd: str,
    args: list[str],
    caller_userhost: str,
    caller_is_oper: bool,
    channel: str | None = None,
) -> dict:
    return {
        "type": "command",
        "cmd": cmd,
        "args": args,
        "caller_userhost": caller_userhost,
        "caller_is_oper": caller_is_oper,
        "channel": channel,
    }


def make_command_result(
    ok: bool,
    reply: str,
    target_type: str,
    target: str,
    action: str = "none",
    action_args: list[str] | None = None,
) -> dict:
    return {
        "type": "command_result",
        "ok": ok,
        "reply": reply,
        "target_type": target_type,
        "target": target,
        "action": action,
        "action_args": action_args or [],
    }


def make_permission_update(snapshot: dict, oper_cache: dict) -> dict:
    return {"type": "permission_update", **snapshot, "oper_cache": oper_cache}


def make_shellop_proposal(
    proposal_id: str, candidate: str, deadline_ts: float, voters: list[str]
) -> dict:
    return {
        "type": "shellop_proposal",
        "proposal_id": proposal_id,
        "candidate": candidate,
        "deadline_ts": deadline_ts,
        "voters": voters,
    }


def make_vote(proposal_id: str, vote: str, voter_userhost: str) -> dict:
    return {
        "type": "vote",
        "proposal_id": proposal_id,
        "vote": vote,
        "voter_userhost": voter_userhost,
    }


def make_runcmd(task_id: str, cmd: str) -> dict:
    return {"type": "runcmd", "task_id": task_id, "cmd": cmd}


def make_runcmd_result(task_id: str, ok: bool, output: str) -> dict:
    return {"type": "runcmd_result", "task_id": task_id, "ok": ok, "output": output}
