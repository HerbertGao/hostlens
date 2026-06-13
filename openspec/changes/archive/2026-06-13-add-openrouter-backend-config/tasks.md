## 1. 配置 schema（core-services）

- [x] 1.1 `BackendSettings` 新增 `extra_headers: dict[str, str] | None = None` 与 `prompt_caching: bool | None = None`（`core/config.py`，`extra="forbid"` 下两字段缺省 None，无 type 交叉校验）
- [x] 1.2 单测：`backend:` 无两字段 → 加载 exit 0 且均为 None；`HOSTLENS_BACKEND__EXTRA_HEADERS='{"HTTP-Referer":"https://x","X-OpenRouter-Title":"hostlens"}'` 加载为 dict；`HOSTLENS_BACKEND__PROMPT_CACHING=false` 加载为 `False`；非 `anthropic_api` type（如 playback）设两字段不报错（`tests/core/test_config.py`）
- [x] 1.3 文档/注释明确 `extra_headers` 定位为**非密钥统计 header**（不提供 `SecretStr` 级序列化保护）；脱敏义务在 backend `__repr__`（见 task 2.4），doctor `BackendHealthRow` 不输出 `extra_headers`（无需新增 doctor 脱敏测试——该输出面不存在）

## 2. AnthropicAPIBackend 改造（llm-backend-protocol）

- [x] 2.1 `capabilities` 从 `ClassVar[BackendCapabilities]` 改为 `__init__` 设置的实例属性，默认 `prompt_caching=True`、其余 6 字段固定（`agent/backends/anthropic_api.py`）
- [x] 2.2 `__init__` 新增 `prompt_caching: bool = True`，注入到 `self.capabilities.prompt_caching`
- [x] 2.3 `__init__` 新增 `extra_headers: dict[str, str] | None = None`；非 None 时透传 `anthropic.AsyncAnthropic(..., default_headers=extra_headers)`，None 时禁止传 `default_headers`
- [x] 2.4 `__repr__` 对 `extra_headers` **值无条件全遮蔽**为 `***`（keys 保留，不依赖 `core.redact` 形态识别）；单测断言 `{"X-Custom-Auth":"not-a-real-secret-PROBE-0001"}` 时 `repr(backend)` 不含 `not-a-real-secret-PROBE-0001` 子串、值显示为 `***`。**测试值必须选 `redact_text` 不命中的形态**（如此哨兵：非 `sk-`/Bearer/JWT/URL）——这样一个错误地走形态脱敏的实现会漏遮蔽该值而令断言变红，falsify 假绿；避免用真 token 形态值以免触发 secret scanner
- [x] 2.5 单测：默认构造 `capabilities` 等于既有 7 字段值（实例访问）；`prompt_caching=False` → `capabilities.prompt_caching is False` 其余不变；`extra_headers={...}` → SDK client 以 `default_headers` 构造；缺省不传 `default_headers`（`tests/agent/backends/test_anthropic_api.py`）
- [x] 2.6 回归：异常包装路径不受改造影响——既有 429 with retry-after 严格 honor、529→RateLimited、5xx/网络→Unavailable、auth→Error、API 宕机 degraded 的测试全绿（`pytest tests/agent/backends/test_anthropic_api.py -q`）

## 3. create_backend 接线（llm-backend-protocol）

- [x] 3.1 `create_backend` 读 `settings.backend.prompt_caching`（`None` → `True`）传入 `AnthropicAPIBackend(prompt_caching=...)`（`agent/backend.py`）
- [x] 3.2 `create_backend` 读 `settings.backend.extra_headers`，丢弃大小写不敏感的 `x-api-key` / `authorization` 键后传入；认证仍以 `api_key` 为唯一来源
- [x] 3.3 单测：`extra_headers={"x-api-key":"attacker","HTTP-Referer":"https://x"}` → 传入 backend 的仅 `{"HTTP-Referer":"https://x"}`（认证防覆盖）；`prompt_caching=False` 端到端 → `backend.capabilities.prompt_caching is False`（`tests/agent/test_create_backend.py`）

## 4. Agent loop 集成 + prompt cache 行为验证

- [x] 4.1 验证 `prompt_caching=False` 的 backend 下 Agent loop **不注入** `cache_control`（FakeBackend 注入 `capabilities=BackendCapabilities(prompt_caching=False, ...)`，断言出站 system/tools 无 `cache_control` block）；`prompt_caching=True` 时仍注入
- [x] 4.2 prompt cache hit rate 验证：真 Anthropic 默认路径 `cache_control` 仍注入、cache 行为回归绿；非 Claude 路径断言 payload 无 cache block（即「不缓存」是有意的、指标不再失真）
- [x] 4.3 `check_capability_consistency`：`prompt_caching=False` 时若 payload 误含 `cache_control` → raise `BackendCapabilityViolation`（既有门，补一条断言它对非 Claude 实例可触发）

## 5. 文档 + 手动验收

- [x] 5.1 `docs/ARCHITECTURE.md §9` OpenRouter 段补「非 Claude 端点须设 `prompt_caching: false`」与 `extra_headers` 配置示例；`.env.example` OpenRouter 模板对齐两字段
- [x] 5.2 真端点验收（2026-06-13 实测通过）：`base_url=https://openrouter.ai/api` + `prompt_caching=false` + `extra_headers` 下经 `qwen/qwen3.7-plus` 发真请求往返成功（`create_backend` 读真 `.env`，返回 `OPENROUTER_OK`、`capabilities.prompt_caching=False`、repr extra_headers 值全遮蔽 `***`、`cache_creation_input_tokens=0`）。**修正**：原写 `hostlens demo run cpu_saturation` 验证不了 OpenRouter——`hostlens demo` 离线写死 `PlaybackBackend`、不走 `create_backend`；真端点须走 `create_backend`/`hostlens inspect` agent 路径。运营发现（非缺陷）：deepseek/qwen 经 OpenRouter 延迟常 >5s 超 `doctor` health_check 5s 硬超时

## 6. 收尾

- [x] 6.1 `mypy --strict` 0 错、`ruff` 绿、`openspec-cn validate add-openrouter-backend-config --strict` 通过；MODIFIED 块在临时副本实测 `openspec-cn archive` 不中止（标题逐字对齐原 spec）
- [x] 6.2 `TODO.md` M10.6 三个验收子项勾掉，进度总览相应更新
