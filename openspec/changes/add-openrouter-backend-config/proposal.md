## 为什么

OpenRouter 在 `https://openrouter.ai/api/v1/messages` 暴露一个 **Anthropic Messages 兼容端点**，经 `tests/manual/openrouter_probe.py` 实测（2026-06-12）确认：现有 `AnthropicAPIBackend` 配 `type=anthropic_api` + `base_url` 即可接入第三方模型（DeepSeek / Qwen 等），**无需新 backend 类型**。接入路径已可用，但暴露两个配置缺口：

1. OpenRouter 推荐请求方携带 `HTTP-Referer` / `X-OpenRouter-Title` 统计 header，当前 `BackendSettings` 无法注入任意出站 header。
2. 非 Claude 模型走兼容端点时不支持 prompt caching：`cache_control: ephemeral` block 被上游静默忽略，`cache_creation_input_tokens` 恒 `0`。而 `AnthropicAPIBackend.capabilities.prompt_caching` 写死为 `True`（ClassVar），Agent loop 据此仍注入 `cache_control` → **cache hit rate 指标失真**（CLAUDE.md §4.8 红线本意是「prompt_caching=False 时 loop 不得注入 cache_control」，但当前该 capability 不可按端点配置，红线对非 Claude endpoint 无法触发）。

这是「接入已可用」之后的两点对症优化，**不阻塞当前使用**。

## 变更内容

- **A — `extra_headers` 透传**：`BackendSettings` 新增 `extra_headers: dict[str, str] | None = None`；`AnthropicAPIBackend.__init__` 接收并透传给 Anthropic SDK 的 `default_headers`；`create_backend` 负责接线。与 `backend.type` 无耦合校验（沿用 `disable_thinking` 先例：任意 type 可设，仅 `anthropic_api` 路径消费）。
- **B — `prompt_caching` capability 按实例注入**：`AnthropicAPIBackend.capabilities` 从写死 `prompt_caching=True` 的 **ClassVar** 改为**构造时注入的实例字段**（默认 `prompt_caching=True`，向后兼容真 Anthropic）。新增 `BackendSettings.prompt_caching: bool | None = None` 定向覆盖，`create_backend` 传入；为 `False` 时该 backend 实例声明 `prompt_caching=False`，Agent loop 既有分支据此**不注入** `cache_control`，指标恢复真实。
- 仅覆盖 `prompt_caching` 单项（非全 `BackendCapabilities` 子块）：判据是「唯一既被 loop/gate branch、又随模型变的字段」——`prompt_caching`（`loop.py:542/589` 的 `cache_control` 注入门 + `backend.py:411/419/432` 的 gate）在非 Claude 上游被忽略，是唯一需要按实例配的。其余 6 个字段无此「按模型配置」的需求：`tool_use`（`backend.py:440` 被 branch）对 anthropic 兼容端点恒真、`structured_output` 是 Planner 强制 JSON 的语义依赖（loop 不显式 branch）、`parallel_tool_use` / `extended_thinking` / `vision` / `streaming` loop/gate 均不 branch 也无消费者（`extended_thinking` / `streaming` 还恒 False）。给它们开 per-config 覆盖只是 footgun（制造「声明了但无人按它分支」的失真旋钮）。详见 design.md D-1。

## 功能 (Capabilities)

### 新增功能
<!-- 无新增 capability。本变更复用 anthropic_api backend + 既有 BackendCapabilities 字段集，不新增 BackendType、不新增 capability 字段。 -->
（无）

### 修改功能
- `llm-backend-protocol`: `AnthropicAPIBackend` 的 `capabilities` 由 ClassVar 升为构造时注入的实例属性（默认值与今日一致，新增 `prompt_caching=False` 注入场景）；`BackendCapabilities` 字段集（恰好 7 个、全必填、不可变）**不变**。
- `core-services`: `BackendSettings` 新增两个字段 `extra_headers: dict[str, str] | None = None` 与 `prompt_caching: bool | None = None`，均与 `backend.type` 无耦合校验、仅 `anthropic_api` 路径消费、缺省保持既有真 Anthropic 请求路径不变。

## 影响

**对外契约影响**：
- **配置 schema**（`core-services`）：`backend:` namespace 加两个可选字段；缺省值保证 M0–M9 既有配置文件零改动仍合法（`extra="forbid"` 下新增字段不影响旧配置）。
- **LLM Backend 契约**（`llm-backend-protocol`）：`AnthropicAPIBackend.capabilities` 访问语义从「类属性」变「实例属性」；`LLMBackend` Protocol 仍声明 `capabilities: BackendCapabilities` 实例属性（structural typing，Protocol 形状不变）。
- **不影响**：Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令均无变更；`agent-loop` 的 `cache_control` 注入逻辑本身不改（它已按 `backend.capabilities.prompt_caching` 分支，本变更只是让该 capability 可按实例为 `False`）。

**BackendCapabilities 声明**：不新增 capability 字段，不新增 `BackendType`。`AnthropicAPIBackend` 默认实例 capability 与今日完全一致：`BackendCapabilities(prompt_caching=True, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`。OpenRouter 非 Claude 模型场景仅 `prompt_caching` 置 `False`。

**ToS 与合规**：不引入新认证机制 / 不新增 backend 类型。OpenRouter 端点由用户经 `base_url` + `api_key`（OpenRouter key）自行选择，ToS 责任在用户侧；Hostlens 仅复用既有 `anthropic_api` 透传路径，不扩大 Anthropic 直连之外的合规面。

**daemon-safe**：`anthropic_api` 路径无 daemon 限制（仅 `ClaudeSubscriptionBackend` 受限），本变更不触及 `ensure_safe_for_daemon`。

**Failure Semantics（§9）扩展**：无新增故障类别。`extra_headers` 透传错误 / 上游 OpenRouter 的 4xx/5xx 仍由 `AnthropicAPIBackend` 既有 `BackendRateLimited` / `BackendUnavailable` / `BackendError` 包装路径覆盖。

**BackendDiagnostics**：无需新实现；`AnthropicAPIBackend` 既有 `health_check` / `quota_check` / `ensure_safe_for_daemon` 不变（note：OpenRouter 端点的 quota 语义与 Anthropic 不同，但本变更不改 quota_check，留作已知限制写入 design.md）。

## prompt caching 策略与 token 影响

本变更的核心就是修正 prompt caching 行为：
- 真 Anthropic（默认 `prompt_caching=True`）：行为**完全不变**，系统提示词 / Inspector schema / few-shot 仍带 `cache_control: ephemeral`，cache hit rate 指标真实。
- OpenRouter 非 Claude（`prompt_caching=False`）：Agent loop **不注入** `cache_control`，出站请求体不含 cache block；`cache_creation_input_tokens` / `cache_read_input_tokens` 不再被误读为「缓存生效」。token 消耗：无缓存即每轮全量 input token（与真 Anthropic 首轮一致），是非 Claude 模型的固有成本，非本变更引入。

## Non-Goals（非目标）

- **不新增 `BackendType`**：OpenRouter 复用 `anthropic_api` + `base_url`，不做独立 `openrouter` type。
- **不实现** `BedrockBackend` / `VertexBackend` / `ClaudeSubscriptionBackend`（各自 M10.5 / 1.0 提案）。
- **不改** Agent loop 注入 `cache_control` 的既有逻辑本身（只让它按实例 capability 正确生效）。
- **不开放** 其余 6 个 capability（含 `tool_use`）的 per-config 覆盖：只有 `prompt_caching` 既被 branch 又随模型变；`tool_use` 对兼容端点恒真、`structured_output` 是 Planner 语义依赖、另 4 个无消费者，均无按模型配置的需求，暴露覆盖只是 footgun。
- **不做** OpenRouter 平台特性：模型路由 / fallback / 价格优化 / provider 偏好。
- **不动** `disable_thinking` 既有语义；不改 `quota_check` 以适配 OpenRouter quota。

## Failure Modes

1. **`extra_headers` 含非法 header 值**（如换行注入）→ SDK / httpx 在构造请求时报错；由 `AnthropicAPIBackend` 既有 try/except 包装为 `BackendUnavailable`，不静默吞。降级：fail-fast，配置错误在首次 LLM 调用暴露。
2. **用户对 OpenRouter 非 Claude 模型仍配 `prompt_caching=True`（未覆盖）**→ Agent loop 注入 `cache_control`，OpenRouter 静默忽略，指标失真——回到本变更要修的现状。降级：design.md 在 docs/ARCHITECTURE.md §9 明示「非 Claude 端点须设 `prompt_caching: false`」，doctor 不强校验（无法可靠判定上游模型族）。
3. **`prompt_caching=False` 但端点其实是真 Anthropic**（误配）→ 仅损失缓存收益（多花 token），不影响正确性。降级：可接受，纯成本退化。
4. **`extra_headers` 与 SDK 内部 header 同名冲突**（如误传 `x-api-key`）→ 以 SDK 显式参数为准 / 或被覆盖；design.md 规定 `default_headers` 不得覆盖认证 header，必要时在接线层过滤保留键。
5. **`BackendSettings` 旧配置文件加载**（无新字段）→ 两字段缺省 `None`，零行为变化，向后兼容。

## Operational Limits

- **并发预算**：无变化（不改 Agent loop 并发模型）。
- **内存预算**：`extra_headers` 是小 dict（≤ ~1KB），随 backend 实例常驻，可忽略。
- **超时设置**：复用 `AnthropicAPIBackend` 既有 `timeout` 透传，不新增超时维度。

## Security & Secrets

- **新密钥**：无 Hostlens 侧新密钥类别；OpenRouter API key 复用既有 `backend.api_key`（`SecretStr`，`model_dump_json` 输出 `**********`）。
- **脱敏**：`extra_headers` **定位为非密钥统计 header**（Referer/Title 等公开值），不提供 `SecretStr` 级保护。唯一输出面是 `AnthropicAPIBackend.__repr__`（已核：doctor `BackendHealthRow` 不输出 extra_headers、backend 当前无 logger 点）。为防误塞 token，`__repr__`（及任何未来输出 extra_headers 的日志点）必须对**值无条件全遮蔽**为 `***`（不依赖 `core.redact` 的形态识别——裸 token 形态正则兜不住），keys 可保留。design.md D-5 定机制。
- **攻击面**：不扩大——不新增网络监听 / 不新增认证路径；仅透传用户显式配置的 header 到用户显式配置的端点。

## Cost / Quota Impact

- **token 消耗**：真 Anthropic 路径不变；OpenRouter 非 Claude 路径无缓存 → 每轮全量 input token（成本归属 OpenRouter 账户，非 Anthropic 配额）。
- **API 调用频次**：无变化。
- **对 Anthropic 配额影响**：零（OpenRouter 流量不经 Anthropic 配额）。

## Demo Path（5 分钟本地 reproduce，cassette 优先）

无需付费 API 的回放路径：

```bash
pip install -e ".[dev]"
# A) extra_headers 透传单测（断言出站请求 default_headers 含注入值）
pytest tests/agent/backends/test_anthropic_api.py -k extra_headers -q
# B) prompt_caching=False 实例注入单测（断言 loop 不注入 cache_control）
pytest tests/agent/test_backend_capabilities.py -k instance_prompt_caching -q
pytest tests/core/test_config.py -k "extra_headers or prompt_caching" -q
```

需真实端点的验收（手动，非 CI）：

```bash
export HOSTLENS_BACKEND__TYPE=anthropic_api
export HOSTLENS_BACKEND__BASE_URL=https://openrouter.ai/api   # SDK 自动追加 /v1/messages；勿写成 .../api/v1（会双 /v1）
export HOSTLENS_BACKEND__API_KEY=sk-or-...
export HOSTLENS_BACKEND__PROMPT_CACHING=false
export HOSTLENS_BACKEND__EXTRA_HEADERS='{"HTTP-Referer":"https://github.com/HerbertGao/hostlens","X-OpenRouter-Title":"hostlens"}'
export HOSTLENS_AGENT__PRIMARY_MODEL=deepseek/deepseek-v4-pro
hostlens demo run cpu_saturation        # 跑通出报告；再换 qwen/qwen3.7-plus 复跑
```
