# Tasks: add-backend-disable-thinking

## 1. 配置字段

- [x] 1.1 `src/hostlens/core/config.py`：`BackendSettings` 新增 `disable_thinking: bool = False`，带 docstring 说明用途（抑制 thinking-默认-开的 anthropic 兼容端点输出）
- [x] 1.2 单测：`disable_thinking` 默认 `False`；env (`HOSTLENS_BACKEND__DISABLE_THINKING`) 与 YAML 的布尔解析覆盖多取值（`true`/`false`/`1`/`0` 至少各一），确认 pydantic-settings 嵌套 bool 解析正确

## 2. AnthropicAPIBackend 注入 + 错误归一

- [x] 2.1 `src/hostlens/agent/backends/anthropic_api.py`：`__init__` 新增 `disable_thinking: bool = False`，存为 `self._disable_thinking`
- [x] 2.2 `messages_create`：在 `check_capability_consistency(...)` 之后、组装 SDK 调用入参时，若 `self._disable_thinking` 为 `True` 则带上 `extra_body={"thinking": {"type": "disabled"}}`；否则**既不传 `extra_body` 的 thinking 字段、也不传原生 `thinking=` kwarg**（单一注入路径）
- [x] 2.3 确认 `capabilities.extended_thinking` 仍为 `False`（不随开关变化）；在 `extended_thinking` 字段或 `disable_thinking` 处加注释说明「disable_thinking 是抑制 provider 默认 thinking、非启用消费，故与 extended_thinking=False 不矛盾」
- [x] 2.4 **不改** `__repr__`（按 spec：`disable_thinking` 非敏感，且现有 repr 有脱敏断言测试，加字段反而可能破坏既有断言）
- [x] 2.5 `messages_create` 错误归一：把 `MessageResponse.model_validate(sdk_message.model_dump())` 包进 `try/except ValidationError`（文件顶部 `from pydantic import ValidationError`），**按成因区分**（不一刀切）：检查 `exc.errors()`，含 `content[*]` 判别联合 discriminator 类错误（`union_tag_invalid` / `union_tag_not_found` 等，按「loc 落 content + discriminator 类」判别，不钉死单一 tag 名）→ `BackendError("...unmodeled content block (provider may have ignored thinking:disabled)...", backend_name=self.name, kind="unsupported_content_block", cause=exc)`；否则 → `BackendError("...response failed MessageResponse validation (possible SDK/endpoint format drift)...", backend_name=self.name, kind="invalid_response", cause=exc)`。`kind` 自由字符串，无需改 `core/exceptions.py`；**不**建模 thinking 块

## 3. 工厂透传

- [x] 3.1 `src/hostlens/agent/backend.py`：`create_backend` 的 `anthropic_api` 分支读 `backend_settings.disable_thinking` 并传入 `AnthropicAPIBackend(...)`

## 4. 单元测试（默认 CI 跑）

- [x] 4.1 mock SDK client：`disable_thinking=True` → `messages.create` 调用 kwargs 含 `extra_body={"thinking":{"type":"disabled"}}`
- [x] 4.2 mock SDK client：`disable_thinking=False`（默认）→ 调用 kwargs **既不含 `extra_body` 的 thinking 字段、也不含原生 `thinking=` kwarg**（断言保护真 Anthropic 路径逐字不变）
- [x] 4.3 `create_backend` 把 `BackendSettings.disable_thinking=True` 正确透传到构造的 backend（注入行为可观测）
- [x] 4.4 capability 一致性校验与注入互不干扰（`extra_body` 不含 cache_control，不触发 `BackendCapabilityViolation`）
- [x] 4.5 多轮注入回归（不依赖真实 DeepSeek）：mock SDK，对同一 `disable_thinking=True` backend **连续两次** `messages_create`，断言两次调用 kwargs 都带 `extra_body`。**注意**：本测试仅证明「我方每次调用都注入」，**不**证明 provider 续轮真不吐 thinking（那是 5.1 live 的职责）；CI 绿 ≠ 多轮 provider 行为已验证
- [ ] 4.6 cassette replay 解析回归：用 `PlaybackBackend` 回放一段 thinking-free 多轮响应（user→tool_use→tool_result→续轮），断言解析 + Agent loop 跑完。**cassette 来源**：跑 5.1 live 时顺带 `HOSTLENS_LLM_MODE=record` 录制，或用真 Anthropic 录一段 thinking-free 多轮（锁的是我方解析非 DeepSeek 特有行为）；**禁止手写多轮 cassette**；CI 只回放不录制
- [x] 4.7 错误归一 + 守卫单测（①②③④ 完成；④ 端到端 CLI 已实现：tests/cli/test_inspect_intent.py::test_intent_thinking_block_fails_loud_one_line_no_traceback，归一后 BackendError 经真实 AgentLoop fail-loud 上抛 → CLI `except Exception` → 单行 `internal: BackendError: ...` exit 2、无 traceback、无 pydantic）：① 构造含 `type="thinking"` 块的假 SDK 响应 → 断言 `messages_create` raise `BackendError(kind="unsupported_content_block")` 而非裸 `ValidationError`；② 构造非内容块格式不符的假响应（如缺 `usage` / `stop_reason` 未知枚举）→ 断言 raise `BackendError(kind="invalid_response")`（**不**被误标成 unsupported_content_block）；③ `AnthropicAPIBackend(disable_thinking=True).capabilities.extended_thinking is False`（防止未来误把 capability 翻 True）；④ 端到端：用会吐 thinking 的 fake backend 跑 `hostlens inspect`，断言退出码非 0、输出一行错误、**无** pydantic traceback（锁「归一后 loop fail-loud 上抛 → CLI 兜底」链路，防未来有人给 loop 加宽泛 `except BackendError` 把它静默降级）。**注**：CLI 边界「一行/无 traceback」是既有契约（`inspect.py` 的 `except Exception`→exit 2）；若实现时发现 CLI 实际打 traceback，记为既有缺陷的 follow-up，不在本提案 scope

## 5. 集成测试（live 标记，CI 默认跳）

- [x] 5.1 `@pytest.mark.live` 测试：对真实 DeepSeek 端点（pro + flash）跑多轮 tool 循环（user → tool_use → tool_result → continuation），断言每轮响应只含 `text`/`tool_use`、续轮零 400；顺带对真 Anthropic 端点发 disabled 断言无害（同测试加 param）
- [x] 5.2 凭据从环境变量读取，缺失时 `pytest.skip`（不读 cc-switch 库，不把 token 写进 fixture/仓库）
- [x] 5.3 把 design.md「验证数据」依据的两个探针脚本固化入仓 `tests/manual/deepseek_thinking_probe.py` / `deepseek_multiturn_probe.py`（凭据从环境读、不读 cc-switch），让 design 表证据可复现；design.md 引用仓内路径而非 `/tmp`。**确保 `tests/manual/` 不被 CI 的 pytest 收集**（命名/conftest 排除 + 不在模块顶层连真实端点），仅供手动跑

## 6. 收尾

- [x] 6.1 `mypy --strict` 通过
- [x] 6.2 `ruff` lint + format 通过
- [x] 6.3 文档：在 backend 配置文档说明 `disable_thinking`（接 DeepSeek 等 thinking-默认-开端点时设 `true`），并提醒 `agent.health_check_model` 需配成端点支持的模型否则 doctor 报不健康；若仓内已有示例 `config.yaml` 模板则同步注释，没有则只补 docs（不为此新建模板文件）
- [ ] 6.4 （非代码 follow-up，实现 subagent 可忽略）更新 reference memory `deepseek-v4-thinking-incompatible-live-test`：开关落地后「as-is 仍崩」改为「配 `disable_thinking: true` 即可用」
