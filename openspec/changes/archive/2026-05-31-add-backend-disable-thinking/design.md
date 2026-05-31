# Design: add-backend-disable-thinking

## 验证数据（实施前已证实）

探针（2026-05-31 手测，deepseek-v4-pro / flash 各一次，CC Switch `DeepSeek` provider `https://api.deepseek.com/anthropic`）。探针脚本随实现入仓 `tests/manual/`（见 tasks 5.3）；下表为当时手测结果，**可靠性保证由 tasks 5.1 入仓 live 测试承担，不靠这张一次性表**：

| 探针 | deepseek-v4-pro | deepseek-v4-flash |
|---|---|---|
| baseline（不带参数） | `['thinking']` | `['thinking','text']` |
| `thinking:disabled` | `['text']` | `['text']` |
| tool + disabled（turn1） | `['text','tool_use']` | `['text','tool_use']` |
| 多轮（turn2，回 tool_result 续轮，全程 disabled） | `['text']` stop=end_turn | `['text']` stop=end_turn |

结论（手测范围内）：`extra_body={"thinking":{"type":"disabled"}}` 在 pro/flash 两模型、单轮与多轮 tool 循环下抑制了 thinking 块，且**续轮不 400**（disabled 后没有 thinking 块要回传，不存在「漏回传必需块」问题）。这推翻了 reference memory 早期「关闭开关疑似无效」的假设。注意：这是一次性手测，**不构成「provider 未来恒守 disabled」的保证** —— 见「风险与回滚」的 CI 覆盖边界与 D-5 兜底。

## 决策

### D-1：用配置开关 `disable_thinking`，不新增 backend 类型（scope A）

DeepSeek-over-anthropic 与真 Anthropic 的主要差异是 `base_url` + `api_key` + 需要发 disabled（model id 命名空间 / 计费 / 限流行为也不同，但不影响本提案 scope）。`AnthropicAPIBackend` 已支持 `base_url`/`api_key` 注入，因此只差一个抑制 thinking 的开关。新增 `DeepSeekBackend` 会与 `AnthropicAPIBackend` ~90% 重复，且 backend 矩阵扩展本排在 M10.5（CLAUDE.md §4.11）。符合 §8「架构清晰度 > 功能广度」。

**否决**：scope B（独立 `backend.type=deepseek` + 模型映射）。

### D-2：注入点在 `messages_create`，用 SDK 既有 `extra_body`

`thinking` 不是 M2 `LLMBackend` Protocol 签名的参数（Protocol 是 Anthropic-schema-first 的 M2 子集，extended_thinking=False）。在不扩 Protocol 签名的前提下，最小改动是 backend 实现内部按自身 `disable_thinking` 实例状态决定是否注入。用 anthropic SDK 既有的 `extra_body` 透传 `{"thinking":{"type":"disabled"}}`，不引入新依赖、不改 Protocol 形状。

- `disable_thinking=True` → SDK 调用带 `extra_body={"thinking":{"type":"disabled"}}`。
- `disable_thinking=False`（默认）→ **完全不传 `extra_body` 的 thinking 字段、也不传原生 `thinking=` kwarg**（保护真 Anthropic 路径逐字不变）。单一注入路径只走 `extra_body`，避免与 SDK 原生 `thinking` 参数两路并存时 deep-merge 行为含糊。

**注入点精确位置**：`extra_body` 必须作为 `self._client.messages.create(...)` 的 kwarg 构造，位于现有 `messages_create` 的 try 块**内**（与 `model`/`system`/`messages`/`tools` 同处），使任何 SDK 调用异常仍走既有 `except anthropic.*` 分类包装。**注意**：`MessageResponse.model_validate(...)` 在 try 块**外**（约 215 行），注进 try 块**不**能保护解析阶段的 thinking 块异常 —— 那条路径由 D-5 单独兜。`check_capability_consistency(...)` 仍在 try 块前执行，顺序不变。

> 注意：`extended_thinking` capability 仍为 `False`。本开关是「抑制 provider 默认开的 thinking」，不是「启用并消费 thinking」——语义上与 capability 不冲突，capability 描述的是「Hostlens 是否消费 thinking 输出」，答案仍是否。

### D-3：开关位置与透传链；health_check 正交

`BackendSettings.disable_thinking: bool = False` → `create_backend` 读出后传入 `AnthropicAPIBackend(..., disable_thinking=...)` → backend 存为实例状态 → `messages_create` 据此注入。与现有 `base_url` / `health_check_model` 透传完全同形。

`health_check` **不**注入 disabled，也不需要：它只 `await self._client.messages.create(...)` ping 一次、**不调 `MessageResponse.model_validate`**，所以 thinking 块不会让它崩。`health_check` 对 DeepSeek 端点真正会失败是因为默认 `health_check_model="claude-haiku-4-5"`（DeepSeek 不认该 model id）—— 那是**与 disable_thinking 正交的配置项**（用户把 `agent.health_check_model` 配成 DeepSeek 模型即可），不在本提案修。

### D-4：能力一致性校验顺序不变

`check_capability_consistency(...)` 仍先于 SDK 调用执行（且只扫 `system`/`messages`/`tools`，**不**扫 `extra_body`），`extra_body` 注入在校验之后、SDK 调用入参组装时进行。`extra_body` 不含 `cache_control`，对 prompt_caching 校验零影响。

### D-5：响应解析失败必须归一成 `BackendError`，并按成因区分 kind

`messages_create` 的 `MessageResponse.model_validate(sdk_message.model_dump())` 当前在 try 块**外**（约 215 行），任何校验失败都会抛**裸 pydantic `ValidationError`**。该异常不是 `Backend*` 类型 —— Agent loop 的 `_call_with_retry` 只 `except BackendRateLimited / BackendUnavailable`，裸 `ValidationError` 会逃逸到 `run()` 顶、再到 CLI，暴露 SDK 内部结构。本提案把 `model_validate` 包进 `try/except ValidationError`，**归一成 `BackendError`**：

- **作用准确表述**：归一不是「让 loop 接住分类」——`BackendError(kind="unsupported_content_block" / "invalid_response")` 不是可重试族，loop 据 agent-loop spec「不可重试 backend 异常直接上抛」**fail-loud 原样上抛**（这是对的）；真正给用户一行干净错误的是 **CLI 边界**（`inspect.py` 的 `except Exception` → exit 2，无 traceback）。归一的价值是「类型稳定 + `__str__` 已脱敏 + 携带结构化 kind」，而非「被 loop 优雅处理」。

- **必须按成因区分**（不能一刀切标 thinking）：`ValidationError` 成因不止「未建模块」——SDK 升级改字段、`stop_reason` 新枚举、`usage` 结构变化等都会抛它。实测两类可区分：未建模块（thinking）→ `content[*]` 上的 union discriminator 类错误（`type == "union_tag_invalid"`；缺 `type` 字段则 `union_tag_not_found`），`loc` 落在 `content` 上；已建模块缺字段 / 其它 → `type == "missing"` 等、`loc` 不在 content 判别层。except 内检查 `exc.errors()`（按 loc 落 `content` + union discriminator 类错误判别，**不**钉死单一 tag 名）：
  - 含 `content[*]` 的判别联合 discriminator 类错误（`union_tag_invalid` / `union_tag_not_found` 等）→ `BackendError("response contains an unmodeled content block (provider may have ignored thinking:disabled)", backend_name=self.name, kind="unsupported_content_block", cause=exc)`。
  - 否则 → `BackendError("backend response failed MessageResponse validation (possible SDK/endpoint format drift)", backend_name=self.name, kind="invalid_response", cause=exc)`。

`BackendError` 构造签名 `(message="", *, backend_name, kind=None, cause=None)`（见 `core/exceptions.py`）；`kind` 是**自由字符串、无 enum 约束**，新增取值**无需改 `exceptions.py`**。实现需在 `anthropic_api.py` 顶部 `from pydantic import ValidationError`。这是把 TODO M3.6「近期兜底」前置到本提案 —— 本开关正是让用户开始指向第三方端点的路径，该失败模式由本提案引入暴露面，就该本提案兜住。**不**建模 thinking 块（仍是 Non-Goal）。

## 测试策略

- **单测（默认 CI 跑）**：mock `anthropic.AsyncAnthropic` 的 `messages.create`，断言注入/不注入、工厂透传、`model_validate` 失败按 kind 归一、`extended_thinking` 恒 False。
- **集成测试（`@pytest.mark.live`，CI 默认 `-m 'not live'` 跳过）**：对真实 DeepSeek 端点跑多轮 tool 循环，断言两模型 turn1/turn2 零 thinking 块、零 400；顺带断言真 Anthropic 端点收 disabled 无害（成本极低）。凭据从环境读取，缺失时 skip。

## 风险与回滚

- 风险极低：默认 `False`，既有行为零变更；唯一新行为路径需显式配置 `disable_thinking: true`。
- 回滚：删除字段 + 注入分支 + D-5 except 即可，无数据迁移、无持久化状态。
- **CI 覆盖边界（诚实声明）**：
  - 单测（mock）保证**我方代码**不回归：每轮都注入 disabled、`model_validate` 失败按 kind 归一成 `BackendError`、`extended_thinking` 恒 False。
  - cassette replay（`PlaybackBackend` 回放一段 thinking-free 多轮响应）锁定**我方解析 + Agent loop 处理**不回归。该 cassette 由 5.1 live 同一次真实会话录制产出（或用真 Anthropic 录一段 thinking-free 多轮，因为锁的是「我方解析 thinking-free 响应」而非 DeepSeek 特有行为），**禁止手写多轮 cassette**（易格式对语义错）。
  - **「provider 行为本身是否恒守 disabled」CI 无法覆盖**（外部服务行为），只由 tasks 5.1 的 live 测试本地/手动跑确认；建议接 DeepSeek 上线前、以及 SDK / 端点版本变更后各跑一次 `pytest -m live`。
- 跨提案边界：D-5 只保证「不裸抛、归一到 `BackendError` 故障域」。`BackendError`（尤其新 kind）最终如何向用户呈现，取决于 M2 Agent loop / CLI 的错误展示逻辑（现状：loop fail-loud 上抛 → CLI `except Exception` → exit 2 一行），**本提案不改 loop / CLI**；tasks 4.7 加端到端断言锁住「不被误降级、最终一行无 traceback」。
