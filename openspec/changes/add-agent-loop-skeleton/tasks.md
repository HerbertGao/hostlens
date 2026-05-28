## 1. 数据模型与异常

- [ ] 1.1 `core/exceptions.py`：新增 `UnexpectedStopReason(HostlensError)`，携带 `stop_reason` 值，`__str__` 含该值；加入 `__all__`
- [ ] 1.2 `agent/loop.py`：定义 `LoopUsage`（Pydantic frozen：input_tokens/output_tokens/cache_creation_input_tokens/cache_read_input_tokens，默认 0）
- [ ] 1.3 `agent/loop.py`：定义 `ToolInvocation`（frozen：tool_name/tool_use_id/input；output 与 error 二选一，至少一个非空的 model_validator）
- [ ] 1.4 `agent/loop.py`：定义 `LoopResult`（frozen：final_text/tool_invocations/turns/terminal_status: Literal[7值]/usage_totals/stop_reason）

## 2. AgentLoop 骨架与构造

- [ ] 2.1 `agent/loop.py`：`AgentLoop(backend, tool_adapter, settings)` 构造，backend 存为私有属性；不 import anthropic
- [ ] 2.2 构造期校验：`settings.agent is None` → raise `ConfigError`（D-7；不静默默认、不延迟到 run）
- [ ] 2.3 内部常量：重试上限（rate-limited 3 / unavailable 3）、固定退避秒数、指数退避序列 1/4/16、`messages_create` timeout=60.0，各加 WHY 注释对齐 ARCHITECTURE §9
- [ ] 2.4 `_inject_cache_control(system, capabilities)`：prompt_caching=True 且 system 为 list[dict] 时在最后一个 block 注入 ephemeral；否则原样返回

## 3. 主循环

- [ ] 3.1 `run(intent)`：起始 messages + `tool_adapter.list_for_agent()` 取 tools；turn 计数与 usage 累加器
- [ ] 3.2 主 `while`：发起 `messages_create`（注入后的 system）→ `_track_usage` → 按 stop_reason 分支
- [ ] 3.3 `stop_reason` 穷举（D-8）：`end_turn`（有内容→`ok` / 空→`empty_response`）、`tool_use`（→§4）、`refusal`→`empty_response`、`max_tokens`→`degraded_token_budget`、`stop_sequence`/`pause_turn`→raise `UnexpectedStopReason`
- [ ] 3.4 `tool_use` 分支：并行 dispatch（见 §4）→ 追加 assistant 消息 + tool_result user 消息 → continue
- [ ] 3.5 下一轮发起前的兜底闸：超 token 预算 → `degraded_token_budget`；达 max_turns → `degraded_max_turns`（先收尾，绝不再调用）

## 4. 并行 tool dispatch 与错误分流（按 `dispatch` 真实契约，D-5）

- [ ] 4.1 幻觉名前置拦截：`_dispatch_one` 先查 `block.name ∈ list_for_agent() 名字集`；不在 → 不调 dispatch，回灌「无此工具」`is_error` tool_result + 记 `ToolInvocation(error=...)`（KeyError 有歧义不能靠它判别，见 D-5）
- [ ] 4.2 name 在集内 → 调 `tool_adapter.dispatch(block.name, block.input)`（返回 `dict`、自带 ctx via context_factory、自带 timeout）；返回 dict 不匹配 envelope 签名 → 正常 tool_result + 记 `ToolInvocation(output=...)`
- [ ] 4.3 返回 dict 匹配 error envelope 签名（`is_error is True` 且含 `error_kind`+`message` 键，非裸 `is_error`）→ 映射 `tool_result(is_error=True)` + 记 `ToolInvocation(error=...)`，**不二次 scrub**
- [ ] 4.4 dispatch raise `TypeError`（malformed args）→ 循环捕获 + `scrub_exception_message` 后回灌 `is_error` tool_result + 记 `ToolInvocation(error=...)`，continue
- [ ] 4.5 dispatch raise `KeyError`（name 已确认注册 → handler 内部 bug）/ `ToolPolicyViolation` / `asyncio.CancelledError` → 不捕获，原样向上传播（fail-loud）
- [ ] 4.6 同 turn 多 block 用 `asyncio.gather` 并行（不 `return_exceptions=True`）；结果按 `tool_use_id` 一一对应组装

## 5. Backend 故障处理（§9 Failure Semantics）

- [ ] 5.1 `messages_create` 调用包装 `_call_with_retry`：捕获 `BackendRateLimited` → honor retry_after（None 用固定退避），≤3 次
- [ ] 5.2 捕获 `BackendUnavailable` → 指数退避 1/4/16，≤3 次
- [ ] 5.3 `BackendError(kind 不可重试)` 与 `BackendCapabilityViolation` → 不重试，原样上抛
- [ ] 5.4 rate-limited 超限 → `degraded_rate_limited` 收尾；unavailable 超限 → 有结果 `degraded_no_planner` / 无结果 `failed_api_unavailable`

## 6. 单元测试（零 API；happy-path 用 `FakeBackend`，故障/调用计数用本地 scripted backend，D-9）

- [ ] 6.0 `tests/agent/` 内定义 `_ScriptedBackend`（实现 `LLMBackend` Protocol：按 `events: list[MessageResponse | Exception]` 依次返回或 raise，自增 `calls` 计数 + 留存最后一次 `messages`）；happy-path 用既有 `FakeBackend(responses=[...])`
- [ ] 6.1 单轮 end_turn → ok；tool_use→end_turn 两轮（断言 messages 含 assistant tool_use + user tool_result）
- [ ] 6.2 并行 dispatch：两个 ToolUseBlock 结果各归其 id；其中一个失败被隔离、另一个成功
- [ ] 6.3 错误分流（D-5）：handler 异常 envelope 回灌且不二次 scrub / malformed args(TypeError) 回灌 / 幻觉工具名（不在 advertise 集，dispatch 前拦截）回灌 / 已注册工具 handler 内部 KeyError 向上抛 / `ToolPolicyViolation` 向上抛
- [ ] 6.4 cache_control gate：prompt_caching False 不注入（FakeBackend 覆盖 capabilities）/ True 注入 ephemeral
- [ ] 6.5 兜底：token 预算超限只调用一次；max_turns=2 持续 tool_use 停在 2 轮（用 `_ScriptedBackend.calls` 断言）
- [ ] 6.6 故障：限流重试后成功 / 持续限流 → degraded_rate_limited；首轮 unavailable 无结果 → failed_api_unavailable / 有结果 → degraded_no_planner
- [ ] 6.7 不可重试：BackendCapabilityViolation 与 `BackendError(kind="auth_invalid")` 原样上抛
- [ ] 6.8 stop_reason（D-8）：空 end_turn→empty_response / refusal→empty_response / max_tokens→degraded_token_budget / stop_sequence→UnexpectedStopReason
- [ ] 6.9 构造：`settings.agent is None`→ConfigError；含 AgentSettings→构造成功
- [ ] 6.10 LoopResult schema：terminal_status 越界 ValidationError；usage_totals 多轮累加正确

## 7. 收尾

- [ ] 7.1 `mypy --strict src/hostlens/agent/loop.py` 与 `tests/agent/test_loop.py` 0 错误，无裸 `Any`
- [ ] 7.2 `ruff check` + `pytest -m 'not live'` 全绿
- [ ] 7.3 对抗性 review（CLAUDE.md §5.3：含运行时行为的新代码，应跑 `/review-loop-codex`）→ APPROVE/CLEAR 后开 PR
- [ ] 7.4 PR `feat/add-agent-loop-skeleton` → main，描述含 spec 引用与 Demo Path
