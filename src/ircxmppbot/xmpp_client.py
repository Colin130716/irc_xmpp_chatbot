"""XMPP 客户端：slixmpp 封装，仅支持 chat（私聊免前缀/群聊带前缀）。"""

from __future__ import annotations

import asyncio
import logging
import ssl
from pathlib import Path

import slixmpp

from .commands import parse_command
from .config import load_yaml
from .llm import LLMClient, LLMError
from .protocol import (
    decode_msg,
    encode_msg,
    make_auth,
    make_runcmd_result,
)
from .util import backoff_delay, split_text

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
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0045")
        self.register_plugin("xep_0199")
        self.add_event_handler("session_start", self._session_start)
        self.add_event_handler("message", self._on_message)
        self.add_event_handler("groupchat_message", self._on_groupchat)
        self._server_writer = None
        self._server_task: asyncio.Task | None = None

    # ---------- slixmpp 生命周期 ----------

    async def _session_start(self, event) -> None:
        await self.get_roster()
        self.send_presence()
        for room in self.cfg.get("xmpp", {}).get("mucs", []):
            self.plugin["xep_0045"].join_muc(room, self.nick)
        if self._server_task is None or self._server_task.done():
            self._server_task = asyncio.create_task(self._server_loop())
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
        for chunk in split_text(reply, XMPP_LINE_LIMIT):
            self.send_message(mto=target, mbody=chunk, mtype=mtype)

    # ---------- server 连接 ----------

    async def _server_loop(self) -> None:
        srv_cfg = self.cfg["client"]["server"]
        attempt = 0
        while True:
            try:
                await self._server_connect(srv_cfg)
                attempt = 0
            except (ConnectionError, OSError, asyncio.IncompleteReadError, TimeoutError) as e:
                delay = backoff_delay(attempt)
                log.warning("server 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _server_connect(self, srv_cfg: dict) -> None:
        ssl_ctx = None
        tls_cfg = srv_cfg.get("tls", {})
        if tls_cfg:
            verify = tls_cfg.get("verify", True)
            ssl_ctx = ssl.create_default_context()
            if not verify:
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
            if tls_cfg.get("ca_cert"):
                ssl_ctx.load_verify_locations(tls_cfg["ca_cert"])
        reader, writer = await asyncio.open_connection(
            srv_cfg["host"], int(srv_cfg["port"]), ssl=ssl_ctx
        )
        self._server_writer = writer
        client_cfg = self.cfg["client"]
        await self._server_send(make_auth(
            srv_cfg["token"], client_cfg["name"], "xmpp", self.bot_name
        ))
        log.info("已连接 server: %s:%s", srv_cfg["host"], srv_cfg["port"])
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("server 断开")
            try:
                msg = decode_msg(line.decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                log.warning("协议错误: %s", e)
                continue
            await self._on_server_msg(msg)

    async def _server_send(self, msg: dict) -> None:
        if self._server_writer is None:
            raise ConnectionError("未连接 server")
        self._server_writer.write(encode_msg(msg))
        await self._server_writer.drain()

    async def _on_server_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "runcmd":
            await self._exec_runcmd(msg)
        elif t == "auth_ok":
            if not msg.get("ok"):
                log.error("server 认证失败，请检查 token")
        # XMPP 端不参与权限机制，其余消息忽略

    async def _exec_runcmd(self, msg: dict) -> None:
        task_id = msg["task_id"]
        cmd = msg["cmd"]
        log.info("执行 runcmd: %s", cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        await self._server_send(make_runcmd_result(task_id, proc.returncode == 0, output))

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
            except (ConnectionError, OSError) as e:
                log.warning("XMPP 连接异常: %s", e)
            delay = backoff_delay(attempt)
            await asyncio.sleep(delay)
            attempt += 1
