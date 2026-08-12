import pytest

from ircxmppbot.llm import LLMClient, LLMError

CFG = {
    "format": "openai_chat",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-test",
    "model": "gpt-4o-mini",
    "system_prompt": "You are a bot.",
    "temperature": 0.5,
    "max_tokens": 100,
    "timeout": 30,
}


def test_build_openai_chat():
    client = LLMClient(CFG)
    url, headers, body = client.build_request("hello")
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers == {"Authorization": "Bearer sk-test"}
    assert body["model"] == "gpt-4o-mini"
    assert body["messages"][0] == {"role": "system", "content": "You are a bot."}
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    assert body["temperature"] == 0.5 and body["max_tokens"] == 100


def test_build_openai_responses():
    client = LLMClient({**CFG, "format": "openai_responses"})
    url, headers, body = client.build_request("hello")
    assert url == "https://api.openai.com/v1/responses"
    assert body["input"] == "hello"
    assert "messages" not in body


def test_build_anthropic():
    client = LLMClient({**CFG, "format": "anthropic"})
    url, headers, body = client.build_request("hello")
    assert url == "https://api.openai.com/v1/messages"
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "You are a bot."
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_invalid_format():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "format": "nope"})


def test_missing_api_key():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "api_key": ""})


def test_extract_openai_chat():
    data = {"choices": [{"message": {"content": "hi"}}]}
    assert LLMClient.extract_reply("openai_chat", data) == "hi"


def test_extract_openai_responses():
    data = {"output": [
        {"type": "message", "content": [{"type": "output_text", "text": "a"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "b"}]},
    ]}
    assert LLMClient.extract_reply("openai_responses", data) == "a\nb"


def test_extract_anthropic():
    data = {"content": [{"type": "text", "text": "ok"}]}
    assert LLMClient.extract_reply("anthropic", data) == "ok"


async def test_chat_http_error(monkeypatch):
    client = LLMClient(CFG)

    class FakeResp:
        status_code = 500
        text = "boom"

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    with pytest.raises(LLMError):
        await client.chat("hello")


async def test_chat_ok(monkeypatch):
    client = LLMClient(CFG)

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "hi"}}]}

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return FakeResp()

    monkeypatch.setattr("httpx.AsyncClient", FakeClient)
    assert await client.chat("hello") == "hi"


# ---------- 思考开关与深度配置 ----------

CFG_THINK = {
    **CFG,
    "thinking": "enabled",
    "reasoning_effort": "high",
}


def test_build_openai_chat_with_thinking():
    client = LLMClient(CFG_THINK)
    _, _, body = client.build_request("hello")
    assert body["thinking"] == "enabled"
    assert body["reasoning_effort"] == "high"


def test_build_openai_chat_without_thinking():
    client = LLMClient(CFG)
    _, _, body = client.build_request("hello")
    assert "thinking" not in body
    assert "reasoning_effort" not in body


def test_build_openai_responses_with_thinking():
    client = LLMClient({**CFG_THINK, "format": "openai_responses"})
    _, _, body = client.build_request("hello")
    assert body["thinking"] == "enabled"
    assert body["reasoning_effort"] == "high"


def test_build_anthropic_thinking_string_adaptive():
    client = LLMClient({**CFG, "format": "anthropic", "thinking": "adaptive"})
    _, _, body = client.build_request("hello")
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}


def test_build_anthropic_thinking_string_enabled():
    client = LLMClient({**CFG, "format": "anthropic", "thinking": "enabled"})
    _, _, body = client.build_request("hello")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 10000}


def test_build_anthropic_thinking_dict_passthrough():
    client = LLMClient({
        **CFG, "format": "anthropic",
        "thinking_config": {"type": "enabled", "budget_tokens": 20000},
    })
    _, _, body = client.build_request("hello")
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 20000}


def test_build_anthropic_thinking_disabled():
    client = LLMClient({**CFG, "format": "anthropic", "thinking": "disabled"})
    _, _, body = client.build_request("hello")
    assert body["thinking"] == {"type": "disabled"}


def test_build_anthropic_effort():
    client = LLMClient({**CFG, "format": "anthropic", "anthropic_effort": "medium"})
    _, _, body = client.build_request("hello")
    assert body["output_config"] == {"effort": "medium"}


def test_build_anthropic_no_thinking():
    client = LLMClient({**CFG, "format": "anthropic"})
    _, _, body = client.build_request("hello")
    assert "thinking" not in body
    assert "output_config" not in body


def test_invalid_thinking_raises():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "thinking": "bogus"})


def test_invalid_reasoning_effort_raises():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "reasoning_effort": "ultra"})


def test_invalid_anthropic_effort_raises():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "format": "anthropic", "anthropic_effort": "turbo"})


def test_build_anthropic_budget_too_small():
    with pytest.raises(LLMError):
        LLMClient({**CFG, "format": "anthropic",
                   "thinking_config": {"type": "enabled", "budget_tokens": 100}})


def test_extract_openai_chat_reasoning_fallback():
    data = {"choices": [{"message": {"content": None, "reasoning_content": "think..."}}]}
    assert LLMClient.extract_reply("openai_chat", data) == "think..."


def test_extract_anthropic_thinking_fallback():
    data = {"content": [
        {"type": "thinking", "thinking": "plan..."},
        {"type": "text", "text": "final answer"},
    ]}
    assert LLMClient.extract_reply("anthropic", data) == "final answer"


def test_extract_anthropic_thinking_only():
    data = {"content": [{"type": "thinking", "thinking": "plan..."}]}
    assert LLMClient.extract_reply("anthropic", data) == "plan..."
