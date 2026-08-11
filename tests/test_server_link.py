import asyncio
import time
from pathlib import Path

import pytest

from ircxmppbot.server_link import ServerLink


def _write_cfg(tmp_path: Path) -> Path:
    cfg = {
        "client": {
            "name": "irc_main",
            "type": "irc",
            "bot_name": "qsdwindows_bot",
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


@pytest.fixture
def link(tmp_path: Path) -> ServerLink:
    from ircxmppbot.config import load_yaml
    cfg = load_yaml(_write_cfg(tmp_path))
    return ServerLink(cfg, "irc", "qsdwindows_bot")


def test_root_session_ttl_default(link):
    assert link._root_session_ttl() == 300.0


def test_root_session_lookup_missing(link):
    entry, expired = link._root_session_lookup("nobody@host.example")
    assert entry is None and expired is False


def test_root_session_lookup_valid(link):
    link.root_sessions["op@host.example"] = ("pw", time.monotonic() + 100)
    entry, expired = link._root_session_lookup("op@host.example")
    assert entry == ("pw", pytest.approx(time.monotonic() + 100, abs=200))
    assert expired is False


def test_root_session_lookup_expired_prunes(link):
    link.root_sessions["op@host.example"] = ("pw", time.monotonic() - 1)
    entry, expired = link._root_session_lookup("op@host.example")
    assert entry is None and expired is True
    assert "op@host.example" not in link.root_sessions  # 已清理


async def test_exec_getroot_success(link, monkeypatch):
    sent = []

    async def fake_send(msg):
        sent.append(msg)

    monkeypatch.setattr(link, "send", fake_send)

    class FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            return (b"0\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        assert args[:5] == ("su", "--pty", "root", "-c", "id -u")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    await link.exec_getroot({"task_id": "t1", "password": "pw", "caller_userhost": "boss@host.example"})
    assert sent and sent[0]["as_root"] is True
    assert "boss@host.example" in link.root_sessions


async def test_exec_getroot_wrong_password(link, monkeypatch):
    sent = []

    async def fake_send(msg):
        sent.append(msg)

    monkeypatch.setattr(link, "send", fake_send)

    class FakeProc:
        returncode = 1

        async def communicate(self, input=None):
            return (b"Authentication failure\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    await link.exec_getroot({"task_id": "t1", "password": "bad", "caller_userhost": "boss@host.example"})
    assert sent and sent[0]["ok"] is False
    assert "boss@host.example" not in link.root_sessions


async def test_exec_runcmd_as_root(link, monkeypatch):
    link.root_sessions["boss@host.example"] = ("pw", time.monotonic() + 300)
    sent = []
    commands = []

    async def fake_send(msg):
        sent.append(msg)

    monkeypatch.setattr(link, "send", fake_send)

    class FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            return (b"uid=0(root)\n", b"")

    async def fake_create_subprocess_exec(*args, **kwargs):
        commands.append(args)
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    await link.exec_runcmd({"task_id": "t1", "cmd": "id", "caller_userhost": "boss@host.example"})
    assert commands and commands[0][:5] == ("su", "--pty", "root", "-c", "id")
    assert sent and sent[0]["as_root"] is True


async def test_exec_runcmd_no_session(link, monkeypatch):
    sent = []

    async def fake_send(msg):
        sent.append(msg)

    monkeypatch.setattr(link, "send", fake_send)

    class FakeProc:
        returncode = 0

        async def communicate(self, input=None):
            return (b"uid=1000\n", b"")

    async def fake_create_subprocess_shell(cmd, **kwargs):
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_create_subprocess_shell)
    await link.exec_runcmd({"task_id": "t1", "cmd": "id", "caller_userhost": "nobody@host.example"})
    assert sent and sent[0]["as_root"] is False and sent[0]["root_expired"] is False


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
