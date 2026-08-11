"""XMPP 会话：server 内嵌的 slixmpp 客户端，仅 chat（ChatTarget 实现）。"""

from __future__ import annotations

import asyncio
import logging

import slixmpp

from .commands import parse_command
from .util import split_text_bytes

log = logging.getLogger(__name__)

XMPP_LINE_LIMIT = 1500


class XMPPSession(slixmpp.ClientXMPP):
    """server 内嵌的 XMPP 连接（ChatTarget 实现），仅 chat/help。"""

    session_type = "xmpp"

    def __init__(self, server, xmpp_cfg: dict) -> None:
        self.srv = server
        self.cfg = xmpp_cfg
        self.bot_name = xmpp_cfg.get("bot_name", "qsdwindows_bot")
        self.nick = self.bot_name
        super().__init__(xmpp_cfg["jid"], xmpp_cfg["password"])
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0045")
        self.register_plugin("xep_0199")
        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("groupchat_message", self._on_groupchat)
        # LLM 在 server 上，session 通过 server 调用
        self.llm = self.srv.llm

    async def _session_start(self, event) -> None:
        await self.get_roster()
        self.send_presence()
        for room in self.cfg.get("mucs", []):
            self.plugin["xep_0045"].join_muc(room, self.nick)
        log.info("XMPP 已连接并加入 MUC: %s", self.cfg.get("mucs", []))

    def _on_message(self, msg) -> None:
        if msg["type"] != "chat":
            return
        body = str(msg.get("body", "") or "")
        if not body:
            return
        sender = msg["from"].bare
        if sender == self.boundjid.bare:
            return
        asyncio.create_task(self._handle_private(sender, body))

    def _on_groupchat(self, msg) -> None:
        body = str(msg.get("body", "") or "")
        nick = msg["mucnick"]
        if not body or not nick or nick == self.nick:
            return
        parsed = parse_command(body, self.bot_name)
        if parsed is None or parsed.cmd != "chat":
            return
        asyncio.create_task(self.srv.handle_chat_command(
            "chat", parsed.args, msg["from"].bare, False, msg["from"].bare, self
        ))

    async def _handle_private(self, sender: str, body: str) -> None:
        parsed = parse_command(body, self.bot_name)
        if parsed is None:
            await self.srv.handle_chat_command("chat", [body], sender, False, None, self)
            return
        await self.srv.handle_chat_command(parsed.cmd, parsed.args, sender, False, None, self)

    # ---------- ChatTarget 实现 ----------

    async def reply(self, text: str, channel: str | None, private_to: str | None = None) -> None:
        target = private_to or channel
        if target is None:
            return
        mtype = "groupchat" if channel else "chat"
        for chunk in split_text_bytes(text, XMPP_LINE_LIMIT):
            self.send_message(mto=target, mbody=chunk, mtype=mtype)

    async def execute(self, action: str, action_args: list[str], channel: str | None) -> None:
        # XMPP 不支持 ban/unban 等频道动作
        pass

    async def run(self) -> None:
        xmpp_cfg = self.cfg
        attempt = 0
        while True:
            try:
                if xmpp_cfg.get("host"):
                    self.connect((xmpp_cfg["host"], int(xmpp_cfg.get("port", 5222))))
                else:
                    self.connect()
                await self.disconnected
                attempt = 0
            except (ConnectionError, OSError) as e:
                log.warning("XMPP 连接异常: %s", e)
            delay = min(2**attempt, 60.0)
            await asyncio.sleep(delay)
            attempt += 1
