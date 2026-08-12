"""client 与 server 的共享连接层：TLS 连接、消息分发、getroot/root 会话。"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from .protocol import (
    decode_msg,
    encode_msg,
    make_auth,
    make_runcmd_result,
)
from .util import backoff_delay, parse_userhost, tls_enabled

log = logging.getLogger(__name__)

# userhost -> (root 密码, 过期 monotonic 时间)；仅存内存
RootSession = tuple[str, float]


def build_client_ssl_ctx(tls_cfg: dict | None) -> ssl.SSLContext | None:
    """构建 client 端 SSLContext；TLS 未启用返回 None。"""
    if not tls_enabled(tls_cfg):
        return None
    ctx = ssl.create_default_context()
    verify = tls_cfg.get("verify", True)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if tls_cfg.get("ca_cert"):
        ctx.load_verify_locations(tls_cfg["ca_cert"])
    return ctx


class ServerLink:
    """与 server 的 TLS JSON-lines 连接层 + getroot/root 会话管理。

    仅处理执行类消息（runcmd/getroot/auth_ok），client 为纯执行器。
    """

    def __init__(self, cfg: dict, client_type: str, bot_name: str) -> None:
        self.cfg = cfg
        self.client_type = client_type
        self.bot_name = bot_name
        self.root_sessions: dict[str, RootSession] = {}
        self._server_writer: asyncio.StreamWriter | None = None
        self._server_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动连接循环任务（幂等）。"""
        if self._server_task is None or self._server_task.done():
            self._server_task = asyncio.create_task(self._server_loop())

    # ---------- 连接 ----------

    async def _server_loop(self) -> None:
        srv_cfg = self.cfg["client"]["server"]
        attempt = 0
        while True:
            try:
                await self._server_connect(srv_cfg)
                attempt = 0
            except ssl.SSLError as e:
                log.warning("TLS 握手失败（server 可能未启用 TLS，或两端 tls 配置不匹配）: %s", e)
                delay = backoff_delay(attempt)
                await asyncio.sleep(delay)
                attempt += 1
            except (ConnectionError, OSError, asyncio.IncompleteReadError, TimeoutError) as e:
                delay = backoff_delay(attempt)
                log.warning("server 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1

    async def _server_connect(self, srv_cfg: dict) -> None:
        ssl_ctx = build_client_ssl_ctx(srv_cfg.get("tls"))
        reader, writer = await asyncio.open_connection(
            srv_cfg["host"], int(srv_cfg["port"]), ssl=ssl_ctx
        )
        self._server_writer = writer
        client_cfg = self.cfg["client"]
        await self.send(make_auth(
            srv_cfg["token"], client_cfg["name"], self.client_type, self.bot_name
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

    async def send(self, msg: dict) -> None:
        if self._server_writer is None:
            raise ConnectionError("未连接 server")
        self._server_writer.write(encode_msg(msg))
        await self._server_writer.drain()

    async def _on_server_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "runcmd":
            await self.exec_runcmd(msg)
        elif t == "getroot":
            await self.exec_getroot(msg)
        elif t == "auth_ok":
            if not msg.get("ok"):
                log.error("server 认证失败，请检查 token")

    # ---------- root 会话 ----------

    def _root_session_ttl(self) -> float:
        return float(self.cfg.get("root_session_ttl", 300))

    def _root_session_lookup(self, caller: str) -> tuple[RootSession | None, bool]:
        """查找会话；返回 (条目, 是否曾有过但已过期)。清理过期条目防泄漏。"""
        now = time.monotonic()
        expired_prev = False
        entry = self.root_sessions.get(caller)
        if entry is not None and entry[1] <= now:
            expired_prev = True
            del self.root_sessions[caller]
            entry = None
        return entry, expired_prev

    async def exec_getroot(self, msg: dict) -> None:
        task_id = msg["task_id"]
        password = msg["password"]
        caller = parse_userhost(msg.get("caller_userhost", ""))
        log.info("getroot 验证请求（user=%s）", caller)
        proc = await asyncio.create_subprocess_exec(
            "su", "--pty", "root", "-c", "id -u",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate(input=(password + "\n").encode("utf-8"))
        ok = proc.returncode == 0 and stdout.strip() == b"0"
        if ok and caller:
            self._root_session_lookup(caller)[0]  # 清理过期条目
            self.root_sessions[caller] = (password, time.monotonic() + self._root_session_ttl())
            output = f"root 已激活（{int(self._root_session_ttl())} 秒有效）"
            log.info("getroot 成功: %s", caller)
        else:
            output = "root 密码验证失败"
            log.warning("getroot 失败: %s", caller)
        await self.send(make_runcmd_result(task_id, ok, output, as_root=ok))

    async def exec_runcmd(self, msg: dict) -> None:
        task_id = msg["task_id"]
        cmd = msg["cmd"]
        caller = parse_userhost(msg.get("caller_userhost", ""))
        entry, root_expired = self._root_session_lookup(caller)
        as_root = False
        if entry:
            as_root = True
            password = entry[0]
            log.info("执行 runcmd (root=%s): %s", as_root, cmd)
            proc = await asyncio.create_subprocess_exec(
                "su", "--pty", "root", "-c", cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate(input=(password + "\n").encode("utf-8"))
        else:
            log.info("执行 runcmd (root=%s): %s", as_root, cmd)
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        await self.send(make_runcmd_result(
            task_id, proc.returncode == 0, output, as_root=as_root, root_expired=root_expired
        ))
