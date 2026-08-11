"""IRC 端命令解析与权限注册表。"""

from __future__ import annotations

from dataclasses import dataclass

# 命令 → 所需最低权限级别（chat/help 之外全部经 server 权限判定）
COMMAND_LEVELS: dict[str, str] = {
    "chat": "everyone",
    "help": "everyone",
    "ban": "botop",
    "unban": "botop",
    "whitelist": "botop",
    "blacklist": "botop",
    "info": "botop",
    "botop": "oper",
    "shellop": "oper",
    "confirm": "botop",
    "reject": "botop",
    "runcmd": "shellop",
}


@dataclass(frozen=True)
class ParsedCommand:
    cmd: str
    args: list[str]


def parse_command(text: str, bot_name: str) -> ParsedCommand | None:
    """解析 !<bot_name> <cmd> <args...>；不匹配返回 None。"""
    prefix = f"!{bot_name}"
    stripped = text.strip()
    if not stripped.startswith(prefix):
        return None
    rest = stripped[len(prefix):].strip()
    if not rest:
        return None
    parts = rest.split()
    return ParsedCommand(cmd=parts[0].lower(), args=parts[1:])
