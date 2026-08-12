"""LLM 客户端：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages。"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_BUDGET_TOKENS = 10000

_VALID_THINKING = {"enabled", "disabled", "adaptive"}
_VALID_REASONING_EFFORT = {"low", "high", "max"}
_VALID_ANTHROPIC_EFFORT = {"low", "medium", "high", "xhigh", "max"}


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
        self.thinking = cfg.get("thinking")
        self.thinking_config = cfg.get("thinking_config")
        self.reasoning_effort = cfg.get("reasoning_effort")
        self.anthropic_effort = cfg.get("anthropic_effort")
        if self.format not in ("openai_chat", "openai_responses", "anthropic"):
            raise LLMError(f"未知 LLM format: {self.format}")
        if not self.api_key or not self.model or not self.base_url:
            raise LLMError("LLM 配置必须包含 base_url/api_key/model")
        if self.thinking is not None and not isinstance(self.thinking, dict) \
                and self.thinking not in _VALID_THINKING:
            raise LLMError(f"thinking 必须是 enabled/disabled/adaptive 或 dict，得到: {self.thinking!r}")
        if self.reasoning_effort is not None and self.reasoning_effort not in _VALID_REASONING_EFFORT:
            raise LLMError(f"reasoning_effort 必须是 low/high/max，得到: {self.reasoning_effort!r}")
        if self.anthropic_effort is not None and self.anthropic_effort not in _VALID_ANTHROPIC_EFFORT:
            raise LLMError(f"anthropic_effort 必须是 low/medium/high/xhigh/max，得到: {self.anthropic_effort!r}")
        # anthropic budget 预校验（启动即失败，而非请求时）
        payload = self._thinking_payload()
        if payload is not None:
            budget = payload.get("budget_tokens")
            if budget is not None and budget < 1024:
                raise LLMError(f"anthropic budget_tokens 必须 >= 1024，得到: {budget}")

    def _thinking_payload(self) -> dict | None:
        """构造 anthropic 的 thinking 参数；无配置返回 None。"""
        if self.thinking_config is not None:
            return dict(self.thinking_config)
        if isinstance(self.thinking, dict):
            return dict(self.thinking)
        if self.thinking == "adaptive":
            return {"type": "adaptive", "display": "summarized"}
        if self.thinking == "enabled":
            return {"type": "enabled", "budget_tokens": DEFAULT_BUDGET_TOKENS}
        if self.thinking == "disabled":
            return {"type": "disabled"}
        return None

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
            if self.thinking is not None:
                body["thinking"] = self.thinking if isinstance(self.thinking, dict) else str(self.thinking)
            if self.reasoning_effort is not None:
                body["reasoning_effort"] = self.reasoning_effort
        elif self.format == "openai_responses":
            url = f"{self.base_url}/responses"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            body = {
                "model": self.model,
                "input": user_text,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if self.thinking is not None:
                body["thinking"] = self.thinking if isinstance(self.thinking, dict) else str(self.thinking)
            if self.reasoning_effort is not None:
                body["reasoning_effort"] = self.reasoning_effort
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
            thinking = self._thinking_payload()
            if thinking is not None:
                budget = thinking.get("budget_tokens")
                if budget is not None and budget < 1024:
                    raise LLMError(f"anthropic budget_tokens 必须 >= 1024，得到: {budget}")
                body["thinking"] = thinking
            if self.anthropic_effort is not None:
                body["output_config"] = {"effort": self.anthropic_effort}
        return url, headers, body

    @staticmethod
    def extract_reply(fmt: str, data: dict) -> str:
        """从响应 JSON 提取回复文本（content 优先，reasoning/thinking 后备）。"""
        if fmt == "openai_chat":
            msg = data["choices"][0]["message"]
            content = msg.get("content")
            if content:
                return str(content)
            return str(msg.get("reasoning_content", ""))
        if fmt == "openai_responses":
            parts = []
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for c in item.get("content", []):
                        if c.get("type") == "output_text":
                            parts.append(str(c.get("text", "")))
            if parts:
                return "\n".join(parts)
            for item in data.get("output", []):
                if item.get("type") == "reasoning":
                    for c in item.get("content", []):
                        if c.get("type") in ("summary_text", "text"):
                            parts.append(str(c.get("text", "")))
            return "\n".join(parts)
        # anthropic
        parts = []
        for c in data.get("content", []):
            if c.get("type") == "text":
                parts.append(str(c.get("text", "")))
        if parts:
            return "\n".join(parts)
        parts = []
        for c in data.get("content", []):
            if c.get("type") == "thinking":
                parts.append(str(c.get("thinking", "")))
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
