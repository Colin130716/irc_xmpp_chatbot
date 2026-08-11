from ircxmppbot.util import backoff_delay, match_mask, parse_userhost, split_text


def test_split_text_short_unchanged():
    assert split_text("hello", 10) == ["hello"]


def test_split_text_empty():
    assert split_text("", 10) == []


def test_split_text_chunks():
    assert split_text("abcdef", 2) == ["ab", "cd", "ef"]


def test_split_text_exact_multiple():
    assert split_text("abcd", 4) == ["abcd"]


def test_parse_userhost_full_mask():
    assert parse_userhost("Nick!user@host.example") == "user@host.example"


def test_parse_userhost_bare():
    assert parse_userhost("user@host.example") == "user@host.example"


def test_parse_userhost_lowercases():
    assert parse_userhost("Nick!User@Host.Example") == "user@host.example"


def test_match_mask_wildcards():
    # mask "user!*@*.example" 匹配 nick 为 user 的 hostmask
    assert match_mask("user!alice@host.example", "user!*@*.example")
    assert not match_mask("nick!user@host.example", "user!*@*.example")


def test_backoff_delay_sequence():
    assert backoff_delay(0) == 1.0
    assert backoff_delay(1) == 2.0
    assert backoff_delay(5) == 32.0
    assert backoff_delay(6) == 60.0  # 2**6=64 超过默认 cap 60，被截断


def test_backoff_delay_cap():
    assert backoff_delay(10, cap=60.0) == 60.0
