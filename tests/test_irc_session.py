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
    async def fake_whois(nick):
        return {"username": "op1", "hostname": "host.example", "oper": True}

    monkeypatch.setattr(session, "whois", fake_whois)
    await session.on_channel_message("#chan1", "op1", "!qsdwindows_bot info")
    assert session.server.calls == [("info", [], "op1@host.example", True, "#chan1")]


async def test_dm_voters_returns_reachable(session, monkeypatch):
    sent = []

    async def fake_message(nick, text):
        sent.append((nick, text))

    monkeypatch.setattr(session, "message", fake_message)
    reachable = await session.dm_voters(
        "ab12", "op2@host.example", ["op1@host.example", "op2@host.example", "ghost@host.example"], 123.0
    )
    assert set(reachable) == {"op1@host.example", "op2@host.example"}
    assert len(sent) == 2
