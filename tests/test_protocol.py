import pytest

from ircxmppbot.protocol import (
    ProtocolError,
    decode_msg,
    encode_msg,
    make_auth,
    make_command,
    make_command_result,
    make_getroot,
    make_permission_update,
    make_reachability,
    make_runcmd,
    make_runcmd_result,
    make_shellop_proposal,
    make_vote,
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


def test_make_command_shape():
    assert make_command("ban", ["nick"], "u@h", True, "#chan") == {
        "type": "command", "cmd": "ban", "args": ["nick"],
        "caller_userhost": "u@h", "caller_is_oper": True, "channel": "#chan",
    }


def test_make_command_result_shape():
    assert make_command_result(True, "ok", "channel", "#chan", "ban", ["nick"]) == {
        "type": "command_result", "ok": True, "reply": "ok",
        "target_type": "channel", "target": "#chan",
        "action": "ban", "action_args": ["nick"],
    }
    # 新字段默认值
    assert make_command_result(False, "err", "private", "u@h") == {
        "type": "command_result", "ok": False, "reply": "err",
        "target_type": "private", "target": "u@h",
        "action": "none", "action_args": [],
    }


def test_make_shellop_proposal_shape():
    assert make_shellop_proposal("ab12", "u@h", 123.0, ["a@x", "b@y"]) == {
        "type": "shellop_proposal", "proposal_id": "ab12", "candidate": "u@h",
        "deadline_ts": 123.0, "voters": ["a@x", "b@y"],
    }


def test_make_permission_update_shape():
    assert make_permission_update({"botop": []}, {"u@h": True}) == {
        "type": "permission_update", "botop": [], "oper_cache": {"u@h": True},
    }


def test_make_vote_and_runcmd_shape():
    assert make_vote("ab12", "confirm", "u@h") == {
        "type": "vote", "proposal_id": "ab12", "vote": "confirm", "voter_userhost": "u@h",
    }
    # runcmd 新增 caller_userhost 字段
    assert make_runcmd("t1", "ls -la", "u@h") == {
        "type": "runcmd", "task_id": "t1", "cmd": "ls -la", "caller_userhost": "u@h",
    }
    assert make_runcmd("t2", "id") == {
        "type": "runcmd", "task_id": "t2", "cmd": "id", "caller_userhost": "",
    }
    # runcmd_result 新增 as_root/root_expired 字段
    assert make_runcmd_result("t1", True, "out", as_root=True) == {
        "type": "runcmd_result", "task_id": "t1", "ok": True, "output": "out",
        "as_root": True, "root_expired": False,
    }
    assert make_runcmd_result("t2", False, "", root_expired=True) == {
        "type": "runcmd_result", "task_id": "t2", "ok": False, "output": "",
        "as_root": False, "root_expired": True,
    }


def test_make_getroot_shape():
    assert make_getroot("t1", "secret", "u@h") == {
        "type": "getroot", "task_id": "t1", "password": "secret", "caller_userhost": "u@h",
    }


def test_make_reachability_shape():
    assert make_reachability("ab12", ["op1@h", "op2@h"]) == {
        "type": "reachability", "proposal_id": "ab12", "reachable": ["op1@h", "op2@h"],
    }
