from ircxmppbot.util import (
    backoff_delay, match_mask, parse_userhost, split_text, split_text_bytes, tls_enabled,
)


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


def test_split_text_bytes_ascii():
    assert split_text_bytes("abcdef", 2) == ["ab", "cd", "ef"]


def test_split_text_bytes_utf8_not_split():
    # 中文 3 字节/字：limit=12 正好 4 个字
    assert split_text_bytes("中文测试", 12) == ["中文测试"]
    # limit=9 放不下第 4 个字（3*4=12>9），回退到 3 个字
    assert split_text_bytes("中文测试", 9) == ["中文测", "试"]
    assert all(len(c.encode("utf-8")) <= 9 for c in split_text_bytes("中文测试", 9))


def test_split_text_bytes_mixed():
    chunks = split_text_bytes("ab中文cd", 5)
    assert all(len(c.encode("utf-8")) <= 5 for c in chunks)
    assert "".join(chunks) == "ab中文cd"


def test_split_text_bytes_empty():
    assert split_text_bytes("", 10) == []


def test_tls_enabled_missing_section():
    assert tls_enabled(None) is False
    assert tls_enabled({}) is False


def test_tls_enabled_section_default_true():
    assert tls_enabled({"verify": False}) is True


def test_tls_enabled_explicit_false():
    assert tls_enabled({"enabled": False}) is False


def test_tls_enabled_explicit_true():
    assert tls_enabled({"enabled": True, "verify": False}) is True
