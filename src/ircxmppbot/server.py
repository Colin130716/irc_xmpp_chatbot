"""server 中枢：连接 IM（IRC/XMPP）+ 命令处理 + shellop 确认编排 + runcmd 转发 + 权限持久化。"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
import uuid
from pathlib import Path

import yaml

from .commands import COMMAND_LEVELS
from .config import ConfigError, ConfigWatcher
from .irc_session import IRCSession
from .llm import LLMClient
from .permissions import Permissions
from .protocol import (
    decode_msg,
    encode_msg,
    make_getroot,
    make_runcmd,
)
from .util import parse_userhost, tls_enabled
from .xmpp_session import XMPPSession

log = logging.getLogger(__name__)

AUTH_TIMEOUT = 10.0
RUNCMD_TIMEOUT = 300.0


def build_server_ssl_ctx(tls_cfg: dict | None, config_dir: Path) -> ssl.SSLContext | None:
    """构建 server 端 SSLContext；TLS 未启用返回 None。启用但缺 certfile 抛 ConfigError。"""
    if not tls_enabled(tls_cfg):
        return None
    if not tls_cfg.get("certfile"):
        raise ConfigError("server tls 已启用但缺少 certfile")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    certfile = tls_cfg["certfile"]
    keyfile = tls_cfg.get("keyfile")
    if not Path(certfile).is_absolute():
        certfile = str(config_dir / certfile)
    if keyfile and not Path(keyfile).is_absolute():
        keyfile = str(config_dir / keyfile)
    ctx.load_cert_chain(certfile, keyfile)
    return ctx


class ChatTarget:
    """命令回复目标抽象：IRCSession/XMPPSession 实现。"""

    session_type = ""

    async def reply(self, text: str, channel: str | None, private_to: str | None = None) -> None:
        raise NotImplementedError

    async def execute(self, action: str, action_args: list[str], channel: str | None) -> None:
        raise NotImplementedError

    async def dm_voters(self, proposal_id, candidate, voters, deadline_ts) -> list[str]:
        return []


class ClientConnection:
    """一条已认证的 client（shell 执行器）连接。"""

    def __init__(self, reader, writer, client_name: str, client_type: str, bot_name: str) -> None:
        self.reader = reader
        self.writer = writer
        self.client_name = client_name
        self.client_type = client_type
        self.bot_name = bot_name

    async def send(self, msg: dict) -> None:
        self.writer.write(encode_msg(msg))
        await self.writer.drain()


class BotServer:
    """权威权限中枢 + IM 会话宿主。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg: dict = {}
        self.permissions = Permissions()
        self.conns: dict[str, ClientConnection] = {}
        # userhost -> (is_oper, monotonic_ts)
        self.oper_cache: dict[str, tuple[bool, float]] = {}
        self.proposals: dict[str, dict] = {}
        # task_id -> (future, origin_target, caller, target_client_name)
        self.runcmd_pending: dict[str, tuple[asyncio.Future, ChatTarget, str, str]] = {}
        self._persist_lock = asyncio.Lock()
        self.llm: LLMClient | None = None
        self.irc_session: IRCSession | None = None
        self.xmpp_session: XMPPSession | None = None

    # ---------- 配置 ----------

    def reload(self, cfg: dict) -> None:
        if "server" not in cfg or "permissions" not in cfg:
            raise ConfigError("server.yaml 必须包含 server 与 permissions 段")
        self.permissions = Permissions.from_config(cfg)
        self.llm = LLMClient(cfg.get("llm", {}))
        self.cfg = cfg

    def oper_cache_ttl(self) -> float:
        return float(self.cfg.get("server", {}).get("oper_cache_ttl", 60))

    def confirm_timeout(self) -> float:
        return float(self.cfg.get("server", {}).get("shellop_confirm_timeout", 60))

    # ---------- 主循环 ----------

    async def run(self) -> None:
        srv = self.cfg["server"]
        host = srv.get("listen_host", "0.0.0.0")
        port = int(srv.get("listen_port", 8443))
        ssl_ctx = build_server_ssl_ctx(srv.get("tls"), self.config_path.parent)
        server = await asyncio.start_server(self._handle_client, host, port, ssl=ssl_ctx)
        log.info("server 监听 %s:%s (TLS=%s)", host, port, ssl_ctx is not None)
        watcher = ConfigWatcher(self.config_path, self._on_config_change)
        tasks = [asyncio.create_task(server.serve_forever()), asyncio.create_task(watcher.run())]
        if self.cfg.get("irc"):
            self.irc_session = IRCSession(self, self.cfg["irc"])
            tasks.append(asyncio.create_task(self.irc_session.run()))
        if self.cfg.get("xmpp"):
            self.xmpp_session = XMPPSession(self, self.cfg["xmpp"])
            tasks.append(asyncio.create_task(self.xmpp_session.run()))
        try:
            await asyncio.gather(*tasks)
        finally:
            watcher.stop()

    async def _on_config_change(self, cfg: dict) -> None:
        try:
            self.reload(cfg)
        except ConfigError as e:
            log.error("热重载失败: %s", e)

    # ---------- 连接处理（shell 执行器） ----------

    def _try_register(self, conn: ClientConnection) -> bool:
        if conn.client_name in self.conns:
            log.warning("client 重名注册被拒: %s", conn.client_name)
            return False
        self.conns[conn.client_name] = conn
        log.info("client 注册: %s (%s)", conn.client_name, conn.client_type)
        return True

    async def _handle_client(self, reader, writer) -> None:
        conn: ClientConnection | None = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=AUTH_TIMEOUT)
            if not line:
                return
            msg = decode_msg(line.decode("utf-8"))
            if msg.get("type") != "auth":
                log.warning("首个消息非 auth")
                return
            token = self.cfg.get("server", {}).get("token", "")
            if msg.get("token") != token:
                log.warning("认证失败: %s", msg.get("client_name"))
                writer.write(encode_msg({"type": "auth_ok", "ok": False}))
                await writer.drain()
                return
            conn = ClientConnection(
                reader, writer,
                msg["client_name"], msg.get("client_type", ""), msg.get("bot_name", ""),
            )
            if not self._try_register(conn):
                writer.write(encode_msg({"type": "auth_ok", "ok": False}))
                await writer.drain()
                return
            await conn.send({"type": "auth_ok", "ok": True})

            async for raw in reader:
                line = raw.decode("utf-8")
                try:
                    msg = decode_msg(line)
                except Exception as e:  # noqa: BLE001
                    log.warning("协议错误: %s", e)
                    continue
                await self._dispatch(conn, msg)
        except ssl.SSLError as e:
            log.warning("TLS 握手失败（client 可能未启用 TLS，请检查两端 tls.enabled 配置是否一致）: %s", e)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
        finally:
            if conn is not None:
                if self.conns.get(conn.client_name) is conn:
                    self.conns.pop(conn.client_name, None)
                self._drop_pending_for(conn)
                log.info("client 断开: %s", conn.client_name)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _dispatch(self, conn: ClientConnection, msg: dict) -> None:
        t = msg.get("type")
        if t == "runcmd_result":
            await self._handle_runcmd_result(conn, msg)
        elif t == "status":
            pass
        else:
            log.warning("未知消息类型: %s", t)

    # ---------- 命令入口（来自 IRC/XMPP 会话） ----------

    async def handle_chat_command(self, cmd, args, caller_userhost, is_oper, channel, target) -> None:
        if target.session_type == "xmpp" and cmd not in ("chat", "help"):
            await target.reply("XMPP 端仅支持 chat 命令", channel, None)
            return
        uh = parse_userhost(caller_userhost)
        # L3: 仅 IRC 会话写入 oper_cache（XMPP jid 无 oper 概念，避免污染）
        if uh and target.session_type == "irc":
            self.oper_cache[uh] = (is_oper, time.monotonic())
        required = COMMAND_LEVELS.get(cmd)
        if required is None:
            await target.reply(f"未知命令: {cmd}", channel, caller_userhost)
            return
        if not self.permissions.has_level(uh, required, is_oper=is_oper):
            await target.reply("权限不足", channel, caller_userhost)
            return
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            await target.reply(f"未知命令: {cmd}", channel, caller_userhost)
            return
        await handler(target, args, uh, is_oper, channel)

    async def handle_join(self, target: ChatTarget, channel, nick) -> None:
        p = self.permissions
        if not p.whitelist_enabled and not p.blacklist_enabled:
            return
        info = target.users.get(nick)
        if info:
            hostmask = f"{nick}!{info.get('username', '*')}@{info.get('hostname', '*')}"
        else:
            hostmask = f"{nick}!*@*"
        uh = parse_userhost(hostmask)
        if p.whitelist_enabled:
            if p.whitelist_allows(hostmask, channel):
                return
            if p.is_botop(uh) or target.oper_cache.get(uh, False):
                return
            await target.kick(channel, nick, "白名单模式")
            return
        if p.blacklist_blocks(hostmask, channel):
            await target.kick(channel, nick, "黑名单")

    # ---------- 命令处理器 ----------

    async def _cmd_chat(self, target, args, caller, is_oper, channel) -> None:
        text = " ".join(args)
        if not text:
            await target.reply("用法: chat <文本>", channel, caller)
            return
        try:
            reply = await self.llm.chat(text)
        except Exception as e:  # noqa: BLE001 - LLM 错误不致命
            reply = f"LLM 错误: {e}"
        await target.reply(reply, channel, caller if channel is None else None)

    async def _cmd_help(self, target, args, caller, is_oper, channel) -> None:
        await target.reply(
            "命令: chat/help/ban/unban/whitelist/blacklist/botop/shellop/confirm/reject/runcmd/info",
            channel, caller,
        )

    async def _cmd_info(self, target, args, caller, is_oper, channel) -> None:
        clients = ", ".join(f"{n}({c.client_type})" for n, c in self.conns.items()) or "无"
        reply = (
            f"在线 client: {clients}\n"
            f"botop: {len(self.permissions.botop)} | shellop: {len(self.permissions.shellop)}\n"
            f"whitelist: {'开' if self.permissions.whitelist_enabled else '关'} "
            f"({len(self.permissions.whitelist)}条) | "
            f"blacklist: {'开' if self.permissions.blacklist_enabled else '关'} "
            f"({len(self.permissions.blacklist)}条)"
        )
        await target.reply(reply, channel, caller if channel is None else None)

    async def _cmd_botop(self, target, args, caller, is_oper, channel) -> None:
        if len(args) != 2 or args[0] not in ("give", "remove"):
            await target.reply("用法: botop give/remove <user@host>", channel, caller)
            return
        uh = parse_userhost(args[1])
        if args[0] == "give":
            self.permissions.botop.add(uh)
            reply = f"已授予 botop: {uh}"
        else:
            self.permissions.botop.discard(uh)
            reply = f"已移除 botop: {uh}"
        await self._persist_permissions()
        await target.reply(reply, channel, caller if channel is None else None)

    async def _cmd_shellop(self, target, args, caller, is_oper, channel) -> None:
        if len(args) != 2 or args[0] not in ("add", "remove"):
            await target.reply("用法: shellop add/remove <user@host>", channel, caller)
            return
        uh = parse_userhost(args[1])
        if args[0] == "remove":
            self.permissions.shellop.discard(uh)
            await self._persist_permissions()
            await target.reply(f"已移除 shellop: {uh}", channel, caller if channel is None else None)
            return
        if uh not in self.permissions.botop:
            await target.reply(f"候选人 {uh} 不是 botop，无法提议为 shellop", channel, caller)
            return
        oper_info = self.oper_cache.get(uh)
        oper_ttl = self.oper_cache_ttl()
        oper_valid = oper_info and oper_info[0] and (time.monotonic() - oper_info[1]) <= oper_ttl
        if not oper_valid:
            await target.reply(f"候选人 {uh} 未验证为 oper（需其 /oper 后触发机器人 WHOIS），无法提议", channel, caller)
            return
        if uh in self.permissions.shellop:
            await target.reply(f"{uh} 已是 shellop", channel, caller)
            return
        proposal_id = uuid.uuid4().hex[:8]
        voters = self._collect_voters()
        voters.add(caller)  # 发起者必投
        deadline_ts = time.time() + self.confirm_timeout()
        prop = {
            "proposal_id": proposal_id,
            "candidate": uh,
            "deadline": time.monotonic() + self.confirm_timeout(),
            "voters": voters,
            "votes": {caller: True},  # 发起者自动同意
            "origin_target": target,
            "origin_caller": caller,
            "origin_channel": channel,
            "task": None,
        }
        self.proposals[proposal_id] = prop
        prop["task"] = asyncio.create_task(self._proposal_timer(proposal_id))
        # 收集可达投票人（dm_voters 取代 reachability 消息）
        try:
            reachable = await target.dm_voters(proposal_id, uh, sorted(voters), deadline_ts)
        except (ConnectionError, OSError):
            # M2: 会话异常时回退为仅发起者可投票（避免提议卡死）
            reachable = [caller]
        prop["voters"] = set(reachable) | {caller}
        # 仅发起者一人可触达 → 直接通过
        if len(prop["voters"]) <= 1:
            await self._finalize_proposal(proposal_id, success=True, reason="仅发起者一人可触达，全员同意")
            return
        await target.reply(
            f"shellop 提议 #{proposal_id} 已通知 {len(prop['voters'])} 位可触达投票人确认",
            channel, caller if channel is None else None,
        )

    def _collect_voters(self) -> set[str]:
        voters = set(self.permissions.botop)
        ttl = self.oper_cache_ttl()
        now = time.monotonic()
        for uh, (is_oper, ts) in self.oper_cache.items():
            if is_oper and now - ts <= ttl:
                voters.add(uh)
        return voters

    async def _proposal_timer(self, proposal_id: str) -> None:
        prop = self.proposals.get(proposal_id)
        if prop is None:
            return
        wait = max(0.0, prop["deadline"] - time.monotonic())
        await asyncio.sleep(wait)
        if proposal_id in self.proposals:
            await self._finalize_proposal(proposal_id, success=False, reason="超时作废")

    async def _cmd_confirm(self, target, args, caller, is_oper, channel) -> None:
        await self._cmd_vote(target, args, caller, "confirm")

    async def _cmd_reject(self, target, args, caller, is_oper, channel) -> None:
        await self._cmd_vote(target, args, caller, "reject")

    async def _cmd_vote(self, target, args, caller, vote: str) -> None:
        if not args:
            await target.reply(f"用法: {vote} <提议ID>", None, caller)
            return
        proposal_id = args[0]
        prop = self.proposals.get(proposal_id)
        if prop is None:
            await target.reply(f"提议 {proposal_id} 不存在或已结束", None, caller)
            return
        # 校验投票人资格：必须在该提议的 voters 集内
        if caller not in prop["voters"]:
            await target.reply("你不是该提议的投票人", None, caller)
            return
        if caller in prop["votes"]:
            await target.reply("你已投过票", None, caller)
            return
        await self._handle_vote(caller, vote, proposal_id)
        prop = self.proposals.get(proposal_id)
        if prop is not None:
            await target.reply(f"已记录{vote}（{len(prop['votes'])}/{len(prop['voters'])}）", None, caller)

    async def _handle_vote(self, voter: str, vote: str, proposal_id: str) -> None:
        prop = self.proposals.get(proposal_id)
        if prop is None:
            return
        if time.monotonic() > prop["deadline"]:
            await self._finalize_proposal(proposal_id, success=False, reason="超时作废")
            return
        if voter not in prop["voters"]:
            return
        if vote == "reject":
            prop["votes"][voter] = False
            await self._finalize_proposal(proposal_id, success=False, reason=f"{voter} 否决")
            return
        if vote == "confirm":
            prop["votes"][voter] = True
            if len(prop["votes"]) >= len(prop["voters"]):
                await self._finalize_proposal(proposal_id, success=True, reason="全员同意")

    async def _finalize_proposal(self, pid: str, success: bool, reason: str) -> None:
        prop = self.proposals.pop(pid, None)
        if prop is None:
            return
        if prop.get("task"):
            prop["task"].cancel()
            try:
                await prop["task"]
            except asyncio.CancelledError:
                pass
        if success:
            self.permissions.shellop.add(prop["candidate"])
            await self._persist_permissions()
            reply = f"提议 #{pid} 通过：{prop['candidate']} 已成为 shellop"
        else:
            reply = f"提议 #{pid} 未通过：{reason}"
        try:
            await prop["origin_target"].reply(reply, None, prop["origin_caller"])
        except (ConnectionError, OSError):
            pass

    async def _cmd_whitelist(self, target, args, caller, is_oper, channel) -> None:
        await self._manage_list(target, args, caller, channel, "whitelist")

    async def _cmd_blacklist(self, target, args, caller, is_oper, channel) -> None:
        await self._manage_list(target, args, caller, channel, "blacklist")

    async def _manage_list(self, target, args, caller, channel, which: str) -> None:
        entries = self.permissions.whitelist if which == "whitelist" else self.permissions.blacklist
        other = "blacklist" if which == "whitelist" else "whitelist"
        if not args:
            await target.reply(f"用法: {which} add/del/list/on/off [mask] [channel]", channel, caller)
            return
        sub = args[0]
        if sub == "list":
            if entries:
                lines = "\n".join(f"- {e.get('mask')} {e.get('channel', '(全局)')}" for e in entries)
                await target.reply(f"{which} 条目:\n{lines}", channel, caller if channel is None else None)
            else:
                await target.reply(f"{which} 为空", channel, caller if channel is None else None)
            return
        if sub == "on":
            if getattr(self.permissions, f"{other}_enabled"):
                await target.reply(f"{other} 已启用，{which} 与 {other} 不能同时启用", channel, caller)
                return
            setattr(self.permissions, f"{which}_enabled", True)
            await self._persist_permissions()
            await target.reply(f"{which} 已启用", channel, caller if channel is None else None)
            return
        if sub == "off":
            setattr(self.permissions, f"{which}_enabled", False)
            await self._persist_permissions()
            await target.reply(f"{which} 已禁用", channel, caller if channel is None else None)
            return
        if sub in ("add", "del"):
            if len(args) < 2:
                await target.reply(f"用法: {which} {sub} <mask> [channel]", channel, caller)
                return
            mask = args[1]
            ch = args[2] if len(args) > 2 else None
            if sub == "add":
                entry = {"mask": mask}
                if ch:
                    entry["channel"] = ch
                entries.append(entry)
                await self._persist_permissions()
                await target.reply(f"已添加 {which} 条目: {mask} {ch or '(全局)'}", channel, caller if channel is None else None)
            else:
                for i, e in enumerate(entries):
                    if e.get("mask") == mask and e.get("channel") == ch:
                        del entries[i]
                        await self._persist_permissions()
                        await target.reply(f"已删除 {which} 条目: {mask}", channel, caller if channel is None else None)
                        return
                await target.reply(f"未找到条目: {mask}", channel, caller)
            return
        await target.reply(f"未知子命令: {sub}", channel, caller)

    async def _cmd_ban(self, target, args, caller, is_oper, channel) -> None:
        if not args:
            await target.reply("用法: ban <nick>", channel, caller)
            return
        await target.reply(f"已批准封禁 {args[0]}", channel, caller if channel is None else None)
        await target.execute("ban", [args[0]], channel)

    async def _cmd_unban(self, target, args, caller, is_oper, channel) -> None:
        if not args:
            await target.reply("用法: unban <nick>", channel, caller)
            return
        await target.reply(f"已批准解封 {args[0]}", channel, caller if channel is None else None)
        await target.execute("unban", [args[0]], channel)

    async def _cmd_runcmd(self, target, args, caller, is_oper, channel) -> None:
        if not args:
            await target.reply("用法: runcmd <client_name> <cmd...>", channel, caller)
            return
        if args[0] == "getroot":
            if len(args) < 3 or not args[2]:
                await target.reply("用法: runcmd getroot <client_name> <client_root_password>", channel, caller)
                return
            target_name = args[1]
            password = args[2]
            conn = self.conns.get(target_name)
            if conn is None:
                await target.reply(f"client {target_name} 不在线", channel, caller)
                return
            task_id = uuid.uuid4().hex
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.runcmd_pending[task_id] = (fut, target, caller, target_name)
            try:
                await conn.send(make_getroot(task_id, password, caller))
            except (ConnectionError, OSError):
                self.runcmd_pending.pop(task_id, None)
                await target.reply(f"client {target_name} 连接异常", channel, caller)
                return
            try:
                ok, output = await asyncio.wait_for(fut, timeout=RUNCMD_TIMEOUT)
            except asyncio.TimeoutError:
                self.runcmd_pending.pop(task_id, None)
                await target.reply("getroot 验证超时", None, caller)
                return
            text = output if output else ("root 已激活" if ok else "root 验证失败")
            await target.reply(text, None, caller)
            return
        target_name = args[0]
        cmd = " ".join(args[1:])
        conn = self.conns.get(target_name)
        if conn is None:
            await target.reply(f"client {target_name} 不在线", channel, caller)
            return
        task_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.runcmd_pending[task_id] = (fut, target, caller, target_name)
        try:
            await conn.send(make_runcmd(task_id, cmd, caller_userhost=caller))
        except (ConnectionError, OSError):
            self.runcmd_pending.pop(task_id, None)
            await target.reply(f"client {target_name} 连接异常", channel, caller)
            return
        try:
            ok, output = await asyncio.wait_for(fut, timeout=RUNCMD_TIMEOUT)
        except asyncio.TimeoutError:
            self.runcmd_pending.pop(task_id, None)
            await target.reply(f"runcmd 在 {target_name} 超时（{int(RUNCMD_TIMEOUT)}s）", None, caller)
            return
        if not ok:
            text = f"命令执行失败:\n{output}" if output else "命令执行失败"
        else:
            text = output if output else "命令执行成功，无输出"
        await target.reply(text, None, caller)

    async def _handle_runcmd_result(self, conn: ClientConnection, msg: dict) -> None:
        tid = msg.get("task_id", "")
        entry = self.runcmd_pending.get(tid)
        if entry is None:
            log.warning("未知 runcmd task: %s", tid)
            return
        fut, origin_target, caller, target_name = entry
        if conn.client_name != target_name:
            log.warning("runcmd_result 来源不符，拒绝: task=%s from=%s target=%s", tid, conn.client_name, target_name)
            return
        self.runcmd_pending.pop(tid, None)
        if not fut.done():
            output = msg.get("output", "")
            if msg.get("root_expired"):
                output = "[root 会话已过期，已按普通用户执行，请重新 getroot]\n" + output
            fut.set_result((bool(msg.get("ok")), output))

    def _drop_pending_for(self, conn: ClientConnection) -> None:
        for tid, (fut, _origin, _caller, target_name) in list(self.runcmd_pending.items()):
            if target_name == conn.client_name:
                self.runcmd_pending.pop(tid, None)
                if not fut.done():
                    fut.cancel()

    # ---------- 持久化与广播 ----------

    async def _persist_permissions(self) -> None:
        async with self._persist_lock:
            self.cfg["permissions"] = self.permissions.to_config_dict()
            tmp = self.config_path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(self.cfg, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp, self.config_path)

    # 协议精简后无 permission_update 消息；权限数据 server 本地持有，无需广播
