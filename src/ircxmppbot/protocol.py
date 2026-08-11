"""server↔client 协议：JSON lines 编解码与消息构造器（仅执行类消息）。"""

from __future__ import annotations

import json

VALID_TYPES = frozenset(
    {
        "auth",
        "auth_ok",
        "runcmd",
        "runcmd_result",
        "getroot",
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
    _validate(msg)
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_msg(line: str) -> dict:
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


def make_runcmd(task_id: str, cmd: str, caller_userhost: str = "") -> dict:
    return {
        "type": "runcmd",
        "task_id": task_id,
        "cmd": cmd,
        "caller_userhost": caller_userhost,
    }


def make_getroot(task_id: str, password: str, caller_userhost: str) -> dict:
    return {
        "type": "getroot",
        "task_id": task_id,
        "password": password,
        "caller_userhost": caller_userhost,
    }


def make_runcmd_result(
    task_id: str,
    ok: bool,
    output: str,
    as_root: bool = False,
    root_expired: bool = False,
) -> dict:
    return {
        "type": "runcmd_result",
        "task_id": task_id,
        "ok": ok,
        "output": output,
        "as_root": as_root,
        "root_expired": root_expired,
    }
