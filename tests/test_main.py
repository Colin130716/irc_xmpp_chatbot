from pathlib import Path

from ircxmppbot.config import load_yaml
from ircxmppbot.protocol import decode_msg, encode_msg, make_auth


def test_sample_server_config_loads():
    cfg = load_yaml(Path("configs/server.yaml"))
    assert "server" in cfg and "permissions" in cfg
    assert "listen_port" in cfg["server"]
    assert "whitelist" in cfg["permissions"] and "blacklist" in cfg["permissions"]


def test_sample_irc_config_loads():
    cfg = load_yaml(Path("configs/client_irc.yaml"))
    assert cfg["client"]["type"] == "irc"
    assert cfg["client"]["bot_name"]
    assert "irc" in cfg and "llm" in cfg
    assert "realname" in cfg["irc"]
    assert cfg["irc"]["channels"]


def test_sample_xmpp_config_loads():
    cfg = load_yaml(Path("configs/client_xmpp.yaml"))
    assert cfg["client"]["type"] == "xmpp"
    assert "xmpp" in cfg and "llm" in cfg
    assert "jid" in cfg["xmpp"]


def test_protocol_auth_roundtrip():
    msg = make_auth("token", "n", "irc", "bot")
    assert decode_msg(encode_msg(msg).decode("utf-8")) == msg
