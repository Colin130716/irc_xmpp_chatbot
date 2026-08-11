import pytest

from ircxmppbot.config import ConfigError
from ircxmppbot.permissions import LEVELS, Permissions


def _cfg(**overrides):
    base = {
        "permissions": {
            "botop": ["op1@host.example", "op2@host.example"],
            "shellop": ["op1@host.example"],
            "whitelist": {"enabled": False, "entries": []},
            "blacklist": {"enabled": False, "entries": []},
        }
    }
    base["permissions"].update(overrides)
    return base


def test_from_config_basic():
    p = Permissions.from_config(_cfg())
    assert p.is_botop("op1@host.example")
    assert not p.is_botop("nobody@host.example")
    assert p.is_shellop("OP1@HOST.EXAMPLE")  # 大小写不敏感


def test_mutex_both_enabled_raises():
    with pytest.raises(ConfigError):
        Permissions.from_config(
            _cfg(whitelist={"enabled": True, "entries": []},
                 blacklist={"enabled": True, "entries": []})
        )


def test_level_ordering():
    p = Permissions.from_config(_cfg())
    assert LEVELS["everyone"] < LEVELS["botop"] < LEVELS["oper"] < LEVELS["shellop"]
    assert p.level_of("op1@host.example") == "shellop"
    assert p.level_of("op2@host.example") == "botop"
    assert p.level_of("unknown@host.example") == "everyone"
    assert p.level_of("op2@host.example", is_oper=True) == "oper"


def test_has_level():
    p = Permissions.from_config(_cfg())
    assert p.has_level("op1@host.example", "botop")
    assert p.has_level("op1@host.example", "shellop")
    assert not p.has_level("op2@host.example", "shellop")
    assert p.has_level("op2@host.example", "oper", is_oper=True)
    assert not p.has_level("everyone@host.example", "botop")


def test_whitelist_disabled_allows_all():
    p = Permissions.from_config(_cfg())
    assert p.whitelist_allows("any!u@h", "#chan")


def test_whitelist_channel_scoped():
    p = Permissions.from_config(
        _cfg(whitelist={"enabled": True, "entries": [
            {"mask": "good!*@*", "channel": "#chan1"},
        ]})
    )
    assert p.whitelist_allows("good!u@h", "#chan1")
    assert not p.whitelist_allows("good!u@h", "#chan2")  # 频道不符
    assert not p.whitelist_allows("bad!u@h", "#chan1")   # 掩码不符


def test_whitelist_global_entry():
    p = Permissions.from_config(
        _cfg(whitelist={"enabled": True, "entries": [{"mask": "good!*@*"}]})
    )
    assert p.whitelist_allows("good!u@h", "#any")


def test_blacklist_blocks():
    p = Permissions.from_config(
        _cfg(blacklist={"enabled": True, "entries": [{"mask": "bad!*@*"}]})
    )
    assert p.blacklist_blocks("bad!u@h", "#chan")
    assert not p.blacklist_blocks("good!u@h", "#chan")
    p2 = Permissions.from_config(_cfg())
    assert not p2.blacklist_blocks("bad!u@h", "#chan")  # 禁用时恒 False


def test_config_dict_roundtrip():
    p = Permissions.from_config(_cfg())
    p.botop.add("new@host.example")
    p2 = Permissions.from_config({"permissions": p.to_config_dict()})
    assert p2.is_botop("new@host.example")


def test_apply_snapshot():
    p = Permissions()
    p.apply_snapshot({
        "botop": ["a@h"], "shellop": [],
        "whitelist": {"enabled": True, "entries": [{"mask": "m!*@*"}]},
        "blacklist": {"enabled": False, "entries": []},
    })
    assert p.is_botop("a@h")
    assert p.whitelist_enabled


def test_whitelist_channel_case_insensitive():
    p = Permissions.from_config(
        _cfg(whitelist={"enabled": True, "entries": [
            {"mask": "good!*@*", "channel": "#Chan1"},
        ]})
    )
    assert p.whitelist_allows("good!u@h", "#chan1")
    assert not p.whitelist_allows("good!u@h", "#chan2")
