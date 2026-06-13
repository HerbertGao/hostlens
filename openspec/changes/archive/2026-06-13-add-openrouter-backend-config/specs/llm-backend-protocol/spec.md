## 修改需求

### 需求:`AnthropicAPIBackend` 必须完整实现 `LLMBackend` 且关闭 SDK 内部重试

`hostlens.agent.backends.anthropic_api.AnthropicAPIBackend` 必须：

- 实现 `LLMBackend` Protocol 全部成员
- 实现 `BackendDiagnostics` Protocol 全部成员
- `name = "anthropic_api"` 类属性
- `capabilities` 必须为**构造时注入的实例属性**（不再是类属性），默认值 `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`。仅 `prompt_caching` 字段可经构造参数覆盖（详见后续「`AnthropicAPIBackend` 必须支持 `prompt_caching` capability 实例注入」需求），其余 6 字段固定为上述值（判据「既被 branch 又随模型变」只命中 `prompt_caching`：`tool_use` 被 branch 但对 anthropic 兼容端点恒真、`structured_output` 是 Planner 强制 JSON 的语义依赖、`parallel_tool_use`/`extended_thinking`/`vision`/`streaming` loop/gate 不 branch 无消费者，均无按模型配置的需求；`extended_thinking` / `streaming` 必须 False —— M2 Protocol 签名不含 `thinking` 参数与流式响应）
- 构造 `anthropic.AsyncAnthropic` client 时**必须**显式 `max_retries=0` 关闭 SDK 内部重试
- `messages_create` 把 Anthropic SDK 异常包装成 backend 层异常（**异常构造与字段访问必须对齐 SDK 真实 API**：`RateLimitError` 继承 `APIStatusError`，构造签名 `(message, *, response, body)`，状态从 `exc.status_code`，retry-after 从 `exc.response.headers.get("retry-after")` 读，转 float；529 在 SDK 中映射为 `anthropic.OverloadedError`，同样继承 `APIStatusError` 且 `status_code == 529`）：
  - `anthropic.RateLimitError`（429）→ 从 `exc.response.headers.get("retry-after")` 读 retry-after 转 float（缺省 None），raise `BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=<value>, cause=exc)`
  - `anthropic.OverloadedError` 或其他 `anthropic.APIStatusError` 且 `exc.status_code == 529` → raise `BackendRateLimited(backend_name="anthropic_api", retry_after_seconds=None, cause=exc)`
  - 其他 `anthropic.APIStatusError`（5xx 非 529）→ raise `BackendUnavailable(backend_name="anthropic_api", cause=exc)`
  - `anthropic.APIConnectionError` / `anthropic.APITimeoutError` → raise `BackendUnavailable(backend_name="anthropic_api", cause=exc)`
  - `anthropic.AuthenticationError` → raise `BackendError(backend_name="anthropic_api", kind="auth_invalid", cause=exc)`
- `health_check` 调一次 `messages.create`，model 入参**必须**从构造时注入的 `health_check_model: str` 字段读取（构造签名扩展为 `__init__(self, *, api_key: str, base_url: str | None = None, health_check_model: str = "claude-haiku-4-5")` —— 默认走 Haiku 最便宜的 model 探测连通性，不走 primary Opus；调用方如需自定义可在 `create_backend` 时按 `settings.agent.primary_model` 或 fallback_model 显式覆盖）；其余入参 `messages=[{"role": "user", "content": "ping"}], max_tokens=10`；成功返回 `BackendHealth(is_healthy=True, ...)`，失败返回 `BackendHealth(is_healthy=False, error=<scrubbed>, ...)`
- `quota_check` M2 范围**必须**返回 `None`（Anthropic Console quota API 未公开标准接口）
- `ensure_safe_for_daemon` no-op（API key 在 daemon 模式安全）

#### 场景:SDK client `max_retries=0`

- **当** 构造 `backend = AnthropicAPIBackend(api_key="...", ...)`，访问 `backend._client.max_retries`（或等价的 SDK 内部属性）
- **那么** 必须为 0（**禁止**使用 SDK 默认重试）

#### 场景:capabilities 全字段声明（默认实例）

- **当** 构造 `backend = AnthropicAPIBackend(api_key="...")` 不传 `prompt_caching`，访问 `backend.capabilities`（实例属性）
- **那么** 必须等于 `BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`（`extended_thinking` / `streaming` 必须 False —— M2 Protocol 签名不含 `thinking` 参数与流式响应）

#### 场景:429 包装成 BackendRateLimited

- **当** SDK 抛 `anthropic.RateLimitError(message="rate limited", response=httpx.Response(429, headers={"retry-after": "30"}, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")), body=None)`，调用 `backend.messages_create(...)`
- **那么** 必须 raise `BackendRateLimited`，且 `exc.retry_after_seconds == 30.0`；实现路径必须经 `exc.response.headers.get("retry-after")` 读取（不经 SDK message 字符串解析）；**禁止** backend 内部重试

#### 场景:529 无 retry-after 包装成 BackendRateLimited

- **当** SDK 抛 `anthropic.OverloadedError(message="overloaded", response=httpx.Response(529, request=...), body=None)`（或任意 `APIStatusError` 子类满足 `exc.status_code == 529`），不带 retry-after header
- **那么** 必须 raise `BackendRateLimited`，且 `exc.retry_after_seconds is None`

#### 场景:其他 5xx 包装成 BackendUnavailable

- **当** SDK 抛 `anthropic.APIStatusError`（status_code ∈ {500, 502, 503, 504}），不是 429 也不是 529
- **那么** 必须 raise `BackendUnavailable`，`exc.__cause__` 链回原 SDK 异常

#### 场景:网络错误包装成 BackendUnavailable

- **当** SDK 抛 `anthropic.APIConnectionError(...)` 或 `anthropic.APITimeoutError(...)`
- **那么** 必须 raise `BackendUnavailable`，且 `exc.__cause__` 链回原 SDK 异常

#### 场景:认证错误包装成 BackendError

- **当** SDK 抛 `anthropic.AuthenticationError(...)`
- **那么** 必须 raise `BackendError`，`exc.kind == "auth_invalid"`，message 中**禁止**含 api_key 完整值（仅含前 4 + 后 4 字符指纹形式）

#### 场景:health_check 成功

- **当** `await backend.health_check()` 在 API 可用时
- **那么** 返回 `BackendHealth(is_healthy=True, backend_name="anthropic_api", latency_ms=<float>, error=None)`

#### 场景:health_check 失败时脱敏

- **当** `await backend.health_check()` 在 API 401 时
- **那么** 返回 `BackendHealth(is_healthy=False, ...)`，且 `error` 字段**禁止**含 api_key 原值（如有，必须替换为 `***`）

## 新增需求

### 需求:`AnthropicAPIBackend` 必须支持 `extra_headers` 透传到 SDK `default_headers`

`AnthropicAPIBackend` 必须新增构造参数 `extra_headers: dict[str, str] | None = None`（在既有「`AnthropicAPIBackend` 必须完整实现 `LLMBackend` 且关闭 SDK 内部重试」需求所定义的构造签名之上扩展一个可选参数，默认值保证既有调用方不变），控制构造 `anthropic.AsyncAnthropic` client 时是否注入自定义出站 HTTP header。用途：OpenRouter 等 anthropic 兼容端点推荐请求方携带 `HTTP-Referer` / `X-OpenRouter-Title` 统计 header。

- `extra_headers` 非 `None` 时，必须透传给 `anthropic.AsyncAnthropic(..., default_headers=<value>)`
- `extra_headers is None`（默认）时，**禁止**向 SDK client 传入 `default_headers`（既有真 Anthropic client 构造形状逐字不变）
- 接线层（`create_backend`）**必须**在透传前丢弃 `extra_headers` 中与 SDK 认证 header 同名的键（大小写不敏感的 `x-api-key` / `authorization`），认证以 `api_key` 字段为唯一来源——**禁止** `extra_headers` 覆盖认证
- `extra_headers` 的**值**在 `__repr__`（及任何未来输出 `extra_headers` 的日志点）中**必须无条件全遮蔽**为 `***`（keys 可保留），**禁止**依赖形态识别（`core.redact` 的 `redact_text` 形态正则对裸 token 兜不住）。`extra_headers` 定位为非密钥统计 header，不提供 `Settings` 序列化层 `SecretStr` 级保护。当前唯一输出面是 `__repr__`（doctor `BackendHealthRow` 不含 `extra_headers`、backend 无 logger 点）

#### 场景:extra_headers 注入 default_headers

- **当** 构造 `AnthropicAPIBackend(api_key="...", extra_headers={"HTTP-Referer": "https://x", "X-OpenRouter-Title": "hostlens"})`
- **那么** 底层 `anthropic.AsyncAnthropic` 必须以 `default_headers={"HTTP-Referer": "https://x", "X-OpenRouter-Title": "hostlens"}` 构造；后续 `messages_create` 出站请求携带这两个 header

#### 场景:extra_headers 缺省不改请求形状

- **当** 构造 `AnthropicAPIBackend(api_key="...")` 不传 `extra_headers`（默认 None）
- **那么** **禁止**向 `anthropic.AsyncAnthropic` 传 `default_headers`（真 Anthropic 请求 header 形状逐字不变）

#### 场景:extra_headers 不得覆盖认证 header

- **当** `create_backend` 收到 `extra_headers={"x-api-key": "attacker", "HTTP-Referer": "https://x"}`
- **那么** 透传给 backend 的 `extra_headers` 必须已丢弃 `x-api-key`（大小写不敏感），仅保留 `{"HTTP-Referer": "https://x"}`；client 认证仍用 `api_key` 字段值

#### 场景:认证 header 丢弃大小写不敏感

- **当** `create_backend` 收到 `extra_headers={"X-Api-Key": "attacker", "AUTHORIZATION": "Bearer x", "HTTP-Referer": "https://x"}`（大写/混合大小写变体，对齐 SDK 规范名 `X-Api-Key`）
- **那么** 透传给 backend 的 `extra_headers` 必须已丢弃 `X-Api-Key` 与 `AUTHORIZATION`（大小写不敏感比较），仅保留 `{"HTTP-Referer": "https://x"}`

#### 场景:extra_headers 值在 repr 中全遮蔽

- **当** 构造 `AnthropicAPIBackend(api_key="...", extra_headers={"X-Custom-Auth": "not-a-real-secret-PROBE-0001"})`，对该 backend 取 `repr(backend)`（测试值刻意选 `redact_text` 不命中的形态，以 falsify「错误地走形态脱敏」的实现）
- **那么** 输出**禁止**含 `not-a-real-secret-PROBE-0001` 子串；`X-Custom-Auth` 的值必须被全遮蔽为 `***`（不依赖形态识别，任意值同样遮蔽）

### 需求:`AnthropicAPIBackend` 必须支持 `prompt_caching` capability 实例注入

`AnthropicAPIBackend` 必须新增构造参数 `prompt_caching: bool = True`（在既有构造签名之上扩展一个可选参数，默认 `True` 保证既有真 Anthropic 行为不变），其值注入到该实例 `capabilities` 的 `prompt_caching` 字段；`capabilities` 其余 6 字段固定为默认值。用途：OpenRouter 上非 Claude 模型（DeepSeek / Qwen 等）不支持 `cache_control: ephemeral`、`cache_creation_input_tokens` 恒 0，置 `prompt_caching=False` 使 backend 如实声明不支持 prompt caching。

- `prompt_caching=False` 时，`backend.capabilities.prompt_caching` 必须为 `False`，从而 Agent loop 既有分支**不注入** `cache_control`（CLAUDE.md §4.8 红线对非 Claude endpoint 由此可正确触发）
- `prompt_caching=True`（默认）时，`backend.capabilities` 必须等于既有默认值，行为与今日完全一致
- 本参数**仅**影响 `capabilities.prompt_caching` 一项；**禁止**令其影响其余 6 个 capability 字段
- backend 严格透传不静默丢弃语义不变：若 Agent loop 在 `prompt_caching=False` 时仍注入 `cache_control`，既有 `check_capability_consistency` 门必须 raise `BackendCapabilityViolation`（不假装成功，避免 cache hit rate 指标失真）

#### 场景:prompt_caching=False 实例注入

- **当** 构造 `AnthropicAPIBackend(api_key="...", prompt_caching=False)`，访问 `backend.capabilities`
- **那么** `backend.capabilities.prompt_caching is False`，其余 6 字段等于默认值（`tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False`）

#### 场景:prompt_caching 默认 True 行为不变

- **当** 构造 `AnthropicAPIBackend(api_key="...")` 不传 `prompt_caching`
- **那么** `backend.capabilities.prompt_caching is True`，`capabilities` 等于既有默认 `BackendCapabilities` 值

#### 场景:prompt_caching=False 时注入 cache_control 触发 violation

- **当** `prompt_caching=False` 的 backend 收到含 `cache_control` block 的 `system` / `messages` / `tools`，调 `messages_create(...)`
- **那么** 既有 `check_capability_consistency` 门必须 raise `BackendCapabilityViolation`（**禁止**静默丢弃 `cache_control` 后假装成功）
