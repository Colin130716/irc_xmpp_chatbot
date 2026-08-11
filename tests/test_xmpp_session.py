import asyncio
from pathlib import Path

import pytest

from ircxmppbot.xmpp_session import XMPPSession, XMPP_LINE_LIMIT


class FakeServer:
    def __init__(self):
        self.calls = []
        self.llm = type("L", (), {"chat": None})()

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
    """私聊免前缀 → 委托 server 处理 chat。"""
    await session._handle_private("user@snikket.example", "hello world")
    assert session.srv.calls == [("chat", ["hello world"], "user@snikket.example", False, None)]


async def test_private_prefix_ban_rejected(session, monkeypatch):
    """带前缀命令 → 委托 server（server 判定 XMPP 仅 chat/help）。"""
    await session._handle_private("user@snikket.example", "!qsdwindows_bot ban someone")
    assert session.srv.calls == [("ban", ["someone"], "user@snikket.example", False, None)]
