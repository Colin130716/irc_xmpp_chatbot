# LLM 思考开关与深度配置设计

- 日期：2026-08-12
- 状态：待用户审阅
- 基础：`src/ircxmppbot/llm.py`（现有三种格式封装）

## 1. 目标

为 LLM 客户端新增思考（thinking）开关与思考深度控制，适配 DeepSeek（OpenAI 兼容）与 Anthropic（新版 adaptive 思考）两套 API 语义。

## 2. 已确认决策

| 决策点 | 结论 |
|---|---|
| thinking 字段 | **三种格式都支持**；anthropic 用 dict 透传 + 字符串快捷方式 |
| reasoning_effort | 仅 openai_chat / openai_responses（DeepSeek） |
| anthropic 深度控制 | **加 `output_config.effort`**（新版 adaptive 模式） |
| 回复提取 | content 优先 + reasoning/thinking 后备 |
| 默认值 | 不配置则**不发**任何相关字段（向后兼容） |

## 3. 配置结构（server.yaml `llm:` 段）

```yaml
llm:
  format: openai_chat          # openai_chat | openai_responses | anthropic
  # ... 现有字段 ...
  thinking: enabled            # 三格式通用开关（可选）
  # anthropic 专用（可选）：dict 完整透传或字符串快捷
  #   dict: {type: adaptive, display: summarized} | {type: enabled, budget_tokens: 10000} | {type: disabled}
  #   字符串: adaptive / enabled / disabled
  thinking_config:             # 仅 anthropic 用（可选，覆盖 thinking 的快捷映射）
    type: adaptive
    display: summarized
  reasoning_effort: high       # 仅 openai_chat/openai_responses（可选）
  anthropic_effort: medium     # 仅 anthropic（可选，映射到 output_config.effort）
```

## 4. 请求体映射

### 4.1 openai_chat / openai_responses（DeepSeek）

```json
{
  "model": "...",
  "messages": [...],           // 或 "input" for responses
  "temperature": 0.7,
  "max_tokens": 1000,
  "thinking": "enabled",       // 仅当配置了 thinking（"enabled"|"disabled"）
  "reasoning_effort": "high"   // 仅当配置了 reasoning_effort（low|high|max）
}
```

### 4.2 anthropic

`thinking` 解析优先级：
1. `thinking_config` dict 存在 → 原样透传（用户完全掌控，适配新旧模型）
2. `thinking` 为 dict → 原样透传
3. `thinking` 为字符串 → 快捷映射：
   - `"adaptive"` → `{"type": "adaptive", "display": "summarized"}`
   - `"enabled"` → `{"type": "enabled", "budget_tokens": 10000}`（旧模型 extended）
   - `"disabled"` → `{"type": "disabled"}`
4. 均未配置 → 不发

`anthropic_effort` 配置 → `"output_config": {"effort": "medium"}`（仅当配置；adaptive 模式的深度控制）

## 5. 回复提取（思考模式兼容）

| 格式 | 提取逻辑 |
|---|---|
| openai_chat | `message.content` 优先；为空则 `message.reasoning_content` |
| openai_responses | `output` 中 message 条目的 `output_text`；为空则 reasoning 条目文本 |
| anthropic | `content` 中 `text` 类型块；为空则 `thinking` 类型块文本（display: summarized 时返回） |

## 6. 校验

- `thinking` 字符串非 `enabled`/`disabled`/`adaptive` → 启动抛 `LLMError`
- `reasoning_effort` 非 `low`/`high`/`max` → 启动抛 `LLMError`（medium/xhigh 透传，由 API 兼容处理）
- `anthropic_effort` 非 `low`/`medium`/`high`/`xhigh`/`max` → 启动抛 `LLMError`
- `budget_tokens` < 1024 → 启动抛 `LLMError`（Anthropic 硬性下限）

## 7. 明确不做（YAGNI）

- 不做流式思考输出
- 不做多轮上下文中的 thinking 块保留（单轮对话，不涉及）
- 不做 `display: omitted` 的 signature 处理（透传由用户负责）

## 8. 测试策略

- `test_llm.py`：
  - openai_chat 带 thinking/reasoning_effort → body 含字段
  - openai_chat 未配置 → body 无字段（向后兼容）
  - anthropic thinking 字符串 `adaptive` → body 映射为 adaptive dict
  - anthropic thinking_config dict 透传
  - anthropic anthropic_effort → output_config.effort
  - 非法值抛 LLMError（thinking 非法/reasoning_effort 非法/budget<1024）
  - extract_reply：openai_chat reasoning_content 后备、anthropic thinking 块后备
