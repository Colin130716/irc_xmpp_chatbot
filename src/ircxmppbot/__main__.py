"""CLI 入口：python -m ircxmppbot server|client-irc|client-xmpp [config]"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .config import load_yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIGS = {
    "server": "configs/server.yaml",
    "client-irc": "configs/client_irc.yaml",
    "client-xmpp": "configs/client_xmpp.yaml",
}


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


async def _run_server(config_path: Path) -> None:
    from .server import BotServer

    cfg = load_yaml(config_path)
    server = BotServer(config_path)
    server.reload(cfg)
    log.info("server 启动（配置 %s）", config_path)
    await server.run()


async def _run_client(config_path: Path, kind: str) -> None:
    if kind == "irc":
        from .irc_client import IRCBot

        await IRCBot(config_path).run()
    else:
        from .xmpp_client import XMPPBot

        await XMPPBot(config_path).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ircxmppbot")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("server", "client-irc", "client-xmpp"):
        p = sub.add_parser(name)
        p.add_argument("config", nargs="?", default=DEFAULT_CONFIGS[name])
    parser.add_argument("-d", "--debug", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.debug)
    config_path = Path(args.config)
    try:
        if args.mode == "server":
            asyncio.run(_run_server(config_path))
        else:
            kind = args.mode.removeprefix("client-")
            asyncio.run(_run_client(config_path, kind))
    except KeyboardInterrupt:
        log.info("收到中断信号，退出")
    except Exception as e:  # noqa: BLE001
        log.error("运行失败: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
