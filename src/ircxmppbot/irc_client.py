"""IRC 客户端：pydle 封装 + 命令分发 + oper WHOIS + 黑白名单踢人。"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import pydle

from .commands import parse_command
from .config import load_yaml
from .llm import LLMClient, LLMError
from .permissions import Permissions
from .protocol import make_command, make_reachability, make_vote
from .server_link import ServerLink
from .util import parse_userhost, split_text_bytes

log = logging.getLogger(__name__)

IRC_LINE_LIMIT = 400  # IRC 512 字节/行的安全上限
WHOIS_TIMEOUT = 8.0


class IRCBot(pydle.Client):
    """IRC 端机器人。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg = load_yaml(self.config_path)
        client_cfg = self.cfg["client"]
        irc_cfg = self.cfg.get("irc", {})
        super().__init__(
            client_cfg["bot_name"],
            username=client_cfg["bot_name"],
            realname=irc_cfg.get("realname", client_cfg["bot_name"]),
        )
        self.bot_name = client_cfg["bot_name"]
        self.permissions = Permissions()
        # userhost -> is_oper（本地镜像，来自 server 推送 + 本地 WHOIS）
        self.oper_cache: dict[str, bool] = {}
        self.llm = LLMClient(self.cfg.get("llm", {}))
        self.link = ServerLink(self.cfg, "irc", self.bot_name, self._on_link_msg)
        self.root_sessions = self.link.root_sessions  # 兼容既有测试/外部引用

    # ---------- pydle 生命周期 ----------

    async def on_connect(self) -> None:
        await super().on_connect()
        for channel in self.cfg.get("irc", {}).get("channels", []):
            await self.join(channel)
        await self.link.start()
        log.info("IRC 已连接并加入频道: %s", self.cfg.get("irc", {}).get("channels", []))

    # ---------- server 连接（共享 ServerLink 处理连接与 runcmd/getroot） ----------

    async def _on_link_msg(self, link: ServerLink, msg: dict) -> None:
        """处理 client 特有消息；连接/runcmd/getroot/auth_ok 由 ServerLink 处理。"""
        t = msg.get("type")
        if t == "permission_update":
            self.permissions.apply_snapshot(msg)
            self.oper_cache = {parse_userhost(k): bool(v) for k, v in (msg.get("oper_cache") or {}).items()}
            await self._enforce_lists_all_channels()
        elif t == "shellop_proposal":
            await self._dm_proposal(msg)
        elif t == "command_result":
            await self._handle_command_result(msg)
        else:
            log.debug("忽略 server 消息: %s", t)

    async def _server_send(self, msg: dict) -> None:
        # 兼容别名：统一走 ServerLink.send
        await self.link.send(msg)

    # ---------- 命令处理 ----------

    async def on_channel_message(self, target, source, message) -> None:
        await self._maybe_command(message, source, target, is_channel=True)

    async def on_message(self, target, source, message) -> None:
        # pydle 私信：target 是发送者
        await self._maybe_command(message, source, source, is_channel=False)

    async def _maybe_command(self, text, source, target, is_channel) -> None:
        if source == self.nickname:
            return
        parsed = parse_command(text, self.bot_name)
        if parsed is None:
            return
        if parsed.cmd == "chat":
            await self._chat(" ".join(parsed.args), source, target, is_channel)
            return
        if parsed.cmd == "help":
            await self.message(source, "命令: chat/help/ban/unban/whitelist/blacklist/botop/shellop/confirm/reject/runcmd/info")
            return
        if parsed.cmd in ("confirm", "reject"):
            # 投票走专用 vote 消息（server 端由 _handle_vote 处理）
            await self._server_vote(parsed.cmd, parsed.args, source)
            return
        # 其余命令上报 server 做权限判定
        await self._server_command(parsed.cmd, parsed.args, source, target, is_channel)

    async def _server_vote(self, vote: str, args: list[str], source: str) -> None:
        if not args:
            await self.message(source, f"用法: {vote} <提议ID>")
            return
        info = await self._whois_user(source)
        if info is None:
            await self.message(source, "无法获取你的身份信息，请稍后再试")
            return
        userhost = f"{info.get('username', '')}@{info.get('hostname', '')}"
        uh = parse_userhost(userhost)
        self.oper_cache[uh] = bool(info.get("oper"))
        await self._server_send(make_vote(args[0], vote, uh))

    async def _server_command(self, cmd, args, source, target, is_channel) -> None:
        info = await self._whois_user(source)
        if info is None:
            await self.message(source, "无法获取你的身份信息，请稍后再试")
            return
        userhost = f"{info.get('username', '')}@{info.get('hostname', '')}"
        uh = parse_userhost(userhost)
        is_oper = bool(info.get("oper"))
        self.oper_cache[uh] = is_oper
        channel = target if is_channel else None
        await self._server_send(make_command(cmd, args, uh, is_oper, channel))

    async def _whois_user(self, nick) -> dict | None:
        try:
            return await asyncio.wait_for(self.whois(nick), timeout=WHOIS_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("WHOIS %s 超时", nick)
            return None

    async def _chat(self, user_text, source, reply_target, is_channel) -> None:
        if not user_text:
            await self.message(source, "用法: chat <文本>")
            return
        try:
            reply = await self.llm.chat(user_text)
        except LLMError as e:
            reply = f"LLM 错误: {e}"
        limit = min(int(self.cfg.get("llm", {}).get("max_reply_chars", 1500)), IRC_LINE_LIMIT)
        for chunk in split_text_bytes(reply, limit):
            await self.message(reply_target, chunk)

    # ---------- shellop 提议私信 ----------

    async def _dm_proposal(self, msg: dict) -> None:
        remaining = max(0, int(msg.get("deadline_ts", 0) - time.time()))
        text = (
            f"shellop 提议 #{msg['proposal_id']}: 将 {msg['candidate']} 设为 shellop。"
            f"回复 !{self.bot_name} confirm {msg['proposal_id']} 同意 / "
            f"!{self.bot_name} reject {msg['proposal_id']} 拒绝（{remaining} 秒内，全员同意才生效）"
        )
        reachable = []
        for voter in msg.get("voters", []):
            nick = self._nick_for_userhost(voter)
            if nick and nick != self.nickname:
                await self.message(nick, text)
                reachable.append(voter)
        # M8: 上报可达投票人，server 据此定稿 voters（防不可达投票人死锁）
        if reachable:
            await self._server_send(make_reachability(msg["proposal_id"], reachable))

    def _nick_for_userhost(self, userhost: str) -> str | None:
        want = parse_userhost(userhost)
        for nick, info in self.users.items():
            uh = parse_userhost(f"{info.get('username', '')}@{info.get('hostname', '')}")
            if uh == want:
                return nick
        return None

    # ---------- runcmd 执行 ----------

    # root 会话与 getroot/runcmd 执行已移至 ServerLink（self.link）

    # ---------- 命令结果（含 ban/unban 动作） ----------

    async def _handle_command_result(self, msg: dict) -> None:
        ok = bool(msg.get("ok"))
        reply = msg.get("reply", "")
        target = msg.get("target", "")
        target_type = msg.get("target_type", "private")
        action = msg.get("action", "none")
        action_args = msg.get("action_args", [])
        if action == "ban" and action_args:
            mask = self._ban_mask(action_args[0])
            if mask:
                await self.rawmsg("MODE", target, "+b", mask)
        elif action == "unban" and action_args:
            mask = self._ban_mask(action_args[0])
            if mask:
                await self.rawmsg("MODE", target, "-b", mask)
        if not reply:
            return  # 无回复内容则不发送（ban/unban 与 runcmd 的 reply 恒非空）
        if target_type == "private":
            # server 返回的 target 是 user@host，需解析为当前 client 可见的 nick
            nick = self._nick_for_userhost(target)
            if nick is None:
                log.warning("私信目标 %s 在当前 client 不可见，跳过", target)
                return
            await self.message(nick, reply)
        else:
            await self.message(target, reply)

    def _ban_mask(self, nick: str) -> str:
        info = self.users.get(nick)
        if info and info.get("username") and info.get("hostname"):
            return f"{info['username']}@{info['hostname']}"
        return f"{nick}!*@*"

    # ---------- 黑白名单踢人 ----------

    async def on_join(self, channel, user) -> None:
        if user == self.nickname:
            return
        await self._enforce_lists(channel, user)

    async def _enforce_lists_all_channels(self) -> None:
        for channel in list(self.channels):
            for nick in list(self.channels[channel]["users"]):
                await self._enforce_lists(channel, nick)

    async def _enforce_lists(self, channel, nick) -> None:
        p = self.permissions
        if not p.whitelist_enabled and not p.blacklist_enabled:
            return
        info = self.users.get(nick)
        if info:
            hostmask = f"{nick}!{info.get('username', '*')}@{info.get('hostname', '*')}"
        else:
            hostmask = f"{nick}!*@*"
        uh = parse_userhost(hostmask)
        if p.whitelist_enabled:
            if p.whitelist_allows(hostmask, channel):
                return
            if p.is_botop(uh) or self.oper_cache.get(uh, False):
                return
            await self.kick(channel, nick, "白名单模式")
            log.info("白名单踢出 %s from %s", nick, channel)
            return
        if p.blacklist_blocks(hostmask, channel):
            await self.kick(channel, nick, "黑名单")
            log.info("黑名单踢出 %s from %s", nick, channel)

    # ---------- 主入口 ----------

    async def run(self) -> None:
        irc_cfg = self.cfg.get("irc", {})
        attempt = 0
        while True:
            try:
                await self.connect(
                    irc_cfg["host"],
                    port=int(irc_cfg.get("port", 6697)),
                    tls=bool(irc_cfg.get("tls", True)),
                    tls_verify=bool(irc_cfg.get("tls_verify", True)),
                )
                break  # 连接成功
            except (ConnectionError, OSError) as e:
                delay = backoff_delay(attempt)
                log.warning("IRC 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1
        # pydle 在断线时自动重连（RECONNECT_ON_ERROR）并再次触发 on_connect；
        # 主协程保持事件循环运行即可
        await asyncio.Event().wait()
