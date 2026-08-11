import pytest

from ircxmppbot.protocol import (
    ProtocolError,
    decode_msg,
    encode_msg,
    make_auth,
    make_getroot,
    make_runcmd,
    make_runcmd_result,
)


def test_encode_decode_roundtrip_unicode():
    msg = {"type": "auth", "token": "密钥🔑", "client_name": "irc_main"}
    line = encode_msg(msg).decode("utf-8")
    assert line.endswith("\n")
    assert decode_msg(line) == msg


def test_encode_missing_type():
    with pytest.raises(ProtocolError):
        encode_msg({"token": "x"})


def test_encode_unknown_type():
    with pytest.raises(ProtocolError):
        encode_msg({"type": "nope"})


def test_decode_empty_line():
    with pytest.raises(ProtocolError):
        decode_msg("   \n")


def test_decode_bad_json():
    with pytest.raises(ProtocolError):
        decode_msg("{not json")


def test_decode_unknown_type():
    with pytest.raises(ProtocolError):
        decode_msg('{"type": "wat"}')


def test_make_auth_shape():
    assert make_auth("t", "n", "irc", "bot") == {
        "type": "auth", "token": "t", "client_name": "n",
        "client_type": "irc", "bot_name": "bot",
    }


def test_make_runcmd_shape():
    assert make_runcmd("t1", "ls -la", "u@h") == {
        "type": "runcmd", "task_id": "t1", "cmd": "ls -la", "caller_userhost": "u@h",
    }


def test_make_runcmd_result_shape():
    assert make_runcmd_result("t1", True, "out", as_root=True, root_expired=False) == {
        "type": "runcmd_result", "task_id": "t1", "ok": True, "output": "out",
        "as_root": True, "root_expired": False,
    }


def test_make_getroot_shape():
    assert make_getroot("t1", "secret", "u@h") == {
        "type": "getroot", "task_id": "t1", "password": "secret", "caller_userhost": "u@h",
    }
