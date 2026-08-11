"""统一单类型 client：仅执行 runcmd/getroot（纯 shell 执行器）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .config import load_yaml
from .server_link import ServerLink


class ShellClient:
    """连接 server 并执行 shell 命令的轻量节点。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg = load_yaml(self.config_path)
        client_cfg = self.cfg["client"]
        self.link = ServerLink(self.cfg, "shell", client_cfg.get("bot_name", "shell"))

    async def run(self) -> None:
        await self.link.start()
        await asyncio.Event().wait()
