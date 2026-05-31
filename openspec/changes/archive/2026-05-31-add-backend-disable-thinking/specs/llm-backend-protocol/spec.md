## 新增需求

### 需求:`AnthropicAPIBackend` 必须支持 `disable_thinking` 抑制开关

`AnthropicAPIBackend` 必须新增构造参数 `disable_thinking: bool = False`（在既有「`AnthropicAPIBackend` 必须完整实现 `LLMBackend` 且关闭 SDK 内部重试」需求所定义的构造签名之上扩展一个可选参数，默认值保证既有调用方不变），控制每次 `messages_create` 是否指示 provider 抑制 extended-thinking 输出。此开关使「thinking 默认开」的 anthropic 兼容端点（如 DeepSeek v4 经 `https://api.deepseek.com/anthropic`）可作为 M2 backend 使用，而其 `type="thinking"` 内容块不会撞坏只建模 `text` / `tool_use` 的 `MessageResponse` 判别联合。

`AnthropicAPIBackend` 必须遵守：

- `disable_thinking=True` 时，底层 SDK `messages.create` 调用必须带上 `extra_body={"thinking": {"type": "disabled"}}`
- `disable_thinking=False`（默认）时，**禁止**向 SDK 调用添加任何 thinking 相关字段（既不传 `extra_body` 的 thinking 字段、也不传原生 `thinking=` kwarg；真 Anthropic 请求形状逐字不变）
- 无论开关取值，`capabilities.extended_thinking` 必须保持 `False`（本开关抑制 provider 默认的 thinking，**不**启用 Hostlens 对 thinking 输出的消费）
- `extra_body` 注入必须在 `check_capability_consistency(...)` 之后、SDK 调用入参组装时进行，且不影响 prompt_caching / tool_use 的 capability gate（gate 只扫 `system`/`messages`/`tools`，不扫 `extra_body`）
- `health_check` **不**注入 disabled（它只 ping、不调 `model_validate`，thinking 块不影响它）

#### 场景:disable_thinking 开启时注入 thinking:disabled

- **当** 调用 `AnthropicAPIBackend(disable_thinking=True).messages_create(...)`
- **那么** 底层 SDK `messages.create` 调用必须收到 `extra_body={"thinking": {"type": "disabled"}}`
- **并且** `check_capability_consistency(...)` 的调用时机与参数不受影响（注入发生在 capability gate 之后），且 `extra_body` 只含 `thinking` 键、不改写 `model`/`system`/`messages`/`tools`/`max_tokens`

#### 场景:disable_thinking 关闭时不改请求形状

- **当** 调用 `AnthropicAPIBackend(disable_thinking=False).messages_create(...)`（默认）
- **那么** 底层 SDK `messages.create` 调用**禁止**含任何 thinking 相关字段（`extra_body` 的 thinking 字段与原生 `thinking=` kwarg 都不出现）
- **并且** 请求形状必须与本变更前逐字一致

#### 场景:thinking 默认开的端点多轮 tool 循环可用

- **当** `disable_thinking=True` 的 backend 指向一个 thinking 默认开的 anthropic 兼容端点，并跑多轮 tool 循环（tool_use → tool_result → 续轮）
- **那么** 每一轮响应必须只含 `text` / `tool_use` 块（无 `thinking`）
- **并且** 续轮**禁止**因缺失 thinking 块而返回 HTTP 400

### 需求:`create_backend` 必须透传 `disable_thinking`

`create_backend` 在 `backend.type == "anthropic_api"` 分支必须读取 `settings.backend.disable_thinking` 并传入 `AnthropicAPIBackend` 构造。其他 backend type（不针对 thinking 默认开的端点）不受影响。

#### 场景:工厂透传配置开关

- **当** `create_backend` 从 `backend.disable_thinking == True` 的 settings 构造 `AnthropicAPIBackend`
- **那么** 构造出的 backend 在后续 `messages_create` 调用上必须注入 `thinking:disabled`

### 需求:响应解析失败必须归一成 `BackendError` 并按成因区分 kind

`AnthropicAPIBackend.messages_create` 在把 SDK 响应转成 `MessageResponse`（`model_validate`）时，若抛 `pydantic.ValidationError`，必须捕获并包装成 `BackendError`（保留 `cause` + 诊断 `message`），**禁止**让裸 `ValidationError` 传播。

包装的**作用**是把第三方/SDK 异常**归一到 `BackendError` 故障域**：包装后的 `BackendError`（`kind` 不属可重试族）按 agent-loop spec「不可重试 backend 异常直接上抛」由 Agent loop **fail-loud 原样上抛**，最终由 CLI 边界呈现为一行错误（非 pydantic traceback）。归一的价值是「类型稳定 + `__str__` 已脱敏 + 携带结构化 kind」，**不是**「被 loop 优雅处理 / 分类重试」。

包装必须按成因区分 `kind`（**禁止**一刀切）：

- 若 `ValidationError` 命中 `content[*]` 的判别联合 discriminator 类错误（出现未建模 block 类型，如 provider 未遵守 `thinking:disabled` 仍返回 `type="thinking"`；判别按「loc 落 `content` + discriminator 类错误，含 `union_tag_invalid` / `union_tag_not_found`」，不钉死单一 tag 名）→ `kind="unsupported_content_block"`。
- 其它校验失败（字段缺失 / 新枚举 / 结构漂移等）→ `kind="invalid_response"`（**禁止**把这些误标成 `unsupported_content_block`，以免把 SDK 不兼容指向 thinking 问题）。

`kind` 是 `BackendError` 的自由字符串参数，新增取值**无需**修改 `core/exceptions.py`。本需求**不**要求建模 `thinking` 块（仍由 `support-extended-thinking` 负责）。

#### 场景:未建模块导致解析失败 → unsupported_content_block

- **当** backend 收到含未建模 content block（如 `type="thinking"`）的响应，`model_validate` 因 `content[*]` 判别联合 tag 失败
- **那么** `messages_create` 必须 raise `BackendError(kind="unsupported_content_block")`（含 backend_name 与 cause）
- **并且** 禁止裸抛 `pydantic.ValidationError`

#### 场景:非内容块的格式不符 → invalid_response

- **当** `model_validate` 因非 content-block 原因失败（如缺 `usage` 字段 / `stop_reason` 出现未知枚举）
- **那么** `messages_create` 必须 raise `BackendError(kind="invalid_response")`（含 backend_name 与 cause）
- **并且** 禁止误标成 `unsupported_content_block`
