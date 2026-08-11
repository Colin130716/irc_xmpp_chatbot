import asyncio
import time
from pathlib import Path

import pytest

from ircxmppbot.config import load_yaml
from ircxmppbot.protocol import (
    make_command,
    make_runcmd_result,
    make_vote,
)
from ircxmppbot.server import BotServer


class FakeConn:
    """记录 send 消息的假连接。"""

    def __init__(self, name="irc_main"):
        self.client_name = name
        self.client_type = "irc"
        self.bot_name = "qsdwindows_bot"
        self.sent: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


def _write_cfg(tmp_path: Path, **perms) -> Path:
    cfg = {
        "server": {
            "listen_host": "127.0.0.1",
            "listen_port": 8443,
            "token": "test-token",
            "oper_cache_ttl": 60,
            "shellop_confirm_timeout": 30,
        },
        "permissions": {
            "botop": ["op1@host.example", "op2@host.example"],
            "shellop": [],
            "whitelist": {"enabled": False, "entries": []},
            "blacklist": {"enabled": False, "entries": []},
            **perms,
        },
    }
    p = tmp_path / "server.yaml"
    p.write_text(_yaml(cfg), encoding="utf-8")
    return p


def _yaml(cfg: dict) -> str:
    import yaml
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


@pytest.fixture
async def srv(tmp_path: Path) -> BotServer:
    server = BotServer(_write_cfg(tmp_path))
    server.reload(load_yaml(server.config_path))
    return server


async def _cmd(srv: BotServer, conn: FakeConn, cmd: str, args: list[str],
               caller: str, is_oper: bool = False, channel: str = "#chan") -> None:
    await srv._handle_command(conn, make_command(cmd, args, caller, is_oper, channel))


def _last(conn: FakeConn) -> dict:
    return conn.sent[-1]


# ---------- 权限判定 ----------

async def test_command_permission_denied(srv, tmp_path):
    conn = FakeConn()
    await _cmd(srv, conn, "botop", ["give", "x@y"], "nobody@host.example")
    assert not _last(conn)["ok"]
    assert "权限不足" in _last(conn)["reply"]


async def test_botop_give_persists(srv, tmp_path):
    conn = FakeConn()
    srv.conns = {"irc_main": conn}  # 注册以便快照广播到达
    await _cmd(srv, conn, "botop", ["give", "op3@host.example"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    # 已写回配置文件
    cfg = load_yaml(srv.config_path)
    assert "op3@host.example" in cfg["permissions"]["botop"]
    # 快照已推送
    assert any(m["type"] == "permission_update" for m in conn.sent)


async def test_botop_give_rejects_non_oper(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "botop", ["give", "op3@host.example"], "op2@host.example")  # botop 非 oper
    assert not _last(conn)["ok"]


# ---------- 黑白名单互斥 ----------

async def test_whitelist_blacklist_mutex(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "whitelist", ["on"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    await _cmd(srv, conn, "blacklist", ["on"], "op1@host.example", is_oper=True)
    assert not _last(conn)["ok"]
    assert "不能同时启用" in _last(conn)["reply"]


async def test_whitelist_add_entry(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "whitelist", ["add", "good!*@*", "#chan1"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    assert srv.permissions.whitelist[0]["mask"] == "good!*@*"
    assert srv.permissions.whitelist[0]["channel"] == "#chan1"


# ---------- ban/unban 授权 + action ----------

async def test_ban_approves_with_action(srv):
    conn = FakeConn()
    await _cmd(srv, conn, "ban", ["baduser"], "op2@host.example", is_oper=True)
    msg = _last(conn)
    assert msg["ok"] and msg["action"] == "ban" and msg["action_args"] == ["baduser"]


# ---------- shellop 确认流程 ----------

async def test_shellop_add_requires_botop_and_oper(srv):
    conn = FakeConn()
    # 候选人在 botop 但 oper 缓存无记录 → 拒绝
    srv.oper_cache["op2@host.example"] = (False, time.monotonic())
    await _cmd(srv, conn, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    assert not _last(conn)["ok"]
    assert "oper" in _last(conn)["reply"]

    # 候选人不在 botop → 拒绝
    await _cmd(srv, conn, "shellop", ["add", "nobody@host.example"], "op1@host.example", is_oper=True)
    assert not _last(conn)["ok"]
    assert "botop" in _last(conn)["reply"]


async def test_shellop_full_confirm_flow(srv):
    conn = FakeConn()
    srv.conns = {"irc_main": conn}  # 注册以便广播到达
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    await _cmd(srv, conn, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    # 发起成功，提议已广播给 client
    proposal = next(m for m in conn.sent if m["type"] == "shellop_proposal")
    pid = proposal["proposal_id"]
    assert "op1@host.example" in proposal["voters"]
    assert "op2@host.example" in proposal["voters"]

    # 另一个投票人 op2 投 confirm → 全员同意（op1 自动同意 + op2）
    vote_conn = FakeConn("irc_other")
    await srv._handle_vote(vote_conn, make_vote(pid, "confirm", "op2@host.example"))
    assert "op2@host.example" in srv.permissions.shellop
    # 结果已私信发起者
    result = next(m for m in conn.sent if m["type"] == "command_result" and "通过" in m["reply"])
    assert result["target_type"] == "private"


async def test_shellop_reject_flow(srv):
    conn = FakeConn()
    srv.conns = {"irc_main": conn}  # 注册以便广播到达
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    await _cmd(srv, conn, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    pid = next(m for m in conn.sent if m["type"] == "shellop_proposal")["proposal_id"]

    vote_conn = FakeConn("irc_other")
    await srv._handle_vote(vote_conn, make_vote(pid, "reject", "op2@host.example"))
    assert "op2@host.example" not in srv.permissions.shellop
    result = next(m for m in conn.sent if m["type"] == "command_result" and "否决" in m["reply"])
    assert not result["ok"]


async def test_shellop_remove_direct(srv):
    srv.permissions.shellop.add("op1@host.example")
    conn = FakeConn()
    await _cmd(srv, conn, "shellop", ["remove", "op1@host.example"], "op1@host.example", is_oper=True)
    assert _last(conn)["ok"]
    assert "op1@host.example" not in srv.permissions.shellop


# ---------- runcmd 路由 ----------

async def test_runcmd_routes_to_target(srv):
    origin = FakeConn("irc_main")
    target = FakeConn("xmpp_main")
    srv.conns = {"irc_main": origin, "xmpp_main": target}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._handle_command(
            origin,
            make_command("runcmd", ["xmpp_main", "ls", "-la"], "boss@host.example", False, "#chan"),
        )
    )
    # 等待 runcmd 消息到达 target
    for _ in range(100):
        if any(m["type"] == "runcmd" for m in target.sent):
            break
        await asyncio.sleep(0.01)
    runcmd = next(m for m in target.sent if m["type"] == "runcmd")
    assert runcmd["cmd"] == "ls -la"

    await srv._handle_runcmd_result(target, make_runcmd_result(runcmd["task_id"], True, "total 8"))
    await task
    result = next(m for m in origin.sent if m["type"] == "command_result" and m["target_type"] == "private")
    assert result["reply"] == "total 8"


async def test_runcmd_target_offline(srv):
    conn = FakeConn()
    srv.permissions.shellop.add("boss@host.example")
    await _cmd(srv, conn, "runcmd", ["ghost", "ls"], "boss@host.example")
    assert not _last(conn)["ok"]
    assert "不在线" in _last(conn)["reply"]


# ---------- getroot 提权 ----------

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
        conn, make_runcmd_result(getroot["task_id"], True, "root 已激活（300 秒有效）", as_root=True)
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
    """root 会话过期 → 私信结果附加提示。"""
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
        target, make_runcmd_result(runcmd["task_id"], True, "uid=1000", root_expired=True)
    )
    await task
    result = next(m for m in origin.sent if m["type"] == "command_result" and m["target_type"] == "private")
    assert "root 会话已过期" in result["reply"]
    assert "uid=1000" in result["reply"]


# ---------- 审查回归测试 ----------

async def test_runcmd_result_rejects_wrong_sender(srv):
    """M4: runcmd_result 来自非目标 client 被拒绝。"""
    origin = FakeConn("irc_main")
    target = FakeConn("xmpp_main")
    intruder = FakeConn("evil_client")
    srv.conns = {"irc_main": origin, "xmpp_main": target, "evil_client": intruder}
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

    # 冒名 intruder 尝试 resolve——应被拒绝
    await srv._handle_runcmd_result(intruder, make_runcmd_result(runcmd["task_id"], True, "fake"))
    # 真正的 target 才能 resolve
    await srv._handle_runcmd_result(target, make_runcmd_result(runcmd["task_id"], True, "real"))
    await task
    result = next(m for m in origin.sent if m["type"] == "command_result" and m["target_type"] == "private")
    assert result["reply"] == "real"


async def test_dup_client_name_rejected(srv):
    """H2: 重名 client 注册被拒，且旧连接断开不误删新连接。"""
    from ircxmppbot.server import ClientConnection

    class FakeReaderWriter:
        def __init__(self):
            self.sent = b""

        async def write(self, data: bytes) -> None:
            self.sent += data

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    # 注册第一个
    conn1 = ClientConnection(None, FakeReaderWriter(), "irc_main", "irc", "bot")
    assert srv._try_register(conn1) is True
    # 重名第二个被拒
    conn2 = ClientConnection(None, FakeReaderWriter(), "irc_main", "irc", "bot")
    assert srv._try_register(conn2) is False
    # 字典仍是 conn1
    assert srv.conns["irc_main"] is conn1
    # 旧连接（conn1）断开：_drop_pending_for 不影响新条目（此处 conns 无新条目）
    srv._drop_pending_for(conn1)
    assert srv.conns.get("irc_main") is conn1
