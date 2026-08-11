"""IRC 会话：server 内嵌的 pydle 客户端，处理聊天命令与投票私信。"""

from __future__ import annotations

import asyncio
import time

import pydle

from .commands import parse_command
from .util import parse_userhost, split_text_bytes

IRC_LINE_LIMIT = 400  # IRC 512 字节/行的安全上限
WHOIS_TIMEOUT = 8.0


class IRCSession(pydle.Client):
    """server 内嵌的 IRC 连接（ChatTarget 实现）。"""

    session_type = "irc"

    def __init__(self, server, irc_cfg: dict) -> None:
        self.server = server
        self.cfg = irc_cfg
        self.bot_name = irc_cfg.get("bot_name", "qsdwindows_bot")
        super().__init__(
            self.bot_name,
            username=self.bot_name,
            realname=irc_cfg.get("realname", self.bot_name),
        )
        self.oper_cache: dict[str, bool] = {}

    # ---------- pydle 生命周期 ----------

    async def on_connect(self) -> None:
        await super().on_connect()
        for channel in self.cfg.get("channels", []):
            await self.join(channel)
        self.logger.info("IRC 已连接并加入频道: %s", self.cfg.get("channels", []))

    # ---------- 消息处理 ----------

    async def on_channel_message(self, target, source, message) -> None:
        await self._maybe_command(message, source, target, is_channel=True)

    async def on_message(self, target, source, message) -> None:
        await self._maybe_command(message, source, source, is_channel=False)

    async def _maybe_command(self, text, source, target, is_channel) -> None:
        if source == self.nickname:
            return
        parsed = parse_command(text, self.bot_name)
        if parsed is None:
            return
        info = await self._whois_user(source)
        if info is None:
            await self.message(source, "无法获取你的身份信息，请稍后再试")
            return
        userhost = f"{info.get('username', '')}@{info.get('hostname', '')}"
        uh = parse_userhost(userhost)
        is_oper = bool(info.get("oper"))
        self.oper_cache[uh] = is_oper
        channel = target if is_channel else None
        await self.server.handle_chat_command(parsed.cmd, parsed.args, uh, is_oper, channel, self)

    async def _whois_user(self, nick) -> dict | None:
        try:
            return await asyncio.wait_for(self.whois(nick), timeout=WHOIS_TIMEOUT)
        except asyncio.TimeoutError:
            self.logger.warning("WHOIS %s 超时", nick)
            return None

    # ---------- JOIN 踢人 ----------

    async def on_join(self, channel, user) -> None:
        if user == self.nickname:
            return
        await self.server.handle_join(self, channel, user)

    # ---------- ChatTarget 实现 ----------

    async def reply(self, text: str, channel: str | None, private_to: str | None = None) -> None:
        target = private_to or channel
        if target is None:
            return
        if private_to:
            nick = self._nick_for_userhost(private_to)
            if nick is None:
                self.logger.warning("私信目标 %s 不可见，跳过", private_to)
                return
            target = nick
        for chunk in split_text_bytes(text, IRC_LINE_LIMIT):
            await self.message(target, chunk)

    async def execute(self, action: str, action_args: list[str], channel: str | None) -> None:
        if action == "ban" and action_args and channel:
            mask = self._ban_mask(action_args[0])
            if mask:
                await self.rawmsg("MODE", channel, "+b", mask)
        elif action == "unban" and action_args and channel:
            mask = self._ban_mask(action_args[0])
            if mask:
                await self.rawmsg("MODE", channel, "-b", mask)

    async def dm_voters(self, proposal_id, candidate, voters, deadline_ts) -> list[str]:
        """私信可达投票人，返回可达 userhost 列表（server 据此定稿 voters）。"""
        remaining = max(0, int(deadline_ts - time.time()))
        text = (
            f"shellop 提议 #{proposal_id}: 将 {candidate} 设为 shellop。"
            f"回复 !{self.bot_name} confirm {proposal_id} 同意 / "
            f"!{self.bot_name} reject {proposal_id} 拒绝（{remaining} 秒内，全员同意才生效）"
        )
        reachable = []
        for voter in voters:
            nick = self._nick_for_userhost(voter)
            if nick and nick != self.nickname:
                await self.message(nick, text)
                reachable.append(voter)
        return reachable

    def _nick_for_userhost(self, userhost: str) -> str | None:
        want = parse_userhost(userhost)
        for nick, info in self.users.items():
            uh = parse_userhost(f"{info.get('username', '')}@{info.get('hostname', '')}")
            if uh == want:
                return nick
        return None

    def _ban_mask(self, nick: str) -> str:
        info = self.users.get(nick)
        if info and info.get("username") and info.get("hostname"):
            return f"{info['username']}@{info['hostname']}"
        return f"{nick}!*@*"

    # ---------- 主入口 ----------

    async def run(self) -> None:
        irc_cfg = self.cfg
        attempt = 0
        while True:
            try:
                await self.connect(
                    irc_cfg["host"],
                    port=int(irc_cfg.get("port", 6697)),
                    tls=bool(irc_cfg.get("tls", True)),
                    tls_verify=bool(irc_cfg.get("tls_verify", True)),
                )
                break
            except (ConnectionError, OSError) as e:
                delay = min(2**attempt, 60.0)
                self.logger.warning("IRC 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1
        await asyncio.Event().wait()
