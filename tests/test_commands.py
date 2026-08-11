from ircxmppbot.commands import COMMAND_LEVELS, parse_command


def test_parse_basic():
    p = parse_command("!qsdwindows_bot ban nick", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "ban"
    assert p.args == ["nick"]


def test_parse_case_insensitive_cmd():
    p = parse_command("!qsdwindows_bot BAN nick", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "ban"


def test_parse_no_args():
    p = parse_command("!qsdwindows_bot help", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "help"
    assert p.args == []


def test_parse_unknown_bot_name():
    assert parse_command("!other_bot help", "qsdwindows_bot") is None


def test_parse_prefix_only():
    assert parse_command("!qsdwindows_bot", "qsdwindows_bot") is None


def test_parse_not_command():
    assert parse_command("hello world", "qsdwindows_bot") is None


def test_parse_extra_whitespace():
    p = parse_command("  !qsdwindows_bot  chat  hello  world  ", "qsdwindows_bot")
    assert p is not None
    assert p.cmd == "chat"
    assert p.args == ["hello", "world"]


def test_command_levels_cover_all():
    assert COMMAND_LEVELS["runcmd"] == "shellop"
    assert COMMAND_LEVELS["botop"] == "oper"
    assert COMMAND_LEVELS["shellop"] == "oper"
    assert COMMAND_LEVELS["ban"] == "botop"
    assert COMMAND_LEVELS["chat"] == "everyone"
