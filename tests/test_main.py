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


def test_server_config_has_irc_or_xmpp():
    cfg = load_yaml(Path("configs/server.yaml"))
    # irc/xmpp 段可选：样例中至少有一个
    assert "irc" in cfg or "xmpp" in cfg


def test_protocol_auth_roundtrip():
    msg = make_auth("token", "n", "irc", "bot")
    assert decode_msg(encode_msg(msg).decode("utf-8")) == msg
