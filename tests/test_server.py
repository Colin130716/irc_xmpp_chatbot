import asyncio
import time
from pathlib import Path

import pytest

from ircxmppbot.config import load_yaml
from ircxmppbot.protocol import make_runcmd_result
from ircxmppbot.server import BotServer


class FakeConn:
    """记录 send 消息的假连接。"""

    def __init__(self, name="shell_node_1"):
        self.client_name = name
        self.client_type = "shell"
        self.bot_name = "shell"
        self.sent: list[dict] = []

    async def send(self, msg: dict) -> None:
        self.sent.append(msg)


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


def _write_cfg(tmp_path: Path, **perms) -> Path:
    cfg = {
        "server": {
            "listen_host": "127.0.0.1",
            "listen_port": 8443,
            "token": "test-token",
            "oper_cache_ttl": 60,
            "shellop_confirm_timeout": 30,
        },
        "llm": {
            "format": "openai_chat",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
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


async def _cmd(srv: BotServer, target, cmd: str, args: list[str],
               caller: str, is_oper: bool = False, channel: str = "#chan") -> None:
    await srv.handle_chat_command(cmd, args, caller, is_oper, channel, target)


def _last(target: FakeTarget) -> tuple:
    return target.replied[-1]


# ---------- 权限判定（经 handle_chat_command） ----------

async def test_command_permission_denied(srv):
    target = FakeTarget()
    await _cmd(srv, target, "botop", ["give", "x@y"], "nobody@host.example")
    assert "权限不足" in _last(target)[0]


async def test_botop_give_persists(srv, tmp_path):
    target = FakeTarget()
    await _cmd(srv, target, "botop", ["give", "op3@host.example"], "op1@host.example", is_oper=True)
    assert _last(target)[0].startswith("已授予")
    cfg = load_yaml(srv.config_path)
    assert "op3@host.example" in cfg["permissions"]["botop"]


async def test_botop_give_rejects_non_oper(srv):
    target = FakeTarget()
    await _cmd(srv, target, "botop", ["give", "op3@host.example"], "op2@host.example")  # botop 非 oper
    assert "权限不足" in _last(target)[0]


# ---------- 黑白名单互斥 ----------

async def test_whitelist_blacklist_mutex(srv):
    target = FakeTarget()
    await _cmd(srv, target, "whitelist", ["on"], "op1@host.example", is_oper=True)
    assert "已启用" in _last(target)[0]
    await _cmd(srv, target, "blacklist", ["on"], "op1@host.example", is_oper=True)
    assert "不能同时启用" in _last(target)[0]


async def test_whitelist_add_entry(srv):
    target = FakeTarget()
    await _cmd(srv, target, "whitelist", ["add", "good!*@*", "#chan1"], "op1@host.example", is_oper=True)
    assert _last(target)[0].startswith("已添加")
    assert srv.permissions.whitelist[0]["mask"] == "good!*@*"
    assert srv.permissions.whitelist[0]["channel"] == "#chan1"


# ---------- ban/unban 经 execute ----------

async def test_ban_approves_with_action(srv):
    target = FakeTarget()
    await _cmd(srv, target, "ban", ["baduser"], "op2@host.example", is_oper=True)
    assert target.executed and target.executed[0][0] == "ban"
    assert target.executed[0][1] == ["baduser"]


# ---------- chat 命令（LLM） ----------

async def test_cmd_chat_uses_llm(srv):
    target = FakeTarget()

    class FakeLLM:
        async def chat(self, text):
            return "hello back"

    srv.llm = FakeLLM()
    await _cmd(srv, target, "chat", ["hello"], "u@h")
    assert _last(target)[0] == "hello back"


# ---------- shellop 确认流程（dm_voters） ----------

async def test_shellop_add_requires_botop_and_oper(srv):
    target = FakeTarget()
    srv.oper_cache["op2@host.example"] = (False, time.monotonic())
    await _cmd(srv, target, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    assert "oper" in _last(target)[0]

    await _cmd(srv, target, "shellop", ["add", "nobody@host.example"], "op1@host.example", is_oper=True)
    assert "botop" in _last(target)[0]


async def test_shellop_full_confirm_flow(srv):
    target = FakeTarget()
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    await _cmd(srv, target, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    # 应调用 dm_voters 收集可达投票人
    assert target.dm_reachable
    prop_id, candidate, voters = target.dm_reachable[0]
    assert candidate == "op2@host.example"
    assert "op1@host.example" in voters and "op2@host.example" in voters
    # 提议存在且 votes 含发起者
    assert len(srv.proposals) == 1
    prop = next(iter(srv.proposals.values()))
    assert "op1@host.example" in prop["votes"]


async def test_shellop_remove_direct(srv):
    srv.permissions.shellop.add("op1@host.example")
    target = FakeTarget()
    await _cmd(srv, target, "shellop", ["remove", "op1@host.example"], "op1@host.example", is_oper=True)
    assert "已移除" in _last(target)[0]
    assert "op1@host.example" not in srv.permissions.shellop


# ---------- runcmd 路由（跨 client） ----------

async def test_runcmd_routes_to_target(srv):
    origin = FakeTarget("irc_main")
    target = FakeConn("shell_node_1")
    srv.conns = {"shell_node_1": target}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._cmd_runcmd(origin, ["shell_node_1", "ls", "-la"], "boss@host.example", False, "#chan")
    )
    for _ in range(100):
        if any(m["type"] == "runcmd" for m in target.sent):
            break
        await asyncio.sleep(0.01)
    runcmd = next(m for m in target.sent if m["type"] == "runcmd")
    assert runcmd["cmd"] == "ls -la"
    assert runcmd["caller_userhost"] == "boss@host.example"

    await srv._handle_runcmd_result(target, make_runcmd_result(runcmd["task_id"], True, "total 8"))
    await task
    assert any("total 8" in r[0] for r in origin.replied)


async def test_runcmd_target_offline(srv):
    target = FakeTarget()
    srv.permissions.shellop.add("boss@host.example")
    await _cmd(srv, target, "runcmd", ["ghost", "ls"], "boss@host.example")
    assert "不在线" in _last(target)[0]


async def test_runcmd_result_rejects_wrong_sender(srv):
    origin = FakeTarget("irc_main")
    target = FakeConn("shell_node_1")
    intruder = FakeConn("evil_client")
    srv.conns = {"shell_node_1": target, "evil_client": intruder}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._cmd_runcmd(origin, ["shell_node_1", "id"], "boss@host.example", False, "#chan")
    )
    for _ in range(100):
        if any(m["type"] == "runcmd" for m in target.sent):
            break
        await asyncio.sleep(0.01)
    runcmd = next(m for m in target.sent if m["type"] == "runcmd")

    await srv._handle_runcmd_result(intruder, make_runcmd_result(runcmd["task_id"], True, "fake"))
    await srv._handle_runcmd_result(target, make_runcmd_result(runcmd["task_id"], True, "real"))
    await task
    assert any("real" in r[0] for r in origin.replied)


# ---------- getroot 路由 ----------

async def test_runcmd_getroot_routes_to_client(srv):
    target = FakeTarget("irc_main")
    client_conn = FakeConn("shell_node_1")
    srv.conns = {"shell_node_1": client_conn}
    srv.permissions.shellop.add("boss@host.example")

    task = asyncio.create_task(
        srv._cmd_runcmd(target, ["getroot", "shell_node_1", "secretpw"], "boss@host.example", False, "#chan")
    )
    for _ in range(100):
        if any(m["type"] == "getroot" for m in client_conn.sent):
            break
        await asyncio.sleep(0.01)
    getroot = next(m for m in client_conn.sent if m["type"] == "getroot")
    assert getroot["password"] == "secretpw"
    assert getroot["caller_userhost"] == "boss@host.example"

    await srv._handle_runcmd_result(
        client_conn, make_runcmd_result(getroot["task_id"], True, "root 已激活（300 秒有效）", as_root=True)
    )
    await task
    assert any("root 已激活" in r[0] for r in target.replied)


# ---------- XMPP 限制 ----------

async def test_handle_chat_command_xmpp_restricted(srv):
    target = FakeTarget()
    target.session_type = "xmpp"
    await srv.handle_chat_command("ban", ["x"], "user@snikket.example", False, None, target)
    assert "仅支持 chat" in _last(target)[0]


# ---------- 复审回归：confirm/reject 端到端投票 ----------

async def test_confirm_vote_completes_proposal(srv):
    """R1: confirm 命令驱动投票 → 全员同意 → shellop 生效。"""
    target = FakeTarget()
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    srv.permissions.botop.add("op2@host.example")
    await _cmd(srv, target, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    assert target.dm_reachable, "应调用 dm_voters"
    pid = next(iter(srv.proposals))  # 唯一提议
    prop = srv.proposals[pid]
    # 发起者 op1 自动同意；voters 定稿为可达集合
    assert "op1@host.example" in prop["votes"]

    # op2 用 confirm 命令投票 → 全员同意 → shellop 生效
    await _cmd(srv, target, "confirm", [pid], "op2@host.example", is_oper=True)
    assert "op2@host.example" in srv.permissions.shellop
    assert pid not in srv.proposals  # 提议已结束


async def test_reject_vote_denies_proposal(srv):
    """R1: reject 命令投票 → 提议否决。"""
    target = FakeTarget()
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    srv.permissions.botop.add("op2@host.example")
    await _cmd(srv, target, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    pid = next(iter(srv.proposals))

    await _cmd(srv, target, "reject", [pid], "op2@host.example", is_oper=True)
    assert "op2@host.example" not in srv.permissions.shellop
    assert pid not in srv.proposals  # 提议已否决并结束


async def test_confirm_by_non_voter_rejected(srv):
    """R1: 非投票人的 confirm 被拒。"""
    target = FakeTarget()
    srv.oper_cache["op2@host.example"] = (True, time.monotonic())
    srv.permissions.botop.add("op2@host.example")
    await _cmd(srv, target, "shellop", ["add", "op2@host.example"], "op1@host.example", is_oper=True)
    pid = next(iter(srv.proposals))
    prop = srv.proposals[pid]
    # 把 op2 从可达名单移除（模拟 op2 不可达）
    prop["voters"] = {"op1@host.example"}

    # 非投票人 stranger 尝试 confirm → 拒绝
    await _cmd(srv, target, "confirm", [pid], "stranger@host.example", is_oper=True)
    assert "你不是该提议的投票人" in target.replied[-1][0]
    assert pid in srv.proposals  # 提议未被影响


async def test_chat_private_reply_uses_caller(srv):
    """R2/R3: chat 私信（channel=None）回复到 caller。"""
    target = FakeTarget()

    class FakeLLM:
        async def chat(self, text):
            return "hello back"

    srv.llm = FakeLLM()
    await srv.handle_chat_command("chat", ["hi"], "user@host.example", False, None, target)
    # 私信：channel=None → private_to=caller
    assert target.replied and target.replied[-1] == ("hello back", None, "user@host.example")


async def test_getroot_requires_client_name(srv):
    """M1: getroot 缺 client_name 时报用法。"""
    target = FakeTarget()
    srv.permissions.shellop.add("boss@host.example")
    await _cmd(srv, target, "runcmd", ["getroot", "secretpw"], "boss@host.example")
    assert "用法" in _last(target)[0]
