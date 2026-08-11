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
