import asyncio
from pathlib import Path

import pytest

from ircxmppbot.config import load_yaml
from ircxmppbot.xmpp_client import XMPPBot, XMPP_LINE_LIMIT


def _write_cfg(tmp_path: Path, **llm_overrides) -> Path:
    cfg = {
        "client": {
            "name": "xmpp_main",
            "type": "xmpp",
            "bot_name": "qsdwindows_bot",
            "server": {
                "host": "127.0.0.1",
                "port": 8443,
                "token": "test-token",
                "tls": {"verify": False},
            },
        },
        "xmpp": {
            "jid": "bot@snikket.example",
            "password": "pw",
            "host": "snikket.example",
            "port": 5222,
            "tls": True,
            "mucs": ["room@conference.snikket.example"],
        },
        "llm": {
            "format": "openai_chat",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
            **llm_overrides,
        },
    }
    p = tmp_path / "client_xmpp.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture
def bot(tmp_path: Path) -> XMPPBot:
    b = XMPPBot(_write_cfg(tmp_path))
    b.boundjid = type("J", (), {"bare": "bot@snikket.example"})()
    return b


def test_line_limit_constant():
    assert XMPP_LINE_LIMIT == 1500


def test_bot_name(bot):
    assert bot.bot_name == "qsdwindows_bot"
    assert bot.nick == "qsdwindows_bot"


async def test_private_message_no_prefix_chats(bot, monkeypatch):
    sent = []

    async def fake_chat(text):
        assert text == "hello world"
        return "hi"

    def fake_send(mto, mbody, mtype):
        sent.append((mto, mbody, mtype))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "send_message", fake_send)
    await bot._handle_private("user@snikket.example", "hello world")
    assert sent == [("user@snikket.example", "hi", "chat")]


async def test_private_message_with_prefix_chat(bot, monkeypatch):
    sent = []

    async def fake_chat(text):
        assert text == "hi there"
        return "hello"

    def fake_send(mto, mbody, mtype):
        sent.append((mto, mbody, mtype))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "send_message", fake_send)
    await bot._handle_private("user@snikket.example", "!qsdwindows_bot chat hi there")
    assert sent == [("user@snikket.example", "hello", "chat")]


async def test_private_message_other_command_rejected(bot, monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda mto, mbody, mtype: sent.append((mto, mbody, mtype)))
    await bot._handle_private("user@snikket.example", "!qsdwindows_bot ban someone")
    assert sent and "仅支持 chat" in sent[0][1]


async def test_groupchat_only_chat_with_prefix(bot, monkeypatch):
    sent = []

    async def fake_chat(text):
        return "group reply"

    def fake_send(mto, mbody, mtype):
        sent.append((mto, mbody, mtype))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "send_message", fake_send)

    class FakeMsg:
        """模拟 slixmpp stanza：支持 .get() 与 [] 下标访问。"""

        def __init__(self, body, mucnick, bare):
            self._body = body
            self._mucnick = mucnick
            self._bare = bare

        def get(self, key, default=None):
            return {"body": self._body, "mucnick": self._mucnick}.get(key, default)

        def __getitem__(self, key):
            if key == "from":
                return type("F", (), {"bare": self._bare})()
            return {"body": self._body, "mucnick": self._mucnick}[key]

    bot._on_groupchat(FakeMsg("!qsdwindows_bot chat hello", "user1", "room@conference.snikket.example"))
    await asyncio.sleep(0.05)  # 事件处理器是异步任务
    assert sent == [("room@conference.snikket.example", "group reply", "groupchat")]


async def test_groupchat_without_prefix_ignored(bot, monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "send_message", lambda mto, mbody, mtype: sent.append((mto, mbody, mtype)))

    class FakeMsg:
        """模拟 slixmpp stanza：支持 .get() 与 [] 下标访问。"""

        def __init__(self, body, mucnick, bare):
            self._body = body
            self._mucnick = mucnick
            self._bare = bare

        def get(self, key, default=None):
            return {"body": self._body, "mucnick": self._mucnick}.get(key, default)

        def __getitem__(self, key):
            if key == "from":
                return type("F", (), {"bare": self._bare})()
            return {"body": self._body, "mucnick": self._mucnick}[key]

    bot._on_groupchat(FakeMsg("just talking", "user1", "room@conference.snikket.example"))
    await asyncio.sleep(0.05)
    assert sent == []


# ---------- getroot 提权 ----------

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
