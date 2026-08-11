"""LLM 客户端：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages。"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    """LLM 调用失败。"""


class LLMClient:
    """统一封装三种 LLM API 格式。"""

    def __init__(self, cfg: dict) -> None:
        self.format = cfg.get("format", "openai_chat")
        self.base_url = cfg.get("base_url", "").rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.model = cfg.get("model", "")
        self.system_prompt = cfg.get("system_prompt", "")
        self.temperature = cfg.get("temperature", 0.7)
        self.max_tokens = cfg.get("max_tokens", 1000)
        self.timeout = cfg.get("timeout", 60)
        if self.format not in ("openai_chat", "openai_responses", "anthropic"):
            raise LLMError(f"未知 LLM format: {self.format}")
        if not self.api_key or not self.model or not self.base_url:
            raise LLMError("LLM 配置必须包含 base_url/api_key/model")

    def build_request(self, user_text: str) -> tuple[str, dict, dict]:
        """构造 (url, headers, body)。"""
        if self.format == "openai_chat":
            url = f"{self.base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            messages = [{"role": "user", "content": user_text}]
            if self.system_prompt:
                messages.insert(0, {"role": "system", "content": self.system_prompt})
            body = {
                "model": self.model,
                "messages": messages,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        elif self.format == "openai_responses":
            url = f"{self.base_url}/responses"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            body = {
                "model": self.model,
                "input": user_text,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
        else:  # anthropic
            url = f"{self.base_url}/messages"
            headers = {
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            }
            messages = [{"role": "user", "content": user_text}]
            body = {
                "model": self.model,
                "messages": messages,
                "system": self.system_prompt,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            }
        return url, headers, body

    @staticmethod
    def extract_reply(fmt: str, data: dict) -> str:
        """从响应 JSON 提取回复文本。"""
        if fmt == "openai_chat":
            content = data["choices"][0]["message"].get("content")
            return content if content is not None else "" 
        if fmt == "openai_responses":
            parts = []
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            parts.append(str(c.get("text", "")))
            return "\n".join(parts)
        # anthropic
        parts = []
        for c in data.get("content", []):
            if c.get("type") == "text":
                parts.append(str(c.get("text", "")))
        return "\n".join(parts)

    async def chat(self, user_text: str) -> str:
        url, headers, body = self.build_request(user_text)
        log.debug("LLM 请求 %s %s", self.format, url)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"LLM 请求失败: {e}") from e
        if resp.status_code != 200:
            raise LLMError(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise LLMError(f"LLM 响应非 JSON: {e}") from e
        try:
            return self.extract_reply(self.format, data)
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"LLM 响应格式异常: {e}") from e
