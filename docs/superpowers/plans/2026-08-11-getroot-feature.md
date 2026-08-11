# getroot 功能实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 runcmd 增加 root 提权：`!<bot_name> runcmd getroot <client_root_password>` 通过 `su --pty` 验证密码并建立 5 分钟 root 会话，之后该用户对该 client 的 runcmd 以 root 执行，过期回退普通用户并提示。

**Architecture:** server 识别 `runcmd getroot` 子命令并向发起 conn 发送独立 `getroot` 消息（密码走独立字段，不进 cmd）；client 端用 `su --pty` 验证密码后缓存 `root_sessions[user@host] = (password, expiry)`；普通 runcmd 消息新增 `caller_userhost` 字段，目标 client 据此判断是否用 root 执行，结果带 `as_root`/`root_expired` 标记。

**Tech Stack:** Python 3.14、asyncio、`su --pty`（util-linux，已实证支持管道 stdin）、pytest+pytest-asyncio

## Global Constraints

- 所有代码位于 `src/ircxmppbot/` 包内；测试位于 `tests/`
- 依赖版本与既有项目一致（无新增依赖，不使用 pexpect）
- root 密码**仅存 client 内存**（`root_sessions` dict），不落盘、不写日志、不混入 `cmd` 字段
- getroot 仅 shellop 可调（继承 runcmd 权限检查）；会话绑定 user@host，仅所在 client 生效
- 会话 TTL 默认 300 秒，可配置 `root_session_ttl`（client 配置文件）
- 密码经 `shlex.quote` 转义后注入 `su --pty` 命令，防 shell 注入
- 过期行为：回退普通用户执行 + 结果附加提示，不拒绝执行
- 测试不依赖真实 root（mock subprocess / create_subprocess_shell）

---

### Task 1: protocol.py — runcmd 消息加 caller_userhost + 新增 getroot 消息 + runcmd_result 扩展

**Files:**
- Modify: `src/ircxmppbot/protocol.py`
- Modify: `tests/test_protocol.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `make_runcmd(task_id: str, cmd: str, caller_userhost: str = "") -> dict` — 消息含 `caller_userhost` 键
  - `make_getroot(task_id: str, password: str, caller_userhost: str) -> dict` — 返回 `{"type": "getroot", "task_id", "password", "caller_userhost"}`
  - `make_runcmd_result(task_id: str, ok: bool, output: str, as_root: bool = False, root_expired: bool = False) -> dict` — 消息含 `as_root`/`root_expired` 键
  - `VALID_TYPES` 增加 `"getroot"`

- [ ] **Step 1: 更新测试（先写失败断言）**

`tests/test_protocol.py` — 修改 `test_make_vote_and_runcmd_shape` 与 `test_make_command_result_shape`，新增 getroot 断言：

```python
def test_make_command_result_shape():
    assert make_command_result(True, "ok", "channel", "#chan", "ban", ["nick"]) == {
        "type": "command_result", "ok": True, "reply": "ok",
        "target_type": "channel", "target": "#chan",
        "action": "ban", "action_args": ["nick"],
    }
    # 新字段默认值
    assert make_command_result(False, "err", "private", "u@h") == {
        "type": "command_result", "ok": False, "reply": "err",
        "target_type": "private", "target": "u@h",
        "action": "none", "action_args": [],
    }


def test_make_vote_and_runcmd_shape():
    assert make_vote("ab12", "confirm", "u@h") == {
        "type": "vote", "proposal_id": "ab12", "vote": "confirm", "voter_userhost": "u@h",
    }
    # runcmd 新增 caller_userhost 字段
    assert make_runcmd("t1", "ls -la", "u@h") == {
        "type": "runcmd", "task_id": "t1", "cmd": "ls -la", "caller_userhost": "u@h",
    }
    assert make_runcmd("t2", "id") == {
        "type": "runcmd", "task_id": "t2", "cmd": "id", "caller_userhost": "",
    }
    # runcmd_result 新增 as_root/root_expired 字段
    assert make_runcmd_result("t1", True, "out", as_root=True) == {
        "type": "runcmd_result", "task_id": "t1", "ok": True, "output": "out",
        "as_root": True, "root_expired": False,
    }
    assert make_runcmd_result("t2", False, "", root_expired=True) == {
        "type": "runcmd_result", "task_id": "t2", "ok": False, "output": "",
        "as_root": False, "root_expired": True,
    }


def test_make_getroot_shape():
    assert make_getroot("t1", "secret", "u@h") == {
        "type": "getroot", "task_id": "t1", "password": "secret", "caller_userhost": "u@h",
    }
```

`tests/test_protocol.py` import 段增加 `make_getroot`。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_protocol.py -v`
Expected: FAIL（`make_runcmd` 无 `caller_userhost` 键、`make_getroot` 不存在、`VALID_TYPES` 无 `getroot`）

- [ ] **Step 3: 修改 protocol.py**

`src/ircxmppbot/protocol.py` 全文替换为：

```python
"""server↔client 协议：JSON lines 编解码与消息构造器。"""

from __future__ import annotations

import json

VALID_TYPES = frozenset(
    {
        "auth",
        "auth_ok",
        "command",
        "command_result",
        "permission_update",
        "shellop_proposal",
        "vote",
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
    """将消息编码为 JSON line（UTF-8，以 \\n 结尾）。"""
    _validate(msg)
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def decode_msg(line: str) -> dict:
    """解析单行 JSON 消息。"""
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


def make_command(
    cmd: str,
    args: list[str],
    caller_userhost: str,
    caller_is_oper: bool,
    channel: str | None = None,
) -> dict:
    return {
        "type": "command",
        "cmd": cmd,
        "args": args,
        "caller_userhost": caller_userhost,
        "caller_is_oper": caller_is_oper,
        "channel": channel,
    }


def make_command_result(
    ok: bool,
    reply: str,
    target_type: str,
    target: str,
    action: str = "none",
    action_args: list[str] | None = None,
) -> dict:
    return {
        "type": "command_result",
        "ok": ok,
        "reply": reply,
        "target_type": target_type,
        "target": target,
        "action": action,
        "action_args": action_args or [],
    }


def make_permission_update(snapshot: dict, oper_cache: dict) -> dict:
    return {"type": "permission_update", **snapshot, "oper_cache": oper_cache}


def make_shellop_proposal(
    proposal_id: str, candidate: str, deadline_ts: float, voters: list[str]
) -> dict:
    return {
        "type": "shellop_proposal",
        "proposal_id": proposal_id,
        "candidate": candidate,
        "deadline_ts": deadline_ts,
        "voters": voters,
    }


def make_vote(proposal_id: str, vote: str, voter_userhost: str) -> dict:
    return {
        "type": "vote",
        "proposal_id": proposal_id,
        "vote": vote,
        "voter_userhost": voter_userhost,
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
Expected: PASS（13 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/protocol.py tests/test_protocol.py
git commit -m "feat(protocol): runcmd 加 caller_userhost，新增 getroot 消息，runcmd_result 扩展"
```

---

### Task 2: server.py — runcmd getroot 分支 + 过期提示

**Files:**
- Modify: `src/ircxmppbot/server.py`
- Modify: `tests/test_server.py`

**Interfaces:**
- Consumes: `protocol.make_getroot`、`protocol.make_runcmd`（新签名）
- Produces:
  - `BotServer._cmd_runcmd(conn, args, caller, is_oper, channel)` — 检测 `args[0] == "getroot"` 时向发起 conn 发 `make_getroot(task_id, args[1], caller)`，普通路径 `make_runcmd(task_id, cmd, caller_userhost=caller)`
  - `BotServer._handle_runcmd_result(msg)` — `root_expired` 为 True 时在私信文本前附加「[root 会话已过期，已按普通用户执行，请重新 getroot]」

- [ ] **Step 1: 写失败测试**

`tests/test_server.py` 追加：

```python
async def test_runcmd_getroot_routes_to_self(srv):
    """runcmd getroot 目标 = 调用者所在 client（发起 conn 自身）。"""
    conn = FakeConn("irc_main")
    srv.conns = {"irc_main": conn}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._handle_command(
            conn,
            make_command("runcmd", ["getroot", "secretpw"], "boss@host.example", False, "#chan"),
        )
    )
    for _ in range(100):
        if any(m["type"] == "getroot" for m in conn.sent):
            break
        await asyncio.sleep(0.01)
    getroot = next(m for m in conn.sent if m["type"] == "getroot")
    assert getroot["password"] == "secretpw"
    assert getroot["caller_userhost"] == "boss@host.example"

    await srv._handle_runcmd_result(
        make_runcmd_result(getroot["task_id"], True, "root 已激活", as_root=True)
    )
    await task
    result = next(m for m in conn.sent if m["type"] == "command_result" and "root 已激活" in m["reply"])
    assert result["target_type"] == "private"


async def test_runcmd_getroot_missing_password(srv):
    conn = FakeConn()
    srv.permissions.shellop.add("boss@host.example")
    await _cmd(srv, conn, "runcmd", ["getroot"], "boss@host.example")
    assert not _last(conn)["ok"]
    assert "用法" in _last(conn)["reply"]


async def test_runcmd_expired_hint(srv):
    """root 会话过期 → 私信附加提示。"""
    origin = FakeConn("irc_main")
    target = FakeConn("xmpp_main")
    srv.conns = {"irc_main": origin, "xmpp_main": target}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._handle_command(
            origin,
            make_command("runcmd", ["xmpp_main", "id"], "boss@host.example", False, "#chan"),
        )
    )
    for _ in range(100):
        if any(m["type"] == "runcmd" for m in target.sent):
            break
        await asyncio.sleep(0.01)
    runcmd = next(m for m in target.sent if m["type"] == "runcmd")
    assert runcmd["caller_userhost"] == "boss@host.example"

    await srv._handle_runcmd_result(
        make_runcmd_result(runcmd["task_id"], True, "uid=1000", root_expired=True)
    )
    await task
    result = next(m for m in origin.sent if m["type"] == "command_result" and m["target_type"] == "private")
    assert "root 会话已过期" in result["reply"]
    assert "uid=1000" in result["reply"]
```

`tests/test_server.py` import 段增加 `make_runcmd_result`（已存在）并确认 `make_command` 已导入。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_server.py -v`
Expected: FAIL（`_cmd_runcmd` 未处理 getroot；`make_runcmd` 缺 caller_userhost 导致断言失败）

- [ ] **Step 3: 修改 server.py**

`src/ircxmppbot/server.py` 的 import 段增加 `make_getroot`：

```python
from .protocol import (
    decode_msg,
    encode_msg,
    make_command_result,
    make_getroot,
    make_permission_update,
    make_runcmd,
    make_shellop_proposal,
)
```

`_cmd_runcmd` 全文替换为：

```python
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
            self.runcmd_pending[task_id] = (fut, conn, caller)
            try:
                await conn.send(make_getroot(task_id, password, caller))
            except (ConnectionError, OSError):
                self.runcmd_pending.pop(task_id, None)
                await self._reply(conn, False, "client 连接异常", channel, caller)
                return
            await self._reply(conn, True, f"getroot 已发送到本 client，等待验证", channel, caller)
            try:
                ok, output = await asyncio.wait_for(fut, timeout=RUNDMD_TIMEOUT)
            except asyncio.TimeoutError:
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
        self.runcmd_pending[task_id] = (fut, conn, caller)
        try:
            await target.send(make_runcmd(task_id, cmd, caller_userhost=caller))
        except (ConnectionError, OSError):
            self.runcmd_pending.pop(task_id, None)
            await self._reply(conn, False, f"client {target_name} 连接异常", channel, caller)
            return
        await self._reply(conn, True, f"runcmd 已发送到 {target_name}", channel, caller)
        try:
            ok, output = await asyncio.wait_for(fut, timeout=RUNDMD_TIMEOUT)
        except asyncio.TimeoutError:
            await self._reply(conn, False, f"runcmd 在 {target_name} 超时（{int(RUNDMD_TIMEOUT)}s）", None, caller)
            return
        if not ok:
            text = f"命令执行失败:\n{output}" if output else "命令执行失败"
        else:
            text = output if output else "命令执行成功，无输出"
        await conn.send(make_command_result(ok, text, "private", caller))
```

`_handle_runcmd_result` 全文替换为：

```python
    async def _handle_runcmd_result(self, msg: dict) -> None:
        tid = msg.get("task_id", "")
        entry = self.runcmd_pending.pop(tid, None)
        if entry is None:
            log.warning("未知 runcmd task: %s", tid)
            return
        fut, origin_conn, caller = entry
        if not fut.done():
            output = msg.get("output", "")
            if msg.get("root_expired"):
                output = "[root 会话已过期，已按普通用户执行，请重新 getroot]\n" + output
            fut.set_result((bool(msg.get("ok")), output))
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_server.py -v`
Expected: PASS（15 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/server.py tests/test_server.py
git commit -m "feat(server): runcmd getroot 分支与过期提示"
```

---

### Task 3: irc_client.py — root_sessions 缓存 + _exec_getroot + runcmd root 化

**Files:**
- Modify: `src/ircxmppbot/irc_client.py`
- Modify: `tests/test_irc_client.py`

**Interfaces:**
- Consumes: `protocol.make_getroot`（新消息）、`protocol.make_runcmd_result`（新签名）
- Produces:
  - `IRCBot.root_sessions: dict[str, tuple[str, float]]` — userhost → (password, expiry_monotonic)
  - `IRCBot._root_session_ttl() -> float` — `float(self.cfg.get("root_session_ttl", 300))`
  - `async def _exec_getroot(msg: dict) -> None` — su --pty 验证并缓存
  - `async def _exec_runcmd(msg: dict) -> None` — 按 root 会话决定执行方式，回传 as_root/root_expired

- [ ] **Step 1: 写失败测试**

`tests/test_irc_client.py` 追加：

```python
async def test_exec_getroot_success(bot, monkeypatch):
    """密码正确 → 缓存会话 + 回传 as_root=True。"""
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    monkeypatch.setattr(bot, "_server_send", fake_server_send)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"0\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        assert "su --pty root -c 'id -u'" in cmd
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await bot._exec_getroot({"task_id": "t1", "password": "pw", "caller_userhost": "boss@host.example"})
    assert sent and sent[0]["as_root"] is True
    assert "boss@host.example" in bot.root_sessions


async def test_exec_getroot_wrong_password(bot, monkeypatch):
    """密码错误 → 不缓存 + 回传错误。"""
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    monkeypatch.setattr(bot, "_server_send", fake_server_send)

    class FakeProc:
        returncode = 1

        async def communicate(self):
            return (b"su: Authentication failure\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await bot._exec_getroot({"task_id": "t1", "password": "bad", "caller_userhost": "boss@host.example"})
    assert sent and sent[0]["ok"] is False
    assert "boss@host.example" not in bot.root_sessions


async def test_exec_runcmd_as_root(bot, monkeypatch):
    """有效会话 → 命令被 su 包装执行，回传 as_root=True。"""
    import time as _time
    bot.root_sessions["boss@host.example"] = ("pw", _time.monotonic() + 300)
    sent = []
    commands = []

    async def fake_server_send(msg):
        sent.append(msg)

    monkeypatch.setattr(bot, "_server_send", fake_server_send)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"uid=0(root)\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        commands.append(cmd)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await bot._exec_runcmd({"task_id": "t1", "cmd": "id", "caller_userhost": "boss@host.example"})
    assert commands and "su --pty root -c" in commands[0]
    assert sent and sent[0]["as_root"] is True


async def test_exec_runcmd_expired(bot, monkeypatch):
    """过期会话 → 普通执行 + root_expired=True。"""
    import time as _time
    bot.root_sessions["boss@host.example"] = ("pw", _time.monotonic() - 1)
    sent = []
    commands = []

    async def fake_server_send(msg):
        sent.append(msg)

    monkeypatch.setattr(bot, "_server_send", fake_server_send)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"uid=1000\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        commands.append(cmd)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await bot._exec_runcmd({"task_id": "t1", "cmd": "id", "caller_userhost": "boss@host.example"})
    assert commands and "su --pty" not in commands[0]
    assert sent and sent[0]["root_expired"] is True


async def test_exec_runcmd_no_session(bot, monkeypatch):
    """无会话 → 普通执行，无过期标记。"""
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    monkeypatch.setattr(bot, "_server_send", fake_server_send)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"uid=1000\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await bot._exec_runcmd({"task_id": "t1", "cmd": "id", "caller_userhost": "nobody@host.example"})
    assert sent and sent[0]["as_root"] is False and sent[0]["root_expired"] is False
```

`tests/test_irc_client.py` 顶部 import 段增加 `import asyncio` 与 `import time`。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_irc_client.py -v`
Expected: FAIL（`_exec_getroot` 不存在、`_exec_runcmd` 无 root 逻辑）

- [ ] **Step 3: 修改 irc_client.py**

`src/ircxmppbot/irc_client.py`：
1. import 段增加 `import shlex`、`import time`
2. `__init__` 增加 `self.root_sessions: dict[str, tuple[str, float]] = {}`
3. `_on_server_msg` 增加 `elif t == "getroot": await self._exec_getroot(msg)`
4. 新增方法（替换 `_exec_runcmd` 并在其后添加 `_exec_getroot` 与 `_root_session_ttl`）：

```python
    def _root_session_ttl(self) -> float:
        return float(self.cfg.get("root_session_ttl", 300))

    async def _exec_getroot(self, msg: dict) -> None:
        task_id = msg["task_id"]
        password = msg["password"]
        caller = parse_userhost(msg.get("caller_userhost", ""))
        log.info("getroot 验证请求（user=%s）", caller)
        proc = await asyncio.create_subprocess_shell(
            f"printf '%s\\n' {shlex.quote(password)} | su --pty root -c 'id -u'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        ok = proc.returncode == 0 and stdout.strip() == b"0"
        if ok and caller:
            self.root_sessions[caller] = (password, time.monotonic() + self._root_session_ttl())
            output = f"root 已激活（{int(self._root_session_ttl())} 秒有效）"
            log.info("getroot 成功: %s", caller)
        else:
            output = "root 密码验证失败"
            log.warning("getroot 失败: %s", caller)
        await self._server_send(make_runcmd_result(task_id, ok, output, as_root=ok))

    async def _exec_runcmd(self, msg: dict) -> None:
        task_id = msg["task_id"]
        cmd = msg["cmd"]
        caller = parse_userhost(msg.get("caller_userhost", ""))
        entry = self.root_sessions.get(caller)
        now = time.monotonic()
        as_root = False
        root_expired = False
        if entry and entry[1] > now:
            as_root = True
            full_cmd = f"printf '%s\\n' {shlex.quote(entry[0])} | su --pty root -c {shlex.quote(cmd)}"
        elif entry:
            root_expired = True
            full_cmd = cmd
        else:
            full_cmd = cmd
        log.info("执行 runcmd (root=%s): %s", as_root, cmd)
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode("utf-8", errors="replace")
        await self._server_send(make_runcmd_result(
            task_id, proc.returncode == 0, output, as_root=as_root, root_expired=root_expired
        ))
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_irc_client.py -v`
Expected: PASS（19 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/irc_client.py tests/test_irc_client.py
git commit -m "feat(irc): getroot 提权与 root 化 runcmd"
```

---

### Task 4: xmpp_client.py — 同 IRC client 的 getroot/root 逻辑

**Files:**
- Modify: `src/ircxmppbot/xmpp_client.py`
- Modify: `tests/test_xmpp_client.py`

**Interfaces:**
- Consumes: 同 Task 3（protocol 消息）
- Produces: 同 Task 3 的 `root_sessions`/`_exec_getroot`/`_exec_runcmd`/`_root_session_ttl`（XMPP 版）

- [ ] **Step 1: 写失败测试**

`tests/test_xmpp_client.py` 追加（结构同 Task 3，`bot` fixture 已存在）：

```python
async def test_exec_getroot_success(bot, monkeypatch):
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    monkeypatch.setattr(bot, "_server_send", fake_server_send)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"0\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        assert "su --pty root -c 'id -u'" in cmd
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await bot._exec_getroot({"task_id": "t1", "password": "pw", "caller_userhost": "boss@host.example"})
    assert sent and sent[0]["as_root"] is True
    assert "boss@host.example" in bot.root_sessions


async def test_exec_runcmd_as_root(bot, monkeypatch):
    import time as _time
    bot.root_sessions["boss@host.example"] = ("pw", _time.monotonic() + 300)
    sent = []
    commands = []

    async def fake_server_send(msg):
        sent.append(msg)

    monkeypatch.setattr(bot, "_server_send", fake_server_send)

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"uid=0(root)\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        commands.append(cmd)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await bot._exec_runcmd({"task_id": "t1", "cmd": "id", "caller_userhost": "boss@host.example"})
    assert commands and "su --pty root -c" in commands[0]
    assert sent and sent[0]["as_root"] is True
```

`tests/test_xmpp_client.py` import 段已含 `asyncio`（确认），需确认 `import time` 在测试内部使用。

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_xmpp_client.py -v`
Expected: FAIL（`_exec_getroot` 不存在）

- [ ] **Step 3: 修改 xmpp_client.py**

`src/ircxmppbot/xmpp_client.py`：
1. import 段增加 `import shlex`、`import time`
2. `__init__` 增加 `self.root_sessions: dict[str, tuple[str, float]] = {}`
3. `_on_server_msg` 增加 `elif t == "getroot": await self._exec_getroot(msg)`
4. 新增 `_root_session_ttl`/`_exec_getroot` 方法、替换 `_exec_runcmd`（代码与 Task 3 完全相同，复制粘贴）

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_xmpp_client.py -v`
Expected: PASS（9 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/xmpp_client.py tests/test_xmpp_client.py
git commit -m "feat(xmpp): getroot 提权与 root 化 runcmd"
```

---

### Task 5: 配置示例 + 全量回归 + 文档

**Files:**
- Modify: `configs/client_irc.yaml`、`configs/client_xmpp.yaml`
- Modify: `README.md`
- Modify: `docs/superpowers/plans/2026-08-11-getroot-feature.md`（本计划）

**Interfaces:**
- Consumes: 全部模块
- Produces: 配置文档与功能说明

- [ ] **Step 1: 两个 client 配置示例增加 root_session_ttl**

`configs/client_irc.yaml` 与 `configs/client_xmpp.yaml` 的 `client:` 段之后（或文件末尾）增加：

```yaml
root_session_ttl: 300   # getroot 会话有效期（秒）
```

- [ ] **Step 2: README 增加 getroot 说明**

`README.md` 命令表增加一行：

```markdown
| `runcmd getroot <client_root_password>` | shellop | 本 client root 提权（5 分钟有效） |
```

并在「说明」段增加：

```markdown
- runcmd getroot 使用 su 提权，会话绑定发起用户，仅所在 client 生效，5 分钟过期回退普通用户
```

- [ ] **Step 3: 全量测试回归**

Run: `pytest tests/ -v`
Expected: 全部 PASS（util 10 + config 5 + protocol 13 + permissions 10 + commands 8 + llm 10 + server 15 + irc_client 19 + xmpp_client 9 + main 4 = 103 passed）

- [ ] **Step 4: 提交**

```bash
git add configs/client_irc.yaml configs/client_xmpp.yaml README.md
git commit -m "docs(config): root_session_ttl 配置与 getroot 说明"
```

---

## 自审清单

**1. Spec 覆盖核查：**

| 设计文档要求 | 对应任务 |
|---|---|
| `runcmd getroot` 子命令形态 | Task 2（server 分支）+ Task 3/4（client 验证） |
| 目标 = 调用者所在 client | Task 2 `_cmd_runcmd` getroot 分支发 conn 自身 |
| su --pty 提权 | Task 3/4 `_exec_getroot`（`printf pw | su --pty root -c 'id -u'`） |
| 会话绑定 user@host、仅所在 client | Task 3/4 `root_sessions[caller]` |
| 5 分钟 TTL + 可配置 | Task 3/4 `_root_session_ttl()` 默认 300 + Task 5 配置 |
| 过期回退普通用户 + 提示 | Task 3/4 `root_expired` + Task 2 `_handle_runcmd_result` 提示 |
| 密码仅内存、不走 cmd、shlex.quote | Task 3/4 实现约束 |
| 仅 shellop 可调 | 继承 Task 2 权限检查（runcmd=shellop） |
| 协议变更 + 既有测试更新 | Task 1（含 test_protocol 兼容性更新） |

**2. 占位符扫描：** 所有步骤含完整代码与预期输出，无 TBD/TODO。

**3. 类型/签名一致性：** `make_runcmd(task_id, cmd, caller_userhost="")`、`make_getroot(task_id, password, caller_userhost)`、`make_runcmd_result(task_id, ok, output, as_root=False, root_expired=False)` 在 Task 1 定义后，Task 2-4 调用处签名一致；`_exec_getroot(msg)`/`_exec_runcmd(msg)` 两个 client 实现相同。

**4. 已知边界：**
- `_handle_runcmd_result` 在 root_expired 时将提示**拼接到结果输出**（单条私信），`_cmd_runcmd` 原样回传
- getroot 验证命令 `printf '%s\n' pw | su --pty root -c 'id -u'` 依赖 util-linux su（已实证支持 --pty 与管道 stdin）；非 util-linux 系统（如 macOS 的 su）不支持 --pty，需人工确认目标机
- `root_sessions` 过期条目保留（用于过期标记），进程重启自动清空
- Task 3/4 测试 mock `asyncio.create_subprocess_shell`——注意 monkeypatch 作用域为测试内，不影响其他测试
