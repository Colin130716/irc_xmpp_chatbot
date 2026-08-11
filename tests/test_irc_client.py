from pathlib import Path

import pytest

from ircxmppbot.config import load_yaml
from ircxmppbot.irc_client import IRCBot, IRC_LINE_LIMIT


def _write_cfg(tmp_path: Path, **llm_overrides) -> Path:
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
        "irc": {
            "host": "irc.example.com",
            "port": 6697,
            "tls": True,
            "realname": "My IRC Bot",
            "channels": ["#chan1"],
        },
        "llm": {
            "format": "openai_chat",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
            "model": "gpt-4o-mini",
            "max_reply_chars": 1500,
            **llm_overrides,
        },
    }
    p = tmp_path / "client_irc.yaml"
    import yaml
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


@pytest.fixture
def bot(tmp_path: Path) -> IRCBot:
    b = IRCBot(_write_cfg(tmp_path))
    b.users = {
        "op1": {"nickname": "op1", "username": "op1", "hostname": "host.example"},
        "op2": {"nickname": "op2", "username": "op2", "hostname": "host.example"},
    }
    b.channels = {"#chan1": {"users": {"op1", "op2"}}}
    return b


def test_line_limit_constant():
    assert IRC_LINE_LIMIT == 400


def test_bot_name_from_config(bot):
    assert bot.bot_name == "qsdwindows_bot"
    # pydle 未连接时 self.nickname 为 "<unregistered>"，配置的昵称存于 _nicknames[0]
    assert bot._nicknames[0] == "qsdwindows_bot"


def test_nick_for_userhost(bot):
    assert bot._nick_for_userhost("op1@host.example") == "op1"
    assert bot._nick_for_userhost("ghost@host.example") is None


def test_ban_mask_known_user(bot):
    assert bot._ban_mask("op1") == "op1@host.example"


def test_ban_mask_unknown_user(bot):
    assert bot._ban_mask("ghost") == "ghost!*@*"


async def test_maybe_command_chat_local(bot, monkeypatch):
    """chat 不经过 server，直接在本地调 LLM。"""
    sent = []

    async def fake_chat(text):
        return "回复内容"

    async def fake_message(target, msg):
        sent.append((target, msg))

    monkeypatch.setattr(bot.llm, "chat", fake_chat)
    monkeypatch.setattr(bot, "message", fake_message)

    await bot._maybe_command("!qsdwindows_bot chat 你好", "user1", "#chan1", is_channel=True)
    assert sent, "应发送 LLM 回复"
    assert sent[0][1] == "回复内容"


async def test_maybe_command_help_local(bot, monkeypatch):
    sent = []

    async def fake_message(target, msg):
        sent.append((target, msg))

    monkeypatch.setattr(bot, "message", fake_message)
    await bot._maybe_command("!qsdwindows_bot help", "user1", "#chan1", is_channel=True)
    assert sent and "chat" in sent[0][1]


async def test_maybe_command_unknown_goes_to_server(bot, monkeypatch):
    """非 chat/help 命令上报 server（不本地处理）。"""
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    async def fake_whois(nick):
        return {"username": "user1", "hostname": "host.example", "oper": False}

    monkeypatch.setattr(bot, "_server_send", fake_server_send)
    monkeypatch.setattr(bot, "_whois_user", fake_whois)
    await bot._maybe_command("!qsdwindows_bot botop give a@b", "user1", "#chan1", is_channel=True)
    assert sent and sent[0]["type"] == "command"
    assert sent[0]["caller_userhost"] == "user1@host.example"


async def test_confirm_routes_as_vote(bot, monkeypatch):
    """confirm/reject 走 vote 消息而非 command（server 端无 _cmd_confirm 处理器）。"""
    sent = []

    async def fake_server_send(msg):
        sent.append(msg)

    async def fake_whois(nick):
        return {"username": "op1", "hostname": "host.example", "oper": True}

    monkeypatch.setattr(bot, "_server_send", fake_server_send)
    monkeypatch.setattr(bot, "_whois_user", fake_whois)
    await bot._maybe_command("!qsdwindows_bot confirm ab12cd34", "op1", "#chan1", is_channel=True)
    assert sent and sent[0]["type"] == "vote"
    assert sent[0]["vote"] == "confirm"
    assert sent[0]["proposal_id"] == "ab12cd34"
    assert sent[0]["voter_userhost"] == "op1@host.example"


async def test_maybe_command_not_for_bot(bot, monkeypatch):
    sent = []
    monkeypatch.setattr(bot, "message", lambda target, msg: sent.append((target, msg)))
    monkeypatch.setattr(bot, "_server_send", lambda msg: sent.append(msg))
    await bot._maybe_command("hello there", "user1", "#chan1", is_channel=True)
    assert sent == []


async def test_enforce_whitelist_kicks_non_allowed(bot, monkeypatch):
    bot.permissions.apply_snapshot({
        "botop": ["op1@host.example"],
        "shellop": [],
        "whitelist": {"enabled": True, "entries": [{"mask": "op1!*@*"}]},
        "blacklist": {"enabled": False, "entries": []},
    })
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)

    # op1 在白名单 → 不踢
    await bot._enforce_lists("#chan1", "op1")
    assert kicked == []
    # op2 不在白名单且不是 botop/oper → 踢
    await bot._enforce_lists("#chan1", "op2")
    assert kicked and kicked[0][1] == "op2"


async def test_enforce_whitelist_botop_exempt(bot, monkeypatch):
    bot.permissions.apply_snapshot({
        "botop": ["op2@host.example"],
        "shellop": [],
        "whitelist": {"enabled": True, "entries": []},
        "blacklist": {"enabled": False, "entries": []},
    })
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)
    await bot._enforce_lists("#chan1", "op2")
    assert kicked == []  # botop 豁免


async def test_enforce_blacklist_kicks(bot, monkeypatch):
    bot.permissions.apply_snapshot({
        "botop": [],
        "shellop": [],
        "whitelist": {"enabled": False, "entries": []},
        "blacklist": {"enabled": True, "entries": [{"mask": "op2!*@*"}]},
    })
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)
    await bot._enforce_lists("#chan1", "op2")
    assert kicked and kicked[0][1] == "op2"


async def test_no_lists_no_kick(bot, monkeypatch):
    kicked = []

    async def fake_kick(channel, nick, reason=None):
        kicked.append((channel, nick, reason))
    monkeypatch.setattr(bot, "kick", fake_kick)
    await bot._enforce_lists("#chan1", "op2")
    assert kicked == []
