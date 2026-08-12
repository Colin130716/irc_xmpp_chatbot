# LLM 思考开关与深度配置实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 LLM 客户端新增 thinking 开关与思考深度配置，适配 DeepSeek（OpenAI 兼容 reasoning_effort）与 Anthropic（adaptive/output_config.effort）。

**Architecture:** 在 `LLMClient.build_request` 中按格式注入 thinking/reasoning_effort/output_config 字段；`extract_reply` 增加 reasoning/thinking 后备提取；`__init__` 增加配置字段与枚举校验。

**Tech Stack:** Python 3.14、httpx、pytest

## Global Constraints

- 仅修改 `src/ircxmppbot/llm.py` 与 `tests/test_llm.py`、`configs/server.yaml`
- 不配置 thinking/reasoning_effort/anthropic_effort → **不发**任何相关字段（向后兼容）
- anthropic thinking 解析优先级：`thinking_config` dict > `thinking` dict > `thinking` 字符串快捷 > 不发
- 校验失败（非法枚举、budget<1024）→ 启动抛 `LLMError`
- DeepSeek 的 medium/xhigh 透传不做映射（API 兼容处理）
- 回复提取：content/text 优先，reasoning_content/thinking 后备

---

### Task 1: llm.py — thinking/reasoning_effort/anthropic_effort 配置与请求体注入

**Files:**
- Modify: `src/ircxmppbot/llm.py`
- Modify: `tests/test_llm.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `LLMClient.__init__` 新增字段：`self.thinking: str | None`、`self.thinking_config: dict | None`、`self.reasoning_effort: str | None`、`self.anthropic_effort: str | None`
  - `LLMClient.build_request(user_text) -> (url, headers, body)` — 按格式注入新字段
  - `LLMClient.extract_reply(fmt, data) -> str` — 增加 reasoning/thinking 后备

- [ ] **Step 1: 写失败测试**

`tests/test_llm.py` 追加：

```python
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
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_llm.py -v`
Expected: FAIL（新断言 body 无 thinking 字段 / extract_reply 无后备）

- [ ] **Step 3: 修改 llm.py**

`src/ircxmppbot/llm.py` 全文替换：

```python
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
    """统一封装三种 LLM API 格式，支持 thinking/reasoning_effort。"""

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
            # reasoning 后备
            for item in data.get("output", []):
                if item.get("type") == "reasoning":
                    for c in item.get("content", []):
                        if c.get("type") == "summary_text" or c.get("type") == "text":
                            parts.append(str(c.get("text", "")))
            return "\n".join(parts)
        # anthropic
        parts = []
        for c in data.get("content", []):
            if c.get("type") == "text":
                parts.append(str(c.get("text", "")))
        if parts:
            return "\n".join(parts)
        # thinking 后备（display: summarized 时返回）
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
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_llm.py -v`
Expected: PASS（23 passed）

- [ ] **Step 5: 提交**

```bash
git add src/ircxmppbot/llm.py tests/test_llm.py
git commit -m "feat(llm): thinking 开关与思考深度配置（DeepSeek + Anthropic adaptive）"
```

---

### Task 2: 配置示例 + 全量回归

**Files:**
- Modify: `configs/server.yaml`
- Modify: `README.md`、`AGENTS.md`

**Interfaces:**
- Consumes: 全部模块
- Produces: 配置文档与功能说明

- [ ] **Step 1: server.yaml 的 llm 段增加注释示例**

`configs/server.yaml` 的 `llm:` 段追加（注释形式，不启用）：

```yaml
  # thinking: enabled            # enabled/disabled/adaptive（三格式通用，可选）
  # reasoning_effort: high       # low/high/max，仅 openai_chat/openai_responses
  # thinking_config:             # 仅 anthropic：dict 透传（如 {type: adaptive, display: summarized}）
  #   type: adaptive
  #   display: summarized
  # anthropic_effort: medium     # 仅 anthropic（→ output_config.effort）
```

- [ ] **Step 2: README 功能段补充**

`README.md` 功能列表增加：

```markdown
- LLM 思考控制：thinking 开关（三格式）+ DeepSeek reasoning_effort + Anthropic output_config.effort
```

- [ ] **Step 3: AGENTS.md 补充**

`AGENTS.md` 的库版本/配置相关处补充一行：

```markdown
- LLM 思考配置（llm.py）：`thinking`（enabled/disabled/adaptive，三格式）、`reasoning_effort`（仅 OpenAI 兼容）、`thinking_config`（anthropic dict 透传）、`anthropic_effort`（→ output_config.effort）；不配置则不发
```

- [ ] **Step 4: 全量回归**

Run: `pytest tests/ -q`
Expected: 全部 PASS（llm 23 + 其余 79 = 102 passed）

- [ ] **Step 5: 提交**

```bash
git add configs/server.yaml README.md AGENTS.md
git commit -m "docs(config): thinking 配置示例与说明"
```

---

## 自审清单

**1. Spec 覆盖核查：**

| 设计文档要求 | 对应任务 |
|---|---|
| thinking 三格式支持 | Task 1（openai 字符串 / anthropic dict+快捷） |
| anthropic dict 透传 + 字符串快捷 | Task 1 `_thinking_payload` |
| reasoning_effort 仅 OpenAI 兼容 | Task 1 openai 分支 |
| anthropic_effort → output_config.effort | Task 1 anthropic 分支 |
| content 优先 + reasoning/thinking 后备 | Task 1 `extract_reply` |
| 不配置则不发 | Task 1 各分支条件注入 |
| 枚举校验 + budget<1024 | Task 1 `__init__` 校验（注：budget<1024 校验在 dict 透传场景由 API 处理，设计文档的 1024 校验在快捷 enabled 时由 DEFAULT 值保证） |
| 配置示例 + 文档 | Task 2 |

**2. 占位符扫描：** 所有步骤含完整代码与预期输出，无 TBD/TODO。

**3. 类型/签名一致性：** `build_request`/`extract_reply` 签名不变（内部实现扩展）；新字段 `thinking/thinking_config/reasoning_effort/anthropic_effort` 在 __init__ 定义、build_request 使用、测试引用一致。

**4. 已知边界（实现时注意）：**
- `thinking` 配置为 dict 时（openai 分支）`body["thinking"] = dict` 直接透传——DeepSeek 若收到 dict 可能报错，但这是用户显式配置（YAGNI：不为未知 provider 特判）
- `budget_tokens < 1024`：在 `_thinking_payload` 中，若最终 thinking payload 含 `budget_tokens` 且 < 1024，启动抛 `LLMError`（与设计文档第 6 节一致）；快捷 `enabled` 用 DEFAULT_BUDGET_TOKENS=10000 恒合法
- anthropic 的 `thinking` dict 透传时 `display` 默认 omitted（新模型）——用户需自行加 display: summarized 才可见思考（配置示例已标注）
