"""XMPP 客户端：slixmpp 封装，仅支持 chat（私聊免前缀/群聊带前缀）。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import slixmpp

from .commands import parse_command
from .config import load_yaml
from .llm import LLMClient, LLMError
from .server_link import ServerLink
from .util import split_text_bytes

log = logging.getLogger(__name__)

XMPP_LINE_LIMIT = 1500


class XMPPBot(slixmpp.ClientXMPP):
    """XMPP 端机器人（Snikket），不参与权限机制。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg = load_yaml(self.config_path)
        client_cfg = self.cfg["client"]
        xmpp_cfg = self.cfg.get("xmpp", {})
        super().__init__(xmpp_cfg["jid"], xmpp_cfg["password"])
        self.bot_name = client_cfg["bot_name"]
        self.nick = self.bot_name
        self.llm = LLMClient(self.cfg.get("llm", {}))
        self.link = ServerLink(self.cfg, "xmpp", self.bot_name)  # XMPP 无 client 特有消息
        self.root_sessions = self.link.root_sessions  # 兼容既有测试/外部引用
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0045")
        self.register_plugin("xep_0199")
        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("groupchat_message", self._on_groupchat)

    # ---------- slixmpp 生命周期 ----------

    async def _session_start(self, event) -> None:
        await self.get_roster()
        self.send_presence()
        for room in self.cfg.get("xmpp", {}).get("mucs", []):
            self.plugin["xep_0045"].join_muc(room, self.nick)
        await self.link.start()
        log.info("XMPP 已连接并加入 MUC: %s", self.cfg.get("xmpp", {}).get("mucs", []))

    # ---------- 消息处理 ----------

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
        asyncio.create_task(self._chat(msg["from"].bare, " ".join(parsed.args), is_group=True))

    async def _handle_private(self, sender: str, body: str) -> None:
        parsed = parse_command(body, self.bot_name)
        if parsed is None:
            # 私聊免前缀 → 直接 chat
            await self._chat(sender, body, is_group=False)
            return
        if parsed.cmd == "chat":
            await self._chat(sender, " ".join(parsed.args), is_group=False)
        else:
            self.send_message(mto=sender, mbody="XMPP 端仅支持 chat 命令", mtype="chat")

    async def _chat(self, target: str, user_text: str, is_group: bool) -> None:
        mtype = "groupchat" if is_group else "chat"
        if not user_text:
            self.send_message(mto=target, mbody="用法: chat <文本>", mtype=mtype)
            return
        try:
            reply = await self.llm.chat(user_text)
        except LLMError as e:
            reply = f"LLM 错误: {e}"
        for chunk in split_text_bytes(reply, XMPP_LINE_LIMIT):
            self.send_message(mto=target, mbody=chunk, mtype=mtype)

    # ---------- server 连接（共享 ServerLink 处理连接与 runcmd/getroot） ----------

    async def _server_send(self, msg: dict) -> None:
        # 兼容别名：统一走 ServerLink.send
        await self.link.send(msg)

    # ---------- 主入口 ----------

    async def run(self) -> None:
        xmpp_cfg = self.cfg.get("xmpp", {})
        attempt = 0
        while True:
            try:
                # slixmpp connect() 返回 Future，内部会自旋重连直到成功；
                # 初次连接失败后其 _connect_loop 会持续重试，这里用 disconnected 等待
                if xmpp_cfg.get("host"):
                    self.connect((xmpp_cfg["host"], int(xmpp_cfg.get("port", 5222))))
                else:
                    self.connect()
                # XMLStream.disconnected 是文档化的 Future：断开时完成
                await self.disconnected
                attempt = 0  # 连接成功过，重置退避
            except (ConnectionError, OSError) as e:
                log.warning("XMPP 连接异常: %s", e)
            delay = backoff_delay(attempt)
            await asyncio.sleep(delay)
            attempt += 1
