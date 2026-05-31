# Change: add-backend-disable-thinking

## Why

DeepSeek v4（经 anthropic 兼容端点 `https://api.deepseek.com/anthropic`）**默认强制返回 `type="thinking"` 内容块**。Hostlens M2 的 `MessageResponse.content` 判别联合只建模 `text` / `tool_use`（`src/hostlens/agent/backend.py`，`extended_thinking=False`），于是 `AnthropicAPIBackend.messages_create` 在 `MessageResponse.model_validate(...)`（`anthropic_api.py`）抛 `ValidationError` 崩在响应解析 —— 任何「thinking 默认开」的 anthropic 兼容端点当前都无法作为 backend 使用。

实测探针（2026-05-31，手测范围内）证实最小修复可行：请求带 `extra_body={"thinking":{"type":"disabled"}}` 关闭 thinking，含多轮 tool 循环（见 design.md 验证数据）。本提案落地这个开关，使 DeepSeek 类端点可作为**不消费 thinking 的普通 backend** 接入，不触碰判别联合、不提前做 M3 的 extended-thinking 全量支持。

## What Changes

- `BackendSettings` 新增配置字段 `disable_thinking: bool = False`。
- `AnthropicAPIBackend.messages_create` 在 `disable_thinking=True` 时，向 SDK 调用注入 `extra_body={"thinking":{"type":"disabled"}}`；`False`（默认）时不注入任何 thinking 相关字段。
- `create_backend` 工厂把 `settings.backend.disable_thinking` 透传到 `AnthropicAPIBackend` 构造。
- `messages_create` 把响应解析（`MessageResponse.model_validate`）的 `ValidationError` 捕获包装成 `BackendError`：从裸 pydantic 异常**归一到 `BackendError` 故障域**（Agent loop 据 agent-loop spec「不可重试 backend 异常直接上抛」fail-loud 上抛，最终由 CLI 边界 `inspect.py` 的 `except Exception` → exit 2 呈现一行错误、无 traceback），并**按成因区分 kind**：`content[*]` 判别联合 tag 失败（如 provider 未遵守 disabled 仍吐 `thinking`）→ `kind="unsupported_content_block"`，其它字段/结构不符（如 SDK 格式漂移）→ `kind="invalid_response"`（避免把 SDK 不兼容误标成 thinking 问题）。TODO M3.6「近期兜底」前置到本提案 —— 本开关正是开放第三方端点接入的路径，该失败模式由本提案引入暴露面，就该本提案兜住；**不**建模 thinking 块。
- 新增 live 标记集成测试，复用探针逻辑验证 DeepSeek pro/flash 多轮 tool 循环在 disabled 下零 thinking 块、零 400。

非破坏性：默认 `False` 保持真 Anthropic 请求行为逐字不变；`BackendCapabilities.extended_thinking` 仍为 `False`，M2 scope 不变。

## 功能 (Capabilities)

### 新增功能

（无 —— 本提案只给既有 capability 加 / 改需求）

### 修改功能

- `llm-backend-protocol`: `AnthropicAPIBackend` 增加 `disable_thinking` 构造开关（扩展既有「AnthropicAPIBackend 必须完整实现」需求的构造签名），开启时通过 `extra_body` 注入 `thinking:disabled`，使「thinking 默认开」的 anthropic 兼容端点可用而不破坏 M2 的 `text`/`tool_use` 响应契约；`create_backend` 工厂透传该开关；`messages_create` 把响应解析 `ValidationError` 归一成 `BackendError`，区分 `unsupported_content_block`（判别联合 tag 失败）与 `invalid_response`（其它格式不符）。
- `core-services`: `BackendSettings` 字段集增加可选 `disable_thinking: bool = False`（MODIFIED 既有「Settings 必须支持 backend 与 agent」需求，把字段并入原枚举，避免字段清单割裂；默认不改变真 Anthropic 请求行为）。

## Non-Goals

- **不消费 thinking 输出** —— 不建模 `ThinkingBlock` / `RedactedThinkingBlock`、不做原样回传、不改 cassette keying 归一化。那是 M3 §3.6 独立提案 `support-extended-thinking` 的范围。
- **不新增独立 `backend.type=deepseek`** —— 不带模型映射 / 默认 base_url（scope B 已被否决）。DeepSeek-over-anthropic 本质就是 `AnthropicAPIBackend` + 不同 `base_url`/`api_key` + 发 disabled。
- **不动 `ContentBlock` 判别联合**、不动 cassette keying 算法、不改 `extended_thinking` capability。
- **不把 `disable_thinking` 默认设为 `True`** —— 绝不静默改真 Anthropic 请求行为。
- **不给 `health_check` 加 thinking 注入** —— `health_check` 只 ping、不调 `model_validate`，thinking 块不影响它（详见 Failure Modes「doctor 对 DeepSeek」行）。
- 不引入新依赖（`extra_body` 是 anthropic SDK 既有入参）。

## Impact

- Affected specs: `llm-backend-protocol`（ADDED 行为需求）、`core-services`（MODIFIED 字段集需求）
- Affected code:
  - `src/hostlens/core/config.py`（`BackendSettings` 加字段）
  - `src/hostlens/agent/backends/anthropic_api.py`（构造接收 + `messages_create` 注入 `extra_body` + `model_validate` 错误归一）
  - `src/hostlens/agent/backend.py`（`create_backend` 透传 `disable_thinking`）
  - `tests/`（单测断言注入/不注入 + 错误归一两 kind；新增 `@pytest.mark.live` DeepSeek 多轮集成测试）
- Migration: None（新字段可选、默认 `False`，既有配置与行为不变）

### Failure Modes

| 故障场景 | 表现 | 降级行为 |
|---|---|---|
| `disable_thinking=True` 但 provider 不认 `thinking:disabled`（未来某端点 / 部分模型 / 漏配开关） | 仍返回 thinking 块 → `model_validate` 的 `content[*]` 判别联合 tag 失败 | `messages_create` 捕获包装成 `BackendError(kind="unsupported_content_block", message 提示可能漏配/provider 未遵守, cause)`，归一到 `BackendError` 故障域；loop fail-loud 上抛、CLI 边界兜成一行错误；**不**裸抛 pydantic traceback |
| SDK 升级 / 端点响应格式漂移（非 thinking 块）导致 `model_validate` 失败 | 字段缺失 / 新枚举 / 结构变化 | 同一 except 据 `exc.errors()` **判别后**包装成 `BackendError(kind="invalid_response", cause)`（**不**误标成 thinking 问题），保留 cause 供排障 |
| 误把 `disable_thinking=True` 配在真 Anthropic 端点 | Anthropic 接受 `thinking:disabled`（非 thinking 模型为 no-op；**预期，未单独实测**，由 5.1 顺带验证） | 无害；请求成功，响应仍只含 text/tool_use |
| `disable_thinking=False`（默认）下接 DeepSeek 类端点 | provider 默认吐 thinking → 解析崩（现按上面归一成 `BackendError`） | 预期；提示用户须显式设 `disable_thinking: true`（文档说明） |
| `hostlens doctor` 对 DeepSeek 端点报不健康 | `health_check` 用 `agent.health_check_model`（默认 `claude-haiku-4-5`，DeepSeek 不认） | **与 disable_thinking 正交**：`health_check` 只 ping、不 `model_validate`，thinking 块不影响它；要 doctor 对 DeepSeek 健康，需把 `agent.health_check_model` 配成 DeepSeek 模型（用户配置项，不在本提案修） |

### Operational Limits

无新增运维约束。`extra_body` 注入是 dict 字面量构造、错误归一是 except 分支，零额外 IO / 内存 / 并发开销；不改 `messages_create` 的 timeout / 重试 / 并发模型（沿用 `add-llm-backend-protocol` 既有约束）。

### Security & Secrets

无新增密钥、无新增暴露面。DeepSeek 类端点的 `api_key` 走既有 `BackendSettings.api_key`（`SecretStr`，序列化脱敏路径与 Anthropic key 一致）；`base_url` 走既有 `BackendSettings.base_url`（日志脱敏沿用 `core.redact`）。`disable_thinking` 是布尔开关，非敏感。`extra_body` 内容固定为 `{"thinking":{"type":"disabled"}}`，不含任何用户数据或凭据。错误归一保留的 `cause`（pydantic `ValidationError`）经 `BackendError.__str__` 的 `redact_text` 脱敏后输出。

### Cost / Quota Impact

正向（略微降低成本）：抑制 thinking 后 provider 不再生成 thinking token，单次调用 output token 反而减少。对真 Anthropic（默认 `False`）零影响。CI 全程走单测 mock / `not live`，零 API 调用。

### Demo Path

```bash
# 配置一个指向 DeepSeek 的 backend（disable_thinking 开启）
export HOSTLENS_BACKEND__TYPE=anthropic_api
export HOSTLENS_BACKEND__API_KEY=<deepseek-token>
export HOSTLENS_BACKEND__BASE_URL=https://api.deepseek.com/anthropic
export HOSTLENS_BACKEND__DISABLE_THINKING=true
export HOSTLENS_AGENT__PRIMARY_MODEL=deepseek-v4-flash

# 跑一次 intent（落地后）：Agent 多轮 tool 循环不再因 thinking 块崩解析
hostlens inspect local-host --intent "检查这台机器的健康状况"
# 期望：正常出报告，无 ValidationError；对照 disable_thinking 未设时会崩在响应解析（现归一成一行 BackendError）
```

CI 无成本回归路径：`pytest tests/ -m 'not live'` 全绿（单测 mock SDK 断言注入/不注入 + 续轮注入 + `model_validate` 失败按 kind 归一成 `BackendError` + `extended_thinking` 恒 False；cassette replay 锁定 thinking-free 多轮响应的解析）。**provider 是否恒守 disabled 由 `pytest -m live` 在本地/上线前手动验证，CI 不覆盖外部服务行为。**
