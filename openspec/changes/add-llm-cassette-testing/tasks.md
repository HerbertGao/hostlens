## 1. `core.redact` 共享敏感规则下沉（D-3）

- [x] 1.1 在 `src/hostlens/core/redact.py` 新增 `CASSETTE_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...]`：把 `scripts/cassette_lint.py` 内联的 `SENSITIVE_PATTERNS` 9 条规则（`anthropic_or_openai_sk_key` / `bearer_token` / `jwt` / `credential_assignment` / `user_home_path` / `ssh_path` / `ipv4_address` / `email_address` / `hostname_or_fqdn`）原样搬入；验收：每条 `(name, compiled_regex)` 与原 lint 字面一致
- [x] 1.2 在 `src/hostlens/core/redact.py` 实现 `def detect_sensitive_text(text: str) -> str | None`：按 `CASSETTE_SENSITIVE_PATTERNS` 顺序匹配，命中返回首个规则名，否则 `None`；docstring 说明它与 `redact_text` 的语义区别（检测 vs masking、cassette 门禁更宽 vs runtime 保留 HOME）
- [x] 1.3 把 `__all__` 从 `["redact_text"]` 扩为 `["redact_text", "detect_sensitive_text", "CASSETTE_SENSITIVE_PATTERNS"]`；**禁止**改动 `redact_text` / `is_sensitive_key` / `_mask` 的任何现有行为
- [x] 1.4 写 `tests/core/test_detect_sensitive_text.py`：覆盖 spec §需求:`hostlens.core.redact` 必须暴露 cassette 共享敏感规则 的 4 个场景（命中返回规则名：每条 pattern ≥1 正例 / 干净文本返回 None：`"hello world, connection refused"` + model id `"claude-opus-4-8"` 不误伤 / `redact_text` 行为对 `/Users/alice` 与改前一致 / lint 与 recorder 同源一致判定）；验收：测试 pass

## 2. request-key 算法抽共享 helper + playback 行为等价重构（D-4）

- [x] 2.1 新建 `src/hostlens/agent/cassette_key.py`：`def request_key_for_payload(model: str, messages: list[dict], tools_count: int) -> str` **逐字抽取**自现 `PlaybackBackend._request_key`（把现有实现整段搬过来，**不**重新拟定序列化参数措辞——契约是「输出与重构前逐字节相等」，由 2.3 golden 钉死）；无副作用、不 import backend；验收：`mypy --strict` 过
- [x] 2.2 重构 `src/hostlens/agent/backends/playback.py`：`_request_key` 与 `_record_request_key` 改为调用 `cassette_key.request_key_for_payload`，行为等价；验收：现有 `tests/agent/`（含 playback）全绿
- [x] 2.3 写 `tests/agent/test_cassette_key_golden.py`：覆盖 spec §需求:request-key 算法必须单一来源 的 2 个场景——(a) 三处同源：对固定 payload `request_key_for_payload` 与 `PlaybackBackend` 查找路径产出一致；(b) golden：对一组固定 payload 的 hash 等于硬编码 golden 值（钉死重构前后不变）；验收：测试 pass

## 3. `cassette_lint.py` 改同源 import + 重复 key 检测（D-3 / D-4）

- [x] 3.1 `scripts/cassette_lint.py`：删除内联 `SENSITIVE_PATTERNS`，改为 `from hostlens.core.redact import CASSETTE_SENSITIVE_PATTERNS, detect_sensitive_text`；`scan_line_for_sensitive_substrings` 改用 `detect_sensitive_text`（保留 `redact_text` baseline 兜底）；验收：现有 `tests/test_cassette_lint.py` 全绿（行为不变）
- [x] 3.2 在 `scripts/cassette_lint.py` scan 模式增加同文件内重复 request-key 检测：用 `from hostlens.agent.cassette_key import request_key_for_payload`（不再复制算法、也不 import playback backend）对每条 record 的 `request` 算 key，同文件出现重复 → `LintError`（exit 1，stderr 指出文件 + key 前缀）
- [x] 3.3 写 `tests/test_cassette_lint_duplicate_key.py`：覆盖 spec §需求:`cassette_lint.py` secret-scan 必须检测同文件内重复 request-key 的 2 个场景（重复 key exit 1 / 多轮不同 key 通过）；验收：测试 pass

## 4. `RecordingBackend` + 真实 target 守门（D-1 / D-2 / D-7）

- [x] 4.1 新建 `tests/support/__init__.py` 与 `tests/support/cassette_recording.py`；定义 `class RecordingBackend`：`__init__(self, *, cassette_path: Path, inner: AnthropicAPIBackend)`；实现 `LLMBackend` Protocol（`name="recording"`、`capabilities` 透传 `inner.capabilities`）；内部 `_records: list[dict]` + `_poisoned: bool = False`。`__init__` 顺序：**先**检测 `os.environ` 含 `PYTEST_XDIST_WORKER` → raise（xdist 下禁 record）、**再**检查 active-path 注册表同 path 已存在 → raise、**最后**把 `cassette_path` 注册进进程内注册表（模块级 set）作为 `__init__` 末步——保证「注册后无可失败操作」，无需注册后 rollback；若未来在注册后新增初始化，必须 try/except rollback 注销
- [x] 4.2 实现 `async def messages_create(...)`：调 `inner.messages_create` 拿 response → 用 `cassette_key.request_key_for_payload` 同源逻辑把 request 投影成 canonical `{model, messages, tools_count}` → 对 **canonical request 序列化文本与 response 序列化文本都**跑 `detect_sensitive_text`，任一命中则**置 `_poisoned=True` 后** raise（含规则名、不回显原值、**不**累积该 record、**不**写盘）；任何其他异常同样置 poisoned 后传播 → 干净则 append `{"request":..., "response":..., "tools_schema_hash": sha256(json.dumps(tools, sort_keys=True).encode())}` 到 `_records`，返回 response（**注意**：`tools_schema_hash` 用 default `ensure_ascii` 以对齐 `llm-backend-protocol` §drift 既有 CI 计算口径，**不**用 `ensure_ascii=False`）
- [x] 4.3 实现 scenario 结束写盘 + 资源释放：`def flush(self) -> None` —— **若 `_poisoned` 为 True 或已 flush 过：仅从 active-path 注册表注销、不写盘（no-op）**；否则把 `_records` 原子写盘（写 `cassette_path.with_suffix(".tmp")` 后 `os.replace` rename 覆盖整文件）再注销；**禁止** append；幂等（重复调用安全）；用 try/finally 保证即便写盘异常也注销注册表；构造失败路径也 rollback 注册表；由 `llm_cassette` fixture teardown 调用
- [x] 4.4 在 `tests/support/cassette_recording.py`（或同层）实现 `def guard_record_targets(target_registry, *, allow_real: bool) -> None`：遍历 registry（`list()` + `get_entry(name).tags`），判定真实/合成——`type ∈ {ssh, docker, k8s}` 一律真实；`type == local` 仅当 `TargetEntry.tags` 含固定字符串 `"cassette-synthetic"` 才算合成，否则视为真实；存在任一真实 target 且 `allow_real is False` → raise，错误信息含 `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1` 且不回显 host / 凭据。**注意**：守门在此装配层 helper，**不**在 `RecordingBackend.messages_create`（签名看不到 target）
- [x] 4.5 写 `tests/support/test_recording_backend.py`：覆盖 spec §需求:`RecordingBackend` 必须内存收集整个 scenario...（多轮 3 record 且 key 互不同 / 重录覆盖整文件 / 中断不留半写 / recorder 产物必带 tools_schema_hash）+ §需求:写盘前必须对 request 与 response 都跑敏感检测门禁（response 命中拒绝 / **request 的 tool_result 命中拒绝** / 干净写盘）+ §需求:任一检测门禁命中或调用异常后 recorder 必须进入 poisoned 状态（**门禁命中后 teardown flush 不写盘且释放 active path** / flush 幂等）+ §需求:record 模式必须防止同一 cassette 路径被并发/多实例覆盖（同 path 第二个 recorder fail-fast / xdist fail / **构造失败后同 path 能再次成功构造——证明 xdist/collision 检查在注册前、无 stale path 残留**）；用 `FakeBackend` 或 monkeypatch 的 inner 避免真调 API；验收：全部 pass
- [x] 4.6 写 `tests/support/test_guard_record_targets.py`：覆盖 spec §需求:record 模式必须由 fixture 强制在装配层拒绝真实 target 的 helper 层 4 个场景（默认拒 SSH 且不回显 host / 带 `cassette-synthetic` 标记的 local 放行 / 裸 local 无标记被拒 / `allow_real=True` 放行）；验收：全部 pass

## 5. `llm_cassette(name)` fixture + `HOSTLENS_LLM_MODE`（D-5 / D-6）

- [x] 5.1 在 `tests/conftest.py` 新增 `_resolve_llm_mode() -> Literal["replay","record","live"]`：读 `os.environ.get("HOSTLENS_LLM_MODE", "replay")`，空串等同 replay，非法值 raise（含合法取值列表）
- [x] 5.2 在 `tests/conftest.py` 实现 `llm_cassette` fixture（工厂式 `def _make(name: str, target_registry=None) -> LLMBackend`）：按 mode 分派——replay→`PlaybackBackend(cassette_path=cassettes/<name>.jsonl)`（文件缺失 raise 含期望路径，`target_registry` 忽略）；record→校验 `ANTHROPIC_API_KEY`（缺则 `pytest.fail`）；**`target_registry` 缺失则 `pytest.fail`**；**在返回 `RecordingBackend` 前先调 `guard_record_targets(target_registry, allow_real=os.environ.get("HOSTLENS_ALLOW_REAL_TARGET_RECORD")=="1")`**（守门由 fixture 强制，不靠测试体自觉）；注册 teardown 调 `flush()`；live→`AnthropicAPIBackend`（不写盘）；cassette 路径恒为 `tests/fixtures/cassettes/<name>.jsonl`（显式名，**禁止** nodeid 派生）
- [x] 5.3 写 `tests/agent/test_llm_cassette_fixture.py`：覆盖 spec §需求:`HOSTLENS_LLM_MODE`... 的 3 个场景（缺省=replay / 非法值 fail / `create_backend` 不感知 mode）+ §需求:`llm_cassette(name)` fixture 必须按显式名映射 的 4 个场景（replay 返回 PlaybackBackend / 文件缺失报错含路径 / record 缺 key fail / live 不写盘）+ §需求:record 模式必须由 fixture 强制...拒绝真实 target 的 2 个 fixture 层场景（**fixture 强制守门无法绕过：record + 含 SSH 的 registry → 取 backend 即 raise** / **record 缺 target_registry 即 fail**）；用 monkeypatch 设 env；验收：全部 pass
- [x] 5.4 验收 §场景:生产工厂不感知 mode：单测设 `HOSTLENS_LLM_MODE=record` 后断言 `create_backend(settings)` 仅按 `settings.backend.type` 分派（grep 工厂处确认无 `HOSTLENS_LLM_MODE` 字样）

## 6. 示范 cassette + replay 闭环 + 往返确定性

- [x] 6.1 搭一个**字节稳定**的合成多轮 scenario fixture：`target_registry` 含一个 local target 且其 `TargetEntry.tags` 带 `"cassette-synthetic"`（使 record 模式 guard 放行）+ 合成 inspector_registry，让 Planner 走 ≥2 轮 tool-use；合成 tool_result 冻结时钟/UUID/用户名/路径（参考 `tests/integration/test_tool_registry_demo_path.py` 的 stub 装配）
- [x] 6.2 写 `tests/agent/test_planner_replay.py`：用 `llm_cassette("planner_health_check", target_registry=<6.1 的合成 registry>)` 注入 backend 跑 Planner（**record 模式下 fixture 据此 registry 过 guard、replay 模式忽略 registry**），断言报告结构稳定；replay 消费 `tests/fixtures/cassettes/planner_health_check.jsonl`
- [x] 6.3 写往返确定性测试（覆盖 spec §需求:合成 fixture 必须字节稳定，record→replay 往返不得 miss §场景）：用 monkeypatch 的 inner（FakeBackend 固定响应）走 record 写临时 cassette，再 replay 同 scenario，断言无 `CassetteMiss`；验收：测试 pass（不需真 API）
- [ ] 6.4 用 record 模式录制正式 cassette（本地，需 API key）：`HOSTLENS_LLM_MODE=record ANTHROPIC_API_KEY=... pytest tests/agent/test_planner_replay.py`（该测试体已通过 6.2 传入带 `cassette-synthetic` 标记的合成 registry，故 record 模式 guard 放行、不缺 registry）；录后 `python scripts/cassette_lint.py` exit 0（证明录完即过 lint）；commit cassette 文件
- [ ] 6.5 验收闭环：默认 `pytest tests/agent/test_planner_replay.py` 在无 API key 下 replay 通过；若 Planner 多轮 fixture 成本过高，按 design Open Question 降级单轮示范并在 PR 说明（M2.8 补多轮）

## 7. 文档

- [x] 7.1 更新 `tests/fixtures/cassettes/README.md` 「Recording flow (manual, M2)」段：从「M2 无 recorder、手写」改为「`HOSTLENS_LLM_MODE=record pytest tests/...::test_x`」录制流程；补充三态 mode 说明、显式命名约定、`HOSTLENS_ALLOW_REAL_TARGET_RECORD` 风险标注、「request 不 scrub / 只跑字节稳定合成输入 / 命中即 fail 不 scrub」治理约束、parametrize 须并入显式 name
- [x] 7.2 验收：README 不再含「M2 has no recorder」字样；含 `HOSTLENS_LLM_MODE` 与 `HOSTLENS_ALLOW_REAL_TARGET_RECORD` 说明

## 8. CI / 静态检查

- [x] 8.1 确认 CI 测试 job 不依赖 `ANTHROPIC_API_KEY`（默认 replay）；若 CI 已有 cassette_lint 步骤则确认仍 exit 0；验收 spec §需求:CI 默认必须以 replay 模式运行 的 2 个场景（无 key 默认路径全绿 / cassette miss 暴露为红，不回落）
- [x] 8.2 `ruff check . && ruff format --check .` exit 0
- [x] 8.3 `mypy --strict src/ scripts/cassette_lint.py` exit 0（含新增 `src/hostlens/agent/cassette_key.py`；`tests/support/` 按项目既有 test 类型检查范围处理）
- [x] 8.4 `pytest -m 'not live'` 全绿；`pre-commit run --all-files` exit 0

## 9. Git 工作流（CLAUDE.md §5.1）

- [ ] 9.1 feature branch `feat/add-llm-cassette-testing`；`git add` 显式文件（禁 `git add -A`）；conventional commit 含 change name 引用
- [ ] 9.2 commit 后、push 前按 §5.3 跑对抗性 review（本提案含运行时行为 + 安全检测边界，属「应该跑 review」类）；triage + 修复到 APPROVE/CLEAR
- [ ] 9.3 push branch + `\gh pr create --base main`；PR 描述含 spec 引用 `openspec/changes/add-llm-cassette-testing/` 与 Demo Path
- [ ] 9.4 CI 全绿 + review 后 `\gh pr merge <num> --squash --delete-branch`
- [ ] 9.5 准备归档：`openspec-cn validate add-llm-cassette-testing` 确认可归档；后续 `/opsx:archive` 推进到 `openspec/specs/llm-cassette-testing/`
