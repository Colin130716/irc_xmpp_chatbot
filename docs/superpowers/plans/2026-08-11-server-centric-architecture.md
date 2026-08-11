# Server 中枢化架构调整实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将架构调整为 server 连接 IRC/XMPP 并处理一切命令，client 退化为统一单类型的纯 shell 执行器；配置集中到 server.yaml。

**Architecture:** server.py 扩展为中枢（内嵌 IRCSession/XMPPSession 处理命令与 LLM），现有 `_cmd_*` 处理器签名从 `conn` 改为 `ChatTarget` 抽象（session 实现 reply/execute）；server_link.py 精简为 client 端执行器；protocol 删除命令上报类消息。

**Tech Stack:** Python 3.14、asyncio、pydle（IRC session）、slixmpp（XMPP session）、PyYAML、httpx、pytest+pytest-asyncio

## Global Constraints

- 所有代码位于 `src/ircxmppbot/` 包内；测试位于 `tests/`
- 删除 `irc_client.py`/`xmpp_client.py`；新增 `irc_session.py`/`xmpp_session.py`/`client.py`
- server.yaml 为唯一配置：`irc:`/`xmpp:`/`llm:`/`permissions:` 段；`irc`/`xmpp` 段可选
- 协议精简后仅 6 种消息：auth/auth_ok/runcmd/runcmd_result/getroot/status
- XMPP 会话仅放行 chat/help 命令；XMPP 用户 userhost 用 jid 映射，is_oper 恒 False
- `_cmd_*` 处理器签名改为 `(self, target, args, caller, is_oper, channel)`，target 为 `ChatTarget`
- `chat` 命令在 server 端新增 `_cmd_chat`（调 `self.llm.chat`）
- shellop 投票内部化：`IRCSession.dm_voters()` 取代 `reachability` 消息
- runcmd 权限判定仍在 server；getroot 密码验证仍在 client（ServerLink）
- 测试不依赖真实 IRC/XMPP（mock pydle/slixmpp/transport）

---

### Task 1: protocol.py — 删除命令上报类消息

**Files:**
- Modify: `src/ircxmppbot/protocol.py`
- Modify: `tests/test_protocol.py`

**Interfaces:**
- Consumes: 无
- Produces: `VALID_TYPES` 仅含 `{"auth","auth_ok","runcmd","runcmd_result","getroot","status"}`；保留 `make_auth/make_runcmd/make_runcmd_result/make_getroot`；删除 `make_command/make_command_result/make_permission_update/make_shellop_proposal/make_vote/make_reachability`

- [ ] **Step 1: 更新测试（删 6 构造器断言，保留 4 个）**

`tests/test_protocol.py` 全文替换：

```python
import pytest

from ircxmppbot.protocol import (
    ProtocolError,
    decode_msg,
    encode_msg,
    make_auth,
    make_getroot,
    make_runcmd,
    make_runcmd_result,
)


def test_encode_decode_roundtrip_unicode():
    msg = {"type": "auth", "token": "密钥🔑", "client_name": "irc_main"}
    line = encode_msg(msg).decode("utf-8")
    assert line.endswith("\n")
    assert decode_msg(line) == msg


def test_encode_missing_type():
    with pytest.raises(ProtocolError):
        encode_msg({"token": "x"})


def test_encode_unknown_type():
    with pytest.raises(ProtocolError):
        encode_msg({"type": "nope"})


def test_decode_empty_line():
    with pytest.raises(ProtocolError):
        decode_msg("   \n")


def test_decode_bad_json():
    with pytest.raises(ProtocolError):
        decode_msg("{not json")


def test_decode_unknown_type():
    with pytest.raises(ProtocolError):
        decode_msg('{"type": "wat"}')


def test_make_auth_shape():
    assert make_auth("t", "n", "irc", "bot") == {
        "type": "auth", "token": "t", "client_name": "n",
        "client_type": "irc", "bot_name": "bot",
    }


def test_make_runcmd_shape():
    assert make_runcmd("t1", "ls -la", "u@h") == {
        "type": "runcmd", "task_id": "t1", "cmd": "ls -la", "caller_userhost": "u@h",
    }


def test_make_runcmd_result_shape():
    assert make_runcmd_result("t1", True, "out", as_root=True, root_expired=False) == {
        "type": "runcmd_result", "task_id": "t1", "ok": True, "output": "out",
        "as_root": True, "root_expired": False,
    }


def test_make_getroot_shape():
    assert make_getroot("t1", "secret", "u@h") == {
        "type": "getroot", "task_id": "t1", "password": "secret", "caller_userhost": "u@h",
    }
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_protocol.py -v`
Expected: FAIL（import 的 make_command 等不存在）

- [ ] **Step 3: 精简 protocol.py**

`src/ircxmppbot/protocol.py` 全文替换：

```python
"""server↔client 协议：JSON lines 编解码与消息构造器（仅执行类消息）。"""

from __future__ import annotations

import json

VALID_TYPES = frozenset(
    {
        "auth",
        "auth_ok",
        "runcmd",
        "runcmd_result",
        "getroot",
        "status",
    }
)


class ProtocolError(Exception):
    """协议层错误。"""


def _validate(msg: dict) -> None:
    if not isinstance(msg, dict) or "type" not in msg:
        raise ProtocolError(f"消息必须为含 type 的 dict: {msg!r}")
    if msg["type"] not in VALID_TYPES:
        raise ProtocolError(f"未知消息类型: {msg['type']!r}")


def encode_msg(msg: dict) -> bytes:
    _validate(msg)
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_msg(line: str) -> dict:
    line = line.strip()
    if not line:
        raise ProtocolError("空行")
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"JSON 解析失败: {e}") from e
    _validate(msg)
    return msg


def make_auth(token: str, client_name: str, client_type: str, bot_name: str) -> dict:
    return {
        "type": "auth",
        "token": token,
        "client_name": client_name,
        "client_type": client_type,
        "bot_name": bot_name,
    }


def make_runcmd(task_id: str, cmd: str, caller_userhost: str = "") -> dict:
    return {
        "type": "runcmd",
        "task_id": task_id,
        "cmd": cmd,
        "caller_userhost": caller_userhost,
    }


def make_getroot(task_id: str, password: str, caller_userhost: str) -> dict:
    return {
        "type": "getroot",
        "task_id": task_id,
        "password": password,
        "caller_userhost": caller_userhost,
    }


def make_runcmd_result(
    task_id: str,
    ok: bool,
    output: str,
    as_root: bool = False,
    root_expired: bool = False,
) -> dict:
    return {
        "type": "runcmd_result",
        "task_id": task_id,
        "ok": ok,
        "output": output,
        "as_root": as_root,
        "root_expired": root_expired,
    }
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_protocol.py -v`
Expected: PASS（10 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/protocol.py tests/test_protocol.py
git commit -m "refactor(protocol): 精简为执行类消息（删 command/permission_update 等 6 种）"
```

---

### Task 2: server_link.py — 删除 handler 回调（client 纯执行器）

**Files:**
- Modify: `src/ircxmppbot/server_link.py`
- Modify: `tests/test_server_link.py`

**Interfaces:**
- Consumes: `protocol.make_auth/make_runcmd_result`
- Produces: `ServerLink` 不再接受 `handler` 参数；`_on_server_msg` 仅处理 runcmd/getroot/auth_ok；保留 `exec_runcmd/exec_getroot/root_sessions/_root_session_lookup/_root_session_ttl`

- [ ] **Step 1: 更新测试**

`tests/test_server_link.py` 的 `test_on_server_msg_routes_public` 删除，替换为：

```python
async def test_on_server_msg_routes_execution(link, monkeypatch):
    """仅处理 runcmd/getroot/auth_ok；未知消息忽略。"""
    calls = []

    async def fake_exec_runcmd(msg):
        calls.append(("runcmd", msg))

    async def fake_exec_getroot(msg):
        calls.append(("getroot", msg))

    monkeypatch.setattr(link, "exec_runcmd", fake_exec_runcmd)
    monkeypatch.setattr(link, "exec_getroot", fake_exec_getroot)
    await link._on_server_msg({"type": "runcmd", "task_id": "t1", "cmd": "id"})
    await link._on_server_msg({"type": "getroot", "task_id": "t2", "password": "pw", "caller_userhost": "u@h"})
    await link._on_server_msg({"type": "custom", "foo": 1})  # 未知消息忽略
    await link._on_server_msg({"type": "auth_ok", "ok": True})
    assert [c[0] for c in calls] == ["runcmd", "getroot"]
```

同时更新 fixture——`ServerLink(cfg, "irc", "bot")` 调用改为去掉 handler 参数（原 fixture 未传 handler，无需改）。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_server_link.py -v`
Expected: 测试收集失败或断言失败（handler 参数仍存在时自定义消息被 handler 处理）

- [ ] **Step 3: 修改 server_link.py**

`__init__` 删除 `handler` 参数与存储；`_on_server_msg` 删除 handler 分支：

```python
    def __init__(self, cfg: dict, client_type: str, bot_name: str) -> None:
        self.cfg = cfg
        self.client_type = client_type
        self.bot_name = bot_name
        self.root_sessions: dict[str, RootSession] = {}
        self._server_writer: asyncio.StreamWriter | None = None
        self._server_task: asyncio.Task | None = None
```

`_on_server_msg` 替换：

```python
    async def _on_server_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "runcmd":
            await self.exec_runcmd(msg)
        elif t == "getroot":
            await self.exec_getroot(msg)
        elif t == "auth_ok":
            if not msg.get("ok"):
                log.error("server 认证失败，请检查 token")
```

同时删除 docstring 中 handler 相关说明。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_server_link.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/server_link.py tests/test_server_link.py
git commit -m "refactor(server_link): 删 handler 回调，client 纯执行器"
```

---

### Task 3: 新建 irc_session.py — IRCSession（pydle 封装 + ChatTarget）

**Files:**
- Create: `src/ircxmppbot/irc_session.py`
- Create: `tests/test_irc_session.py`

**Interfaces:**
- Consumes: `ircxmppbot.commands.parse_command`、`ircxmppbot.util.{parse_userhost, split_text_bytes}`、`ChatTarget` 接口（定义见 Task 5，本任务先实现 session 侧）
- Produces:
  - `IRC_LINE_LIMIT: int = 400`
  - `class IRCSession(pydle.Client)`:
    - `__init__(self, server, irc_cfg: dict)` — `self.server`、`self.cfg`、`self.session_type = "irc"`、`self.oper_cache: dict[str, bool]`（userhost→is_oper 本地镜像）
    - `async def on_connect()` — join `cfg["channels"]`
    - `async def on_channel_message(target, source, message)` / `async def on_message(target, source, message)` — 转 `self._maybe_command`（解析后调 `self.server.handle_chat_command`，is_channel 区分）
    - `async def on_join(channel, user)` — 调 `self.server.handle_join(self, channel, user)`
    - `async def reply(self, text: str, channel: str | None, private_to: str | None = None)` — 分片发送（IRC 字节限制）
    - `async def execute(self, action: str, action_args: list[str], channel: str | None)` — ban/unban 执行 MODE
    - `async def dm_voters(self, proposal_id, candidate, voters, deadline_ts) -> list[str]` — 私信可达投票人，返回可达 userhost 列表
    - `def _nick_for_userhost(userhost) -> str | None`、`def _ban_mask(nick) -> str`、`async def _whois_user(nick) -> dict | None`、`async def run()`

- [ ] **Step 1: 写失败测试**

`tests/test_irc_session.py`:

```python
from pathlib import Path

import pytest

from ircxmppbot.irc_session import IRCSession, IRC_LINE_LIMIT


class FakeServer:
    """记录 handle_chat_command 调用的假 server。"""

    def __init__(self):
        self.calls = []

    async def handle_chat_command(self, cmd, args, caller, is_oper, channel, session):
        self.calls.append((cmd, args, caller, is_oper, channel))


def _cfg(**overrides):
    cfg = {
        "host": "irc.example.com",
        "port": 6697,
        "tls": True,
        "tls_verify": True,
        "bot_name": "qsdwindows_bot",
        "realname": "My IRC Bot",
        "channels": ["#chan1"],
        **overrides,
    }
    return cfg


@pytest.fixture
def session():
    s = IRCSession(FakeServer(), _cfg())
    s.users = {
        "op1": {"nickname": "op1", "username": "op1", "hostname": "host.example"},
        "op2": {"nickname": "op2", "username": "op2", "hostname": "host.example"},
    }
    return s


def test_line_limit_constant():
    assert IRC_LINE_LIMIT == 400


def test_session_type():
    s = IRCSession(FakeServer(), _cfg())
    assert s.session_type == "irc"
    assert s.bot_name == "qsdwindows_bot"


def test_nick_for_userhost(session):
    assert session._nick_for_userhost("op1@host.example") == "op1"
    assert session._nick_for_userhost("ghost@host.example") is None


def test_ban_mask_known_user(session):
    assert session._ban_mask("op1") == "op1@host.example"


async def test_channel_message_routes_to_server(session, monkeypatch):
    monkeypatch.setattr(session, "whois", lambda nick: {"username": "op1", "hostname": "host.example", "oper": True})
    await session.on_channel_message("#chan1", "op1", "!qsdwindows_bot info")
    assert session.server.calls == [("info", [], "op1@host.example", True, "#chan1")]


async def test_chat_command_local(session, monkeypatch):
    sent = []

    async def fake_message(target, text):
        sent.append((target, text))

    async def fake_llm(text):
        return "回复内容"

    monkeypatch.setattr(session, "message", fake_message)
    monkeypatch.setattr(session.server, "llm", type("L", (), {"chat": fake_llm})())
    await session.on_channel_message("#chan1", "op1", "!qsdwindows_bot chat 你好")
    assert sent, "应发送 LLM 回复"
    assert sent[0][1] == "回复内容"


async def test_dm_voters_returns_reachable(session, monkeypatch):
    sent = []
    monkeypatch.setattr(session, "message", lambda nick, text: sent.append((nick, text)))
    reachable = await session.dm_voters(
        "ab12", "op2@host.example", ["op1@host.example", "op2@host.example", "ghost@host.example"], 123.0
    )
    assert set(reachable) == {"op1@host.example", "op2@host.example"}
    assert len(sent) == 2
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_irc_session.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 irc_session.py**

```python
"""IRC 会话：server 内嵌的 pydle 客户端，处理聊天命令与投票私信。"""

from __future__ import annotations

import asyncio
import time
from typing import Any

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
        if parsed.cmd == "chat" and not is_channel and False:
            pass  # 私聊免前缀 chat 由 server.handle_chat_command 处理
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
                delay = min(2 ** attempt, 60.0)
                self.logger.warning("IRC 连接失败: %s，%.0fs 后重连", e, delay)
                await asyncio.sleep(delay)
                attempt += 1
        await asyncio.Event().wait()
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_irc_session.py -v`
Expected: PASS（6 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/irc_session.py tests/test_irc_session.py
git commit -m "feat: IRCSession（server 内嵌 pydle，ChatTarget 实现）"
```

---

### Task 4: 新建 xmpp_session.py — XMPPSession（slixmpp 封装）

**Files:**
- Create: `src/ircxmppbot/xmpp_session.py`
- Create: `tests/test_xmpp_session.py`

**Interfaces:**
- Consumes: `ircxmppbot.commands.parse_command`、`ircxmppbot.util.split_text_bytes`
- Produces:
  - `XMPP_LINE_LIMIT: int = 1500`
  - `class XMPPSession(slixmpp.ClientXMPP)`:
    - `__init__(self, server, xmpp_cfg)` — `self.session_type = "xmpp"`、`self.nick`、注册 xep_0030/0045/0199、事件 session_start/message/groupchat_message
    - `async def _session_start(event)` — join mucs
    - `def _on_message(msg)` — 私聊：免前缀直接 chat、带前缀解析命令（仅 chat/help 放行）
    - `def _on_groupchat(msg)` — 仅 `!<bot_name> chat` 前缀
    - `async def reply(self, text, channel=None, private_to=None)` — send_message 分片
    - `async def execute(self, action, action_args, channel)` — XMPP 不支持 ban 等 → no-op
    - `async def run()` — connect + `await self.disconnected`

- [ ] **Step 1: 写失败测试**

`tests/test_xmpp_session.py`:

```python
import asyncio
from pathlib import Path

import pytest

from ircxmppbot.xmpp_session import XMPPSession, XMPP_LINE_LIMIT


class FakeServer:
    def __init__(self):
        self.calls = []

    async def handle_chat_command(self, cmd, args, caller, is_oper, channel, session):
        self.calls.append((cmd, args, caller, is_oper, channel))


def _cfg(**overrides):
    cfg = {
        "jid": "bot@snikket.example",
        "password": "pw",
        "host": "snikket.example",
        "port": 5222,
        "tls": True,
        "bot_name": "qsdwindows_bot",
        "mucs": ["room@conference.snikket.example"],
        **overrides,
    }
    return cfg


@pytest.fixture
def session():
    s = XMPPSession(FakeServer(), _cfg())
    s.boundjid = type("J", (), {"bare": "bot@snikket.example"})()
    return s


def test_line_limit_constant():
    assert XMPP_LINE_LIMIT == 1500


def test_session_type():
    s = XMPPSession(FakeServer(), _cfg())
    assert s.session_type == "xmpp"
    assert s.nick == "qsdwindows_bot"


async def test_private_no_prefix_chats(session, monkeypatch):
    sent = []

    async def fake_chat(text):
        assert text == "hello world"
        return "hi"

    async def fake_send(mto, mbody, mtype):
        sent.append((mto, mbody, mtype))

    monkeypatch.setattr(session, "llm", type("L", (), {"chat": fake_chat})())
    monkeypatch.setattr(session, "send_message", fake_send)
    await session._handle_private("user@snikket.example", "hello world")
    assert sent == [("user@snikket.example", "hi", "chat")]


async def test_private_prefix_ban_rejected(session, monkeypatch):
    sent = []
    monkeypatch.setattr(session, "send_message", lambda mto, mbody, mtype: sent.append((mto, mbody, mtype)))
    await session._handle_private("user@snikket.example", "!qsdwindows_bot ban someone")
    assert sent and "仅支持 chat" in sent[0][1]
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_xmpp_session.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 xmpp_session.py**

```python
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
        self.server = server
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
        self.llm = server.llm

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
        asyncio.create_task(self.server.handle_chat_command(
            "chat", parsed.args, msg["from"].bare, False, msg["from"].bare, self
        ))

    async def _handle_private(self, sender: str, body: str) -> None:
        parsed = parse_command(body, self.bot_name)
        if parsed is None:
            await self.server.handle_chat_command("chat", [body], sender, False, None, self)
            return
        await self.server.handle_chat_command(parsed.cmd, parsed.args, sender, False, None, self)

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
            delay = min(2 ** attempt, 60.0)
            await asyncio.sleep(delay)
            attempt += 1
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_xmpp_session.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/xmpp_session.py tests/test_xmpp_session.py
git commit -m "feat: XMPPSession（server 内嵌 slixmpp，仅 chat）"
```

---

### Task 5: server.py — ChatTarget 抽象 + _cmd_* 签名改造 + _cmd_chat + handle_chat_command

**Files:**
- Modify: `src/ircxmppbot/server.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `irc_session.IRCSession`、`xmpp_session.XMPPSession`、`commands.COMMAND_LEVELS`、`llm.LLMClient`、`protocol.make_runcmd/make_getroot/make_runcmd_result`
- Produces:
  - `class ChatTarget` — 协议抽象：`session_type: str`；`async def reply(text, channel, private_to)`；`async def execute(action, action_args, channel)`；`async def dm_voters(...)`（仅 IRC 需实现，XMPP 返回 []）
  - `BotServer.__init__` 增加 `self.llm: LLMClient`（从 `cfg["llm"]` 构造）、`self.irc_session: IRCSession | None`、`self.xmpp_session: XMPPSession | None`
  - `async def handle_chat_command(self, cmd, args, caller_userhost, is_oper, channel, target)` — XMPP 限制 chat/help；权限校验；分发 `_cmd_*`
  - `async def handle_join(self, target, channel, user)` — 黑白名单踢人（从 irc_client 迁移）
  - `_cmd_*` 签名改为 `(self, target, args, caller, is_oper, channel)`，回复走 `target.reply(...)`
  - 新增 `_cmd_chat(self, target, args, caller, is_oper, channel)` — 调 `self.llm.chat` 分片回复
  - `_cmd_runcmd` 用 `target.reply(...)` 私信结果
  - `_cmd_shellop` 调 `target.dm_voters(...)` 收集可达投票人（无 reachability 消息）

- [ ] **Step 1: 写失败测试（ChatTarget 存根 + handle_chat_command 分发 + _cmd_chat）**

`tests/test_server.py` 追加：

```python
class FakeTarget:
    """ChatTarget 测试存根。"""

    session_type = "irc"

    def __init__(self, name="irc_main"):
        self.name = name
        self.replied = []   # (text, channel, private_to)
        self.executed = []
        self.dm_reachable = []

    async def reply(self, text, channel=None, private_to=None):
        self.replied.append((text, channel, private_to))

    async def execute(self, action, action_args, channel=None):
        self.executed.append((action, action_args, channel))

    async def dm_voters(self, proposal_id, candidate, voters, deadline_ts):
        self.dm_reachable.append((proposal_id, candidate, voters))
        return [v for v in voters if v != "ghost@host.example"]
```

追加测试：

```python
async def test_handle_chat_command_denied(srv):
    target = FakeTarget()
    await srv.handle_chat_command("botop", ["give", "x@y"], "nobody@host.example", False, "#chan", target)
    assert target.replied and "权限不足" in target.replied[-1][0]


async def test_handle_chat_command_botop_give(srv, tmp_path):
    target = FakeTarget()
    srv.conns = {}
    await srv.handle_chat_command("botop", ["give", "op3@host.example"], "op1@host.example", True, "#chan", target)
    assert target.replied and target.replied[-1][0].startswith("已授予")
    cfg = load_yaml(srv.config_path)
    assert "op3@host.example" in cfg["permissions"]["botop"]


async def test_cmd_chat_uses_llm(srv, monkeypatch):
    target = FakeTarget()

    async def fake_chat(text):
        return "hello back"

    srv.llm = type("L", (), {"chat": fake_chat})()
    await srv._cmd_chat(target, ["hello"], "u@h", False, "#chan")
    assert target.replied and target.replied[-1][0] == "hello back"


async def test_cmd_ban_executes_via_target(srv):
    target = FakeTarget()
    await srv._cmd_ban(target, ["baduser"], "op2@host.example", True, "#chan")
    assert target.executed and target.executed[0][0] == "ban"
    assert target.executed[0][1] == ["baduser"]


async def test_cmd_runcmd_private_reply(srv):
    origin = FakeTarget("irc_main")
    client_conn = FakeConn("shell_node_1")
    srv.conns = {"shell_node_1": client_conn}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._cmd_runcmd(origin, ["shell_node_1", "ls"], "boss@host.example", False, "#chan")
    )
    for _ in range(100):
        if any(m["type"] == "runcmd" for m in client_conn.sent):
            break
        await asyncio.sleep(0.01)
    runcmd = next(m for m in client_conn.sent if m["type"] == "runcmd")
    assert runcmd["caller_userhost"] == "boss@host.example"
    await srv._handle_runcmd_result(client_conn, make_runcmd_result(runcmd["task_id"], True, "out"))
    await task
    assert any("out" in r[0] for r in origin.replied)


async def test_cmd_shellop_uses_dm_voters(srv):
    target = FakeTarget()
    srv.conns = {}
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    srv.permissions.botop.add("op2@host.example")
    await srv._cmd_shellop(target, ["add", "op2@host.example"], "op1@host.example", True, "#chan")
    assert target.dm_reachable, "应调用 dm_voters 收集可达投票人"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_server.py -q`
Expected: FAIL（`_cmd_*` 签名不匹配、`handle_chat_command` 不存在）

- [ ] **Step 3: 改造 server.py**

关键改动（分步）：

**(a) 新增 ChatTarget 抽象**（文件顶部，`ClientConnection` 之前）：

```python
class ChatTarget:
    """命令回复目标抽象：IRCSession/XMPPSession 实现。"""

    session_type = ""

    async def reply(self, text: str, channel: str | None, private_to: str | None = None) -> None:
        raise NotImplementedError

    async def execute(self, action: str, action_args: list[str], channel: str | None) -> None:
        raise NotImplementedError

    async def dm_voters(self, proposal_id, candidate, voters, deadline_ts) -> list[str]:
        return []
```

**(b) `__init__` 增加 llm/sessions**：

```python
        self.llm = LLMClient(cfg.get("llm", {}))
        self.irc_session: IRCSession | None = None
        self.xmpp_session: XMPPSession | None = None
```

注意：`__init__` 目前只收 config_path，`reload(cfg)` 才拿到 cfg——把 `self.llm` 初始化移到 `reload`（cfg 就绪后），或 `__init__` 预读。采用：`__init__` 设 `self.llm = LLMClient({})` 占位，`reload` 里 `self.llm = LLMClient(cfg.get("llm", {}))`。

**(c) `reload` 中重建 llm**：

```python
    def reload(self, cfg: dict) -> None:
        if "server" not in cfg or "permissions" not in cfg:
            raise ConfigError("server.yaml 必须包含 server 与 permissions 段")
        self.permissions = Permissions.from_config(cfg)
        self.llm = LLMClient(cfg.get("llm", {}))
        self.cfg = cfg
        asyncio.create_task(self._broadcast_permissions())
```

**(d) `run()` 启动 IM sessions**：

```python
    async def run(self) -> None:
        ...现有监听启动...
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
```

**(e) `handle_chat_command`**：

```python
    async def handle_chat_command(self, cmd, args, caller_userhost, is_oper, channel, target) -> None:
        if target.session_type == "xmpp" and cmd not in ("chat", "help"):
            await target.reply("XMPP 端仅支持 chat 命令", channel, None)
            return
        uh = parse_userhost(caller_userhost)
        if uh:
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
```

**(f) `handle_join`**（迁移自 irc_client `_enforce_lists`）：

```python
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
```

**(g) 改造 `_cmd_*`**：`conn` 参数改为 `target`，`_reply(conn, ...)` 改为 `await target.reply(...)`，`_reply` 辅助删除。逐个替换：

- `_cmd_info`：`await self._reply(conn, True, reply, channel, caller)` → `await target.reply(reply, channel, caller if not channel else None)`（channel 存在则回频道，否则私信 caller）
- `_cmd_botop`：同上模式
- `_cmd_shellop`：`origin_conn` 改 `origin_target`；投票私信走 `target.dm_voters(...)`
- `_cmd_whitelist/_cmd_blacklist/_manage_list`：同 info 模式
- `_cmd_ban/_cmd_unban`：`await target.reply(...)` + `await target.execute("ban", [args[0]], channel)`
- `_cmd_runcmd`：`origin_conn` 改 `origin_target`；pending 存 `(fut, target, caller, target_name)`；结果 `await target.reply(text, None, caller)`
- `_handle_runcmd_result(conn, msg)` 保留（conn 是 client 连接，校验目标 client）
- 删除 `_reply` 方法

**(h) 新增 `_cmd_chat`**：

```python
    async def _cmd_chat(self, target, args, caller, is_oper, channel) -> None:
        text = " ".join(args)
        if not text:
            await target.reply("用法: chat <文本>", channel, caller)
            return
        try:
            reply = await self.llm.chat(text)
        except Exception as e:  # noqa: BLE001 - LLM 错误不致命
            reply = f"LLM 错误: {e}"
        await target.reply(reply, channel, None)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_server.py -v`
Expected: PASS（约 17 项改造后通过）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/server.py tests/test_server.py
git commit -m "refactor(server): ChatTarget 抽象 + _cmd_* 签名改造 + _cmd_chat + handle_chat_command"
```

---

### Task 6: client.py — 统一单类型执行器

**Files:**
- Create: `src/ircxmppbot/client.py`
- Create: `tests/test_client.py`

**Interfaces:**
- Consumes: `server_link.ServerLink`、`config.load_yaml`
- Produces:
  - `class ShellClient` — `__init__(config_path)` 建 `ServerLink`；`async def run()` 启动 link 循环并 `await asyncio.Event().wait()`

- [ ] **Step 1: 写失败测试**

`tests/test_client.py`:

```python
from pathlib import Path

from ircxmppbot.client import ShellClient


def _write_cfg(tmp_path: Path) -> Path:
    cfg = {
        "client": {
            "name": "shell_node_1",
            "server": {
                "host": "127.0.0.1",
                "port": 8443,
                "token": "test-token",
                "tls": {"verify": False},
            },
        },
        "root_session_ttl": 300,
    }
    p = tmp_path / "client.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def test_client_creates_link(tmp_path):
    c = ShellClient(_write_cfg(tmp_path))
    assert c.link is not None
    assert c.link.client_type == "shell"
    assert c.cfg["client"]["name"] == "shell_node_1"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_client.py -v`
Expected: FAIL（ModuleNotFoundError）

- [ ] **Step 3: 实现 client.py**

```python
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
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_client.py -v`
Expected: PASS（1 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/client.py tests/test_client.py
git commit -m "feat: 统一 ShellClient（纯执行器）"
```

---

### Task 7: __main__.py + configs 重组 + 删除旧模块

**Files:**
- Modify: `src/ircxmppbot/__main__.py`
- Create: `configs/server.yaml`（重组）、`configs/client.yaml`
- Delete: `src/ircxmppbot/irc_client.py`、`src/ircxmppbot/xmpp_client.py`、`configs/client_irc.yaml`、`configs/client_xmpp.yaml`
- Modify: `tests/test_main.py`

**Interfaces:**
- Consumes: `BotServer.run()`、`ShellClient.run()`
- Produces: CLI `server [config]` / `client [config]`；两个样例配置

- [ ] **Step 1: 更新测试**

`tests/test_main.py` 全文替换：

```python
from pathlib import Path

from ircxmppbot.config import load_yaml
from ircxmppbot.protocol import decode_msg, encode_msg, make_auth


def test_sample_server_config_loads():
    cfg = load_yaml(Path("configs/server.yaml"))
    assert "server" in cfg and "permissions" in cfg
    assert "listen_port" in cfg["server"]
    assert "whitelist" in cfg["permissions"] and "blacklist" in cfg["permissions"]


def test_sample_client_config_loads():
    cfg = load_yaml(Path("configs/client.yaml"))
    assert "client" in cfg
    assert "name" in cfg["client"]
    assert "server" in cfg["client"]
    assert "token" in cfg["client"]["server"]


def test_server_config_irc_optional():
    cfg = load_yaml(Path("configs/server.yaml"))
    # irc/xmpp 段可选：样例中应至少有一个，但结构上不强制
    assert "irc" in cfg or "xmpp" in cfg


def test_protocol_auth_roundtrip():
    msg = make_auth("token", "n", "irc", "bot")
    assert decode_msg(encode_msg(msg).decode("utf-8")) == msg
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_main.py -v`
Expected: FAIL（configs/client.yaml 不存在）

- [ ] **Step 3: 重写 __main__.py**

```python
"""CLI 入口：python -m ircxmppbot server|client [config]"""

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
    "client": "configs/client.yaml",
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


async def _run_client(config_path: Path) -> None:
    from .client import ShellClient

    await ShellClient(config_path).run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ircxmppbot")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("server", "client"):
        p = sub.add_parser(name)
        p.add_argument("config", nargs="?", default=DEFAULT_CONFIGS[name])
        p.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("-d", "--debug", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    _setup_logging(bool(getattr(args, "debug", False)))
    config_path = Path(args.config)
    try:
        if args.mode == "server":
            asyncio.run(_run_server(config_path))
        else:
            asyncio.run(_run_client(config_path))
    except KeyboardInterrupt:
        log.info("收到中断信号，退出")
    except Exception as e:  # noqa: BLE001
        log.error("运行失败: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 重组配置**

`configs/server.yaml` 全文：

```yaml
server:
  listen_host: 0.0.0.0
  listen_port: 8443
  tls:
    certfile: server.crt
    keyfile: server.key
  token: "change-me-shared-secret"
  oper_cache_ttl: 60
  shellop_confirm_timeout: 60

irc:
  host: irc.example.com
  port: 6697
  tls: true
  tls_verify: true
  bot_name: qsdwindows_bot
  realname: "My IRC Bot"
  channels:
    - "#chan1"

xmpp:
  jid: "bot@snikket.example"
  password: "change-me"
  host: "snikket.example"
  port: 5222
  tls: true
  bot_name: qsdwindows_bot
  mucs:
    - "room@conference.snikket.example"

llm:
  format: openai_chat
  base_url: "https://api.openai.com/v1"
  api_key: "sk-xxx"
  model: "gpt-4o-mini"
  system_prompt: "You are a helpful bot."
  temperature: 0.7
  max_tokens: 1000
  timeout: 60
  max_reply_chars: 1500

permissions:
  botop:
    - "user1@host.example"
  shellop: []
  whitelist:
    enabled: false
    entries: []
  blacklist:
    enabled: false
    entries: []
```

`configs/client.yaml` 全文：

```yaml
client:
  name: shell_node_1
  server:
    host: 127.0.0.1
    port: 8443
    token: "change-me-shared-secret"
    tls:
      verify: false
root_session_ttl: 300
```

- [ ] **Step 5: 删除旧模块与配置 + 验证**

```bash
git rm src/ircxmppbot/irc_client.py src/ircxmppbot/xmpp_client.py configs/client_irc.yaml configs/client_xmpp.yaml
/tmp/opencode/ircxmppbot-venv/bin/pytest tests/test_main.py -v
```

Expected: PASS（4 passed）

- [ ] **Step 6: 提交**

```bash
git add src/ircxmppbot/__main__.py configs tests/test_main.py
git commit -m "refactor: CLI server/client + 配置重组 + 删除旧 client 模块"
```

---

### Task 8: 全量回归 + README/AGENTS.md 同步

**Files:**
- Modify: `README.md`、`AGENTS.md`
- Modify: 全部测试文件（删除 irc/xmpp client 测试引用，确认 test_server_link 不受影响）

**Interfaces:**
- Consumes: 全部模块
- Produces: 文档同步 + 全量测试通过

- [ ] **Step 1: 清理过期测试**

```bash
git rm tests/test_irc_client.py tests/test_xmpp_client.py
```

删除后全量回归：

```bash
/tmp/opencode/ircxmppbot-venv/bin/pytest tests/ -q
```

修复所有因删除 irc_client/xmpp_client 或协议精简导致的失败（主要是 test_server.py 中对已删构造器的引用、test_server_link 中 handler 相关）。

- [ ] **Step 2: 更新 README.md**

关键修改：
- 架构描述：server 连 IM + client 纯执行
- 命令表：`runcmd getroot` 保留（client 端执行）
- 快速开始：`python -m ircxmppbot client`（替代 client-irc/client-xmpp）
- 说明：配置集中 server.yaml，irc/xmpp 段可选

- [ ] **Step 3: 更新 AGENTS.md**

关键修改：
- 架构段：server.py（IRCSession/XMPPSession + 命令本地执行）、client.py（纯执行器）、server_link.py
- 删除 irc_client/xmpp_client 描述
- 命令与权限段：chat 在 server 处理；shellop 投票用 dm_voters
- 测试约定：session 测试 mock pydle/slixmpp
- 环境陷阱：不变

- [ ] **Step 4: 全量回归 + CLI 冒烟**

```bash
/tmp/opencode/ircxmppbot-venv/bin/pytest tests/ -q
/tmp/opencode/ircxmppbot-venv/bin/python -m ircxmppbot --help
```

Expected: 全部 PASS；help 显示 server/client 两个子命令

- [ ] **Step 5: 提交**

```bash
git add README.md AGENTS.md tests/
git commit -m "docs: 架构调整文档同步 + 全量回归"
```

---

## 自审清单

**1. Spec 覆盖核查：**

| 设计文档要求 | 对应任务 |
|---|---|
| server.yaml 唯一配置（irc/xmpp/llm/permissions） | Task 7（配置重组） |
| irc/xmpp 段可选 | Task 5（run() 条件启动）+ Task 7 配置 |
| server 内嵌 IRCSession/XMPPSession | Task 3/4 + Task 5 run() |
| 命令处理上移（handle_chat_command） | Task 5 |
| _cmd_chat 新增（server 调 LLM） | Task 5(h) |
| client 统一单类型 | Task 6 |
| 协议精简 6 消息 | Task 1 |
| server_link 删 handler | Task 2 |
| 删除 irc_client/xmpp_client | Task 7 |
| CLI server/client | Task 7 |
| XMPP 仅 chat/help + jid 映射 | Task 5(e) + Task 4 |
| shellop 投票内部化（dm_voters） | Task 5(g) + Task 3 |
| 黑白名单踢人上移（handle_join） | Task 5(f) |
| getroot 仍在 client | Task 6（ServerLink 保留） |

**2. 占位符扫描：** 所有步骤含完整代码与预期输出，无 TBD/TODO。

**3. 类型/签名一致性：**
- `_cmd_*` 新签名 `(self, target, args, caller, is_oper, channel)` 在 Task 5 统一改造，Task 3/4 session 实现的 `reply/execute/dm_voters` 与之匹配
- `handle_chat_command(cmd, args, caller_userhost, is_oper, channel, target)` 被 IRCSession（Task 3）与 XMPPSession（Task 4）调用
- `ServerLink` 构造 `(cfg, client_type, bot_name)` 无 handler（Task 2），ShellClient 用它（Task 6）
- `make_runcmd/make_runcmd_result/make_getroot` 在 Task 1 保留，server（Task 5）与 ServerLink（Task 2/6）调用一致

**4. 已知边界（实现时注意）：**
- Task 5 的 `_cmd_*` 改造是最大工程（约 10 个处理器签名 + 回复路径变更），建议分步提交（可先改核心 4 个：botop/ban/runcmd/shellop，再改其余）
- `_cmd_shellop` 中 `origin_conn` → `origin_target` 后，`_finalize_proposal` 的回复也改 `target.reply(...)`
- `runcmd_pending` 元组改为 `(fut, origin_target: ChatTarget, caller, target_name)`——发起者从 ClientConnection 变为 ChatTarget（命令在 server 本地发起）；`_handle_runcmd_result(conn, msg)` 仍校验目标 client 的 `conn.client_name == target_name`；超时/断开清理 `_drop_pending_for` 需适配（`origin_target` 无 client_name，改为按 `origin_target is target` 或加 `origin_target_id` 字段）
- XMPPSession 的 `_on_groupchat` 把 `msg["from"].bare` 当 channel 传（群聊回复目标），与 IRC 的 channel 语义对齐——XMPP 群聊 chat 用 groupchat 类型发送
- `handle_join` 依赖 `target.users`/`target.oper_cache`——IRCSession 已实现；XMPP 无 JOIN 概念，不调 handle_join
- Task 8 删除 test_irc_client/test_xmpp_client 后，原 getroot/runcmd 相关断言需确认在 test_server_link（client 侧逻辑）或新增的 server 测试中仍有覆盖
- server.py 的 `_dispatch`/`_handle_client`（client 连接处理）保留——用于 runcmd 路由与认证，但不再处理 `command` 消息（删除该分支）
