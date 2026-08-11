"""server 中枢：监听/认证/命令路由/shellop 确认编排/runcmd 转发/权限持久化。"""

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
from .config import ConfigError, ConfigWatcher, load_yaml
from .permissions import Permissions
from .protocol import (
    decode_msg,
    encode_msg,
    make_command_result,
    make_getroot,
    make_permission_update,
    make_runcmd,
    make_shellop_proposal,
)
from .util import parse_userhost

log = logging.getLogger(__name__)

AUTH_TIMEOUT = 10.0
RUNCMD_TIMEOUT = 300.0


class ClientConnection:
    """一条已认证的 client 连接。"""

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
    """权威权限中枢。"""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)
        self.cfg: dict = {}
        self.permissions = Permissions()
        self.conns: dict[str, ClientConnection] = {}
        # userhost -> (is_oper, monotonic_ts)
        self.oper_cache: dict[str, tuple[bool, float]] = {}
        self.proposals: dict[str, dict] = {}
        # task_id -> (future, origin_conn, caller, target_client_name)
        self.runcmd_pending: dict[str, tuple[asyncio.Future, ClientConnection, str, str]] = {}
        self._persist_lock = asyncio.Lock()

    # ---------- 配置 ----------

    def reload(self, cfg: dict) -> None:
        if "server" not in cfg or "permissions" not in cfg:
            raise ConfigError("server.yaml 必须包含 server 与 permissions 段")
        self.permissions = Permissions.from_config(cfg)
        self.cfg = cfg
        asyncio.create_task(self._broadcast_permissions())

    def oper_cache_ttl(self) -> float:
        return float(self.cfg.get("server", {}).get("oper_cache_ttl", 60))

    def confirm_timeout(self) -> float:
        return float(self.cfg.get("server", {}).get("shellop_confirm_timeout", 60))

    # ---------- 主循环 ----------

    async def run(self) -> None:
        srv = self.cfg["server"]
        host = srv.get("listen_host", "0.0.0.0")
        port = int(srv.get("listen_port", 8443))
        tls_cfg = srv.get("tls", {})
        ssl_ctx = None
        if tls_cfg and tls_cfg.get("certfile"):
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            cfg_dir = self.config_path.parent
            certfile = tls_cfg["certfile"]
            keyfile = tls_cfg.get("keyfile")
            # 相对路径基于配置文件所在目录解析（避免 CWD 依赖）
            if not Path(certfile).is_absolute():
                certfile = str(cfg_dir / certfile)
            if keyfile and not Path(keyfile).is_absolute():
                keyfile = str(cfg_dir / keyfile)
            ssl_ctx.load_cert_chain(certfile, keyfile)
        server = await asyncio.start_server(self._handle_client, host, port, ssl=ssl_ctx)
        log.info("server 监听 %s:%s (TLS=%s)", host, port, ssl_ctx is not None)
        watcher = ConfigWatcher(self.config_path, self._on_config_change)
        try:
            await asyncio.gather(server.serve_forever(), watcher.run())
        finally:
            watcher.stop()

    async def _on_config_change(self, cfg: dict) -> None:
        try:
            self.reload(cfg)
        except ConfigError as e:
            log.error("热重载失败: %s", e)

    # ---------- 连接处理 ----------

    def _try_register(self, conn: ClientConnection) -> bool:
        """注册连接；重名返回 False（拒绝），防止旧 conn 断开时误删新连接。"""
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
            await conn.send(self._permission_update_msg())

            async for raw in reader:
                line = raw.decode("utf-8")
                try:
                    msg = decode_msg(line)
                except Exception as e:  # noqa: BLE001 - 协议错误不致命
                    log.warning("协议错误: %s", e)
                    continue
                await self._dispatch(conn, msg)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError, OSError):
            pass
        finally:
            if conn is not None:
                # H2: 仅当字典中的条目仍是本连接时才删除（防止覆盖后误删新连接）
                if self.conns.get(conn.client_name) is conn:
                    self.conns.pop(conn.client_name, None)
                self._drop_pending_for(conn)
                log.info("client 断开: %s", conn.client_name)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    # ---------- 分发 ----------

    async def _dispatch(self, conn: ClientConnection, msg: dict) -> None:
        t = msg.get("type")
        if t == "command":
            await self._handle_command(conn, msg)
        elif t == "vote":
            await self._handle_vote(conn, msg)
        elif t == "runcmd_result":
            await self._handle_runcmd_result(conn, msg)
        elif t == "status":
            pass
        else:
            log.warning("未知消息类型: %s", t)

    async def _handle_command(self, conn: ClientConnection, msg: dict) -> None:
        cmd = msg.get("cmd", "")
        args = msg.get("args", [])
        caller = msg.get("caller_userhost", "")
        is_oper = bool(msg.get("caller_is_oper", False))
        channel = msg.get("channel")
        uh = parse_userhost(caller)
        if uh:
            self.oper_cache[uh] = (is_oper, time.monotonic())
        required = COMMAND_LEVELS.get(cmd)
        if required is None:
            await self._reply(conn, False, f"未知命令: {cmd}", channel, caller)
            return
        if not self.permissions.has_level(uh, required, is_oper=is_oper):
            await self._reply(conn, False, "权限不足", channel, caller)
            return
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            await self._reply(conn, False, f"未知命令: {cmd}", channel, caller)
            return
        await handler(conn, args, uh, is_oper, channel)

    async def _reply(
        self,
        conn: ClientConnection,
        ok: bool,
        reply: str,
        channel: str | None,
        caller: str,
        action: str = "none",
        action_args: list[str] | None = None,
    ) -> None:
        target_type = "channel" if channel else "private"
        target = channel or caller
        await conn.send(make_command_result(ok, reply, target_type, target, action, action_args))

    # ---------- 命令处理器 ----------

    async def _cmd_info(self, conn, args, caller, is_oper, channel) -> None:
        clients = ", ".join(f"{n}({c.client_type})" for n, c in self.conns.items()) or "无"
        reply = (
            f"在线 client: {clients}\n"
            f"botop: {len(self.permissions.botop)} | shellop: {len(self.permissions.shellop)}\n"
            f"whitelist: {'开' if self.permissions.whitelist_enabled else '关'} "
            f"({len(self.permissions.whitelist)}条) | "
            f"blacklist: {'开' if self.permissions.blacklist_enabled else '关'} "
            f"({len(self.permissions.blacklist)}条)"
        )
        await self._reply(conn, True, reply, channel, caller)

    async def _cmd_botop(self, conn, args, caller, is_oper, channel) -> None:
        if len(args) != 2 or args[0] not in ("give", "remove"):
            await self._reply(conn, False, "用法: botop give/remove <user@host>", channel, caller)
            return
        target = parse_userhost(args[1])
        if args[0] == "give":
            self.permissions.botop.add(target)
            reply = f"已授予 botop: {target}"
        else:
            self.permissions.botop.discard(target)
            reply = f"已移除 botop: {target}"
        await self._persist_permissions()
        await self._reply(conn, True, reply, channel, caller)

    async def _cmd_shellop(self, conn, args, caller, is_oper, channel) -> None:
        if len(args) != 2 or args[0] not in ("add", "remove"):
            await self._reply(conn, False, "用法: shellop add/remove <user@host>", channel, caller)
            return
        target = parse_userhost(args[1])
        if args[0] == "remove":
            self.permissions.shellop.discard(target)
            await self._persist_permissions()
            await self._reply(conn, True, f"已移除 shellop: {target}", channel, caller)
            return
        # add → 确认流程
        if target not in self.permissions.botop:
            await self._reply(conn, False, f"候选人 {target} 不是 botop，无法提议为 shellop", channel, caller)
            return
        oper_info = self.oper_cache.get(target)
        oper_ttl = self.oper_cache_ttl()
        oper_valid = oper_info and oper_info[0] and (time.monotonic() - oper_info[1]) <= oper_ttl
        if not oper_valid:
            await self._reply(conn, False, f"候选人 {target} 未验证为 oper（需其 /oper 后触发机器人 WHOIS），无法提议", channel, caller)
            return
        if target in self.permissions.shellop:
            await self._reply(conn, False, f"{target} 已是 shellop", channel, caller)
            return
        proposal_id = uuid.uuid4().hex[:8]
        voters = self._collect_voters()
        voters.add(caller)  # 发起者必投
        deadline_ts = time.time() + self.confirm_timeout()
        prop = {
            "proposal_id": proposal_id,
            "candidate": target,
            "deadline": time.monotonic() + self.confirm_timeout(),
            "voters": voters,
            "votes": {caller: True},  # 发起者自动同意
            "origin_conn": conn,
            "origin_caller": caller,
            "origin_channel": channel,
            "task": None,
        }
        self.proposals[proposal_id] = prop
        prop["task"] = asyncio.create_task(self._proposal_timer(proposal_id))
        await self._broadcast_proposal(proposal_id, target, deadline_ts, sorted(voters))
        await self._reply(conn, True, f"shellop 提议 #{proposal_id} 已发送给 {len(voters)} 位在线 botop+oper 确认", channel, caller)

    def _collect_voters(self) -> set[str]:
        voters = set(self.permissions.botop)
        ttl = self.oper_cache_ttl()
        now = time.monotonic()
        for uh, (is_oper, ts) in self.oper_cache.items():
            if is_oper and now - ts <= ttl:
                voters.add(uh)
        return voters

    async def _broadcast_proposal(self, proposal_id, candidate, deadline_ts, voters) -> None:
        msg = make_shellop_proposal(proposal_id, candidate, deadline_ts, voters)
        for conn in list(self.conns.values()):
            try:
                await conn.send(msg)
            except (ConnectionError, OSError):
                pass

    async def _proposal_timer(self, proposal_id: str) -> None:
        prop = self.proposals.get(proposal_id)
        if prop is None:
            return
        wait = max(0.0, prop["deadline"] - time.monotonic())
        await asyncio.sleep(wait)
        if proposal_id in self.proposals:
            await self._finalize_proposal(proposal_id, success=False, reason="超时作废")

    async def _handle_vote(self, conn: ClientConnection, msg: dict) -> None:
        pid = msg.get("proposal_id", "")
        vote = msg.get("vote", "")
        voter = parse_userhost(msg.get("voter_userhost", ""))
        prop = self.proposals.get(pid)
        if prop is None:
            await self._reply(conn, False, f"提议 {pid} 不存在或已结束", None, voter)
            return
        if voter not in prop["voters"]:
            await self._reply(conn, False, "你不是该提议的投票人", None, voter)
            return
        if voter in prop["votes"]:
            await self._reply(conn, False, "你已投过票", None, voter)
            return
        if time.monotonic() > prop["deadline"]:
            await self._finalize_proposal(pid, success=False, reason="超时作废")
            await self._reply(conn, False, "提议已超时作废", None, voter)
            return
        if vote == "reject":
            prop["votes"][voter] = False
            await self._finalize_proposal(pid, success=False, reason=f"{voter} 否决")
            return
        if vote == "confirm":
            prop["votes"][voter] = True
            if len(prop["votes"]) >= len(prop["voters"]):
                await self._finalize_proposal(pid, success=True, reason="全员同意")
                return
            await self._reply(conn, True, f"已记录同意 ({len(prop['votes'])}/{len(prop['voters'])})", None, voter)
            return
        await self._reply(conn, False, "投票值必须是 confirm/reject", None, voter)

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
            await self._reply(prop["origin_conn"], success, reply, None, prop["origin_caller"])
        except (ConnectionError, OSError):
            pass

    async def _cmd_whitelist(self, conn, args, caller, is_oper, channel) -> None:
        await self._manage_list(conn, args, caller, channel, "whitelist")

    async def _cmd_blacklist(self, conn, args, caller, is_oper, channel) -> None:
        await self._manage_list(conn, args, caller, channel, "blacklist")

    async def _manage_list(self, conn, args, caller, channel, which: str) -> None:
        entries = self.permissions.whitelist if which == "whitelist" else self.permissions.blacklist
        other = "blacklist" if which == "whitelist" else "whitelist"
        if not args:
            await self._reply(conn, False, f"用法: {which} add/del/list/on/off [mask] [channel]", channel, caller)
            return
        sub = args[0]
        if sub == "list":
            if entries:
                lines = "\n".join(f"- {e.get('mask')} {e.get('channel', '(全局)')}" for e in entries)
                await self._reply(conn, True, f"{which} 条目:\n{lines}", channel, caller)
            else:
                await self._reply(conn, True, f"{which} 为空", channel, caller)
            return
        if sub == "on":
            if getattr(self.permissions, f"{other}_enabled"):
                await self._reply(conn, False, f"{other} 已启用，{which} 与 {other} 不能同时启用", channel, caller)
                return
            setattr(self.permissions, f"{which}_enabled", True)
            await self._persist_permissions()
            await self._reply(conn, True, f"{which} 已启用", channel, caller)
            return
        if sub == "off":
            setattr(self.permissions, f"{which}_enabled", False)
            await self._persist_permissions()
            await self._reply(conn, True, f"{which} 已禁用", channel, caller)
            return
        if sub in ("add", "del"):
            if len(args) < 2:
                await self._reply(conn, False, f"用法: {which} {sub} <mask> [channel]", channel, caller)
                return
            mask = args[1]
            ch = args[2] if len(args) > 2 else None
            if sub == "add":
                entry = {"mask": mask}
                if ch:
                    entry["channel"] = ch
                entries.append(entry)
                await self._persist_permissions()
                await self._reply(conn, True, f"已添加 {which} 条目: {mask} {ch or '(全局)'}", channel, caller)
            else:
                for i, e in enumerate(entries):
                    if e.get("mask") == mask and e.get("channel") == ch:
                        del entries[i]
                        await self._persist_permissions()
                        await self._reply(conn, True, f"已删除 {which} 条目: {mask}", channel, caller)
                        return
                await self._reply(conn, False, f"未找到条目: {mask}", channel, caller)
            return
        await self._reply(conn, False, f"未知子命令: {sub}", channel, caller)

    async def _cmd_ban(self, conn, args, caller, is_oper, channel) -> None:
        if not args:
            await self._reply(conn, False, "用法: ban <nick>", channel, caller)
            return
        await self._reply(conn, True, f"已批准封禁 {args[0]}", channel, caller, action="ban", action_args=[args[0]])

    async def _cmd_unban(self, conn, args, caller, is_oper, channel) -> None:
        if not args:
            await self._reply(conn, False, "用法: unban <nick>", channel, caller)
            return
        await self._reply(conn, True, f"已批准解封 {args[0]}", channel, caller, action="unban", action_args=[args[0]])

    async def _cmd_runcmd(self, conn, args, caller, is_oper, channel) -> None:
        if not args:
            await self._reply(conn, False, "用法: runcmd <client_name> <cmd...>", channel, caller)
            return
        if args[0] == "getroot":
            # getroot 子命令：目标 = 调用者所在 client（发起 conn 自身）
            if len(args) < 2 or not args[1]:
                await self._reply(conn, False, "用法: runcmd getroot <client_root_password>", channel, caller)
                return
            password = args[1]
            task_id = uuid.uuid4().hex
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.runcmd_pending[task_id] = (fut, conn, caller, conn.client_name)
            try:
                await conn.send(make_getroot(task_id, password, caller))
            except (ConnectionError, OSError):
                self.runcmd_pending.pop(task_id, None)
                await self._reply(conn, False, "client 连接异常", channel, caller)
                return
            await self._reply(conn, True, "getroot 已发送到本 client，等待验证", channel, caller)
            try:
                ok, output = await asyncio.wait_for(fut, timeout=RUNCMD_TIMEOUT)
            except asyncio.TimeoutError:
                self.runcmd_pending.pop(task_id, None)
                await self._reply(conn, False, "getroot 验证超时", None, caller)
                return
            text = output if output else ("root 已激活" if ok else "root 验证失败")
            await conn.send(make_command_result(ok, text, "private", caller))
            return
        target_name = args[0]
        cmd = " ".join(args[1:])
        target = self.conns.get(target_name)
        if target is None:
            await self._reply(conn, False, f"client {target_name} 不在线", channel, caller)
            return
        task_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.runcmd_pending[task_id] = (fut, conn, caller, target_name)
        try:
            await target.send(make_runcmd(task_id, cmd, caller_userhost=caller))
        except (ConnectionError, OSError):
            self.runcmd_pending.pop(task_id, None)
            await self._reply(conn, False, f"client {target_name} 连接异常", channel, caller)
            return
        await self._reply(conn, True, f"runcmd 已发送到 {target_name}", channel, caller)
        try:
            ok, output = await asyncio.wait_for(fut, timeout=RUNCMD_TIMEOUT)
        except asyncio.TimeoutError:
            self.runcmd_pending.pop(task_id, None)
            await self._reply(conn, False, f"runcmd 在 {target_name} 超时（{int(RUNCMD_TIMEOUT)}s）", None, caller)
            return
        if not ok:
            text = f"命令执行失败:\n{output}" if output else "命令执行失败"
        else:
            text = output if output else "命令执行成功，无输出"
        await conn.send(make_command_result(ok, text, "private", caller))

    async def _handle_runcmd_result(self, conn: ClientConnection, msg: dict) -> None:
        tid = msg.get("task_id", "")
        entry = self.runcmd_pending.get(tid)
        if entry is None:
            log.warning("未知 runcmd task: %s", tid)
            return
        # M4: 校验结果发送者——只有该任务的目标 client 才能 resolve
        fut, origin_conn, caller, target_name = entry
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
        """连接断开时清理其发起的未完成 runcmd/getroot 请求。"""
        for tid, (fut, origin_conn, _caller, _target) in list(self.runcmd_pending.items()):
            if origin_conn is conn:
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
        await self._broadcast_permissions()

    def _oper_cache_snapshot(self) -> dict:
        ttl = self.oper_cache_ttl()
        now = time.monotonic()
        return {uh: is_oper for uh, (is_oper, ts) in self.oper_cache.items() if now - ts <= ttl}

    def _permission_update_msg(self) -> dict:
        return make_permission_update(self.permissions.snapshot(), self._oper_cache_snapshot())

    async def _broadcast_permissions(self) -> None:
        msg = self._permission_update_msg()
        for conn in list(self.conns.values()):
            try:
                await conn.send(msg)
            except (ConnectionError, OSError):
                pass
