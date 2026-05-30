## 为什么

M2.1（`add-llm-backend-protocol`）已交付 cassette **回放半边**：`PlaybackBackend` 按 `SHA256({model, messages, tools_count})` 查 cassette、miss → `CassetteMiss`（绝不回落真 API）、`scripts/cassette_lint.py --check-schema-drift` 软警告、`create_backend` 支持 `backend.type=playback`。但 **录制半边缺失** —— 现在要新增一条 cassette 只能手写完整的 Anthropic `MessageResponse`（`id` / `usage` / `content[]` block / `stop_reason`），对有 tool-use 循环的多轮场景极易写出「格式对、语义错」的假数据（典型：`tool_use.id` 与 `tool_result.tool_use_id` 对不上，或写的是你*期望* LLM 调的工具而非它*真会*调的）。测试因此通过，但覆盖的是一个**不存在的 LLM 行为**。

CLAUDE.md §2 把「LLM 调用走 cassette 回放」列为测试基线，M2.8 incident pack 的 8 个场景每个都要 fixture + snapshot —— 没有可靠的录制路径，这些 cassette 要么手写易错、要么各自为政。M2.6 是把「录 → 检测门禁 → 回放」这条闭环补齐、并把测试侧的 backend 选择统一到一个 `llm_cassette()` fixture 的节点。

本提案的定位刻意收窄：**录制器是 pytest-only 的开发者测试工具，不是生产 backend 模式**（经 Codex + Claude 双评审收敛）。

## 变更内容

新增 `llm-cassette-testing` capability，把 M2.1 的回放半边补成录-放闭环：

- **录制器（薄 wrapper，test-support 层）**：`RecordingBackend` 包一层真实 `AnthropicAPIBackend`，拦截 `messages_create` 的入/出，**在内存中收集一个 scenario 的全部 LLM calls**，测试结束时**原子 overwrite 整个 cassette 文件**（一文件 = 一 scenario，含多轮多 record；每条 record 必带 `tools_schema_hash` 供 drift 检测）。写盘前对 **canonical request 与 response 都**跑同源**敏感检测门禁**（detect-and-reject：命中即 fail 不落盘，**不**做静默 scrub）。任一门禁命中或调用异常后 recorder 进入 **poisoned** 状态，teardown 的 `flush()` 退化为只注销不写盘（幂等），避免把残缺 scenario 写成 cassette。**禁止 append**。同一 run 内同 `cassette_path` 出现第二个 active recorder / 处于 `pytest-xdist` 并发时 **fail-fast**（防多实例覆盖丢数据）。

- **`HOSTLENS_LLM_MODE=record|replay|live` 三态，只活在 `llm_cassette()` fixture 内**：
  - `replay`（**CI 默认**）：构造 `PlaybackBackend(cassette_path=...)`，miss → fail，不烧 API。
  - `record`（本地 opt-in）：构造 `RecordingBackend`，需 `ANTHROPIC_API_KEY`，写盘前过检测门禁。
  - `live`（本地调试）：直接用 `AnthropicAPIBackend`，真调不写盘。
  - **关键架构决定**：`HOSTLENS_LLM_MODE` **不**接进生产工厂 `create_backend()`（后者已按 `Settings.backend.type` 分派；再叠一个全局 env mode 会制造两个冲突的 backend 来源）。三态切换是测试 fixture 的私有职责。

- **`llm_cassette(name)` pytest fixture（显式传名）**：`name` 是语义化稳定标识（如 `planner_health_check`），映射到 `tests/fixtures/cassettes/<name>.jsonl`。返回按 mode 选好的 backend 实例，测试注入 `AgentLoop`。**不**按 test nodeid 自动映射（rename/拆分/parametrize 会让 cassette 路径无意义 churn，且 review 看不出测试绑定哪个语义场景）。

- **录制输入治理（张力1 的真正解法 = 方案 A）**：cassette 测试**只跑合成 target / 合成 tool_result**，真实 hostname / IP / 路径 / 用户名**不得进入 `messages`**，且合成输入**必须字节稳定**（冻结时钟 / UUID / 用户名 / 路径——否则非确定值进 messages 会让 record 与 replay 端 hash 不一致而 `CassetteMiss`）。理由：request-key 把 `messages` 进 hash，若录制时脱敏 messages 则 record 端 hash(scrubbed) 与 replay 端 hash(raw) 永不相等。治理把矛盾从源头消除，request 侧**不 scrub、不改 keying 契约**。配套护栏：(a) helper `guard_record_targets(target_registry, *, allow_real)` 拒绝真实 target（除非显式 `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1`）——**不**在 `RecordingBackend` 做（`messages_create` 签名看不到 target），且**由 `llm_cassette` fixture 在 record 模式强制调用**（fixture 要求传入 `target_registry`，取 backend 这一步即过守门，测试无法忘记绕过）；(b) 写盘前 request+response 都过检测门禁，命中即 fail。

- **脱敏 pattern 同源（张力3）**：把 `cassette_lint.py` 内联的那套比 runtime 更宽的敏感规则下沉到 `hostlens.core.redact`，导出 `CASSETTE_SENSITIVE_PATTERNS` 与 `detect_sensitive_text(text) -> str | None`（命中返回规则名，否则 None）。`cassette_lint.py` 与 `RecordingBackend` **同源 import**，保证「录完即过 lint」。**不**扩大 `redact_text()` 的 runtime masking 语义（runtime 允许保留 HOME / 路径等非 secret 信息，与 cassette 提交门禁标准不同）。

- **CI 默认 replay 验收**：CI 跑全套测试时 `HOSTLENS_LLM_MODE` 缺省 → replay，零 API 消耗；并在 lint 阶段对全部 cassette 跑 secret-scan。

## 功能 (Capabilities)

### 新增功能

- `llm-cassette-testing`: cassette 录制器（`RecordingBackend`：内存收集 scenario 全部 LLM calls + 原子 overwrite + 每条带 `tools_schema_hash` + 写盘前 request&response 检测门禁 + 多实例/xdist fail-fast）、装配层 `guard_record_targets` 拒真 target helper、`HOSTLENS_LLM_MODE` 三态（仅在 `llm_cassette()` fixture 内生效，不进 `create_backend`）、`llm_cassette(name)` 显式命名 fixture、录制输入治理约束（合成输入字节稳定 / request 不 scrub / 不改 keying）、共享 keying helper `cassette_key.request_key_for_payload`（playback/recorder/lint 同源）、`hostlens.core.redact` 共享 `CASSETTE_SENSITIVE_PATTERNS` + `detect_sensitive_text`、`cassette_lint.py` secret-scan + 重复-key 检测与 recorder 同源、CI 默认 replay。

### 修改功能

无 spec-level 修改。`PlaybackBackend` 的 keying **算法/契约** / cassette 格式 / `CassetteMiss` / `create_backend` 工厂分派的**行为**均**不**改变；本提案只在其上补「录制」与「测试侧 backend 选择」两个 M2.1 未覆盖的新维度。唯一触及 `playback.py` 源码的是**行为等价重构**——把内联 keying 算法抽到共享 `cassette_key.request_key_for_payload` 并改为调用它（golden test 钉死 hash 不变），算法字面与 `llm-backend-protocol` spec 描述完全一致，故不构成 spec 级修改。

## 影响

- **对外契约影响**：
  - **Agent tool schema / Inspector / MCP / Notifier / Schedule / CLI 契约**：均无变更。
  - **cassette 文件格式**：无变更（recorder 写出的就是 M2.1 已规约的 `{request, response, tools_schema_hash}`；`request` 仍是投影后 canonical 子集；recorder 把 `tools_schema_hash` 从「可选」收紧为「产物必带」，不改格式只收紧填充约束）。
  - **`PlaybackBackend` keying 行为**：无变更（行为等价重构为调用共享 helper，golden test 钉死 hash 不变）。
  - **`create_backend` 工厂签名 / `Settings.backend`**：无变更（三态 mode 不进工厂）。
- **代码**：
  - 新增：`src/hostlens/agent/cassette_key.py` —— `request_key_for_payload(model, messages, tools_count)`，keying 单一来源。
  - 改：`src/hostlens/agent/backends/playback.py` —— 内联 keying 改调 `cassette_key.request_key_for_payload`（行为等价重构 + golden test）。
  - 改：`src/hostlens/core/redact.py` —— 新增 `CASSETTE_SENSITIVE_PATTERNS` + `detect_sensitive_text`（搬迁 + 暴露，不动 `redact_text` 语义）。
  - 改：`scripts/cassette_lint.py` —— 内联 `SENSITIVE_PATTERNS` 改从 `core.redact` import；新增同文件重复-key 检测（用共享 helper）；secret-scan 既有行为不变。
  - 新增：`RecordingBackend`（test-support 层，如 `tests/support/cassette_recording.py`）+ `guard_record_targets` helper —— 内存收集 + 原子写盘 + 每条带 `tools_schema_hash` + 写盘前 request&response 检测门禁 + 多实例/xdist fail-fast；真 target 守门在装配层 helper。
  - 新增：`tests/conftest.py` 的 `llm_cassette(name)` fixture + `HOSTLENS_LLM_MODE` 解析。
  - 新增：1 个示范 cassette（用 record 模式录一次 Planner 多轮 scenario 或沿用 `list_inspectors_demo`）+ 对应 replay 测试。
  - 文档：`tests/fixtures/cassettes/README.md` 更新「录制流程」段（从「M2 无 recorder、手写」改为「`HOSTLENS_LLM_MODE=record pytest ...`」）。
- **依赖**：无新增 runtime 依赖（`RecordingBackend` 在 test-support 层，仅 dev/test 路径触达）。
- **不触碰行为**：`AnthropicAPIBackend`、`create_backend`、`Settings`、`PlaybackBackend` 的 keying 行为与 `CassetteMiss` 契约。

## 非目标（Non-Goals）

- **不**改 `PlaybackBackend` 的 request-key 算法 / keying 契约（方案 B 被否决：会把 request-key 从「协议输入」变成「脱敏器输出」，未来任一 pattern 改动让全部 cassette miss）。
- **不** scrub（静默替换）request 或 response（方案 C 被否决作默认）：cassette 写盘安全靠「合成输入前置治理（字节稳定）+ 写盘前 request&response **检测门禁**（命中 fail 不落盘）+ lint」三道，而非事后 in-place 脱敏——scrub request 会改 keying，scrub response 会篡改真实响应让 cassette 失真。
- **不**把 `RecordingBackend` 做成生产 backend：不进 `create_backend` 工厂矩阵、不进 `backend.type` 枚举、不实现 `BackendDiagnostics`、与 daemon 模式无关（pytest-only，daemon 永不触达）。
- **不**做 `HOSTLENS_LLM_MODE` 与生产 `create_backend` 的耦合（避免双 backend 来源）。
- **不**做 CLI 录制工具 / 交互式重录 UI / 录制进度面板（`HOSTLENS_LLM_MODE=record pytest ...` 即录制入口）。
- **不**做 cache hit rate / token usage 的可观测面板（M2.5 已划归后续可观测性专项）。
- **不**录制真实生产 target 的数据（除非显式 `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1`，且即便如此 request&response 仍过检测门禁）。
- **不**实现 M2.8 incident pack 本身的 8 个 cassette —— M2.6 只交付录制基建 + ≥1 示范 cassette；8 个场景由 M2.8 用本基建录制。

## Failure Modes

1. **`record` 模式无 `ANTHROPIC_API_KEY`**：`llm_cassette()` fixture 构造 `RecordingBackend` 前显式校验，缺 key → 立即 `pytest.fail`（doctor 式清晰报错，不是录到一半 401）。
2. **`replay` 模式 cassette 文件不存在 / scenario name 写错**：`PlaybackBackend` 构造时文件缺失 → 清晰报错指出期望路径 `tests/fixtures/cassettes/<name>.jsonl`；运行中 key miss → `CassetteMiss`（M2.1 既有），fail-fast 不回落真 API。
3. **录制时合成 fixture 仍漏真实信息**（Codex 点出的隐坑：即便 FakeTarget，`tool_result` 也可能带入 tmp path / 当前用户名 / hostname / 时间戳 / 随机 id，且会进下一轮 request `messages`）：写盘前对 **canonical request 与 response 都**跑 `detect_sensitive_text`，命中 → `RecordingBackend` raise、**不落盘**，错误信息指出命中的规则名（不回显敏感值）。lint 是 CI 最后防线，不是设计依赖。
4. **非确定合成输入致 record→replay miss**：合成 fixture 含时钟/UUID/用户名/路径等非确定值时，messages 字节不稳定让 replay 端 hash 不匹配。治理要求合成输入冻结这些值；spec「record→replay 往返不 miss」场景守住。
5. **同 cassette 文件出现重复 request-key**（手写编辑失误，recorder 因 overwrite 不会产生）：`cassette_lint.py` secret-scan 阶段用共享 keying helper 检测同文件内重复 key → exit 1（避免 `PlaybackBackend` 静默取第一个、其余轮被吞）。
6. **同名 cassette 多实例覆盖**（parametrize 未并入 name / rerun / xdist）：record 模式进程内 active-path 注册表对同 path 第二个 recorder fail-fast；xdist 下禁 record。
7. **record 模式被误用于真实 target**：装配层 `guard_record_targets` 检测 target 非合成且无 `HOSTLENS_ALLOW_REAL_TARGET_RECORD=1` → fail，防止真实拓扑数据进 cassette（不在 backend 层做，因 `messages_create` 看不到 target）。

## Operational Limits

- **并发预算**：record 模式串行录制单 scenario（多轮 loop 顺序调用），不引入新并发；replay 是纯本地文件查找，O(records) 线性，无并发。
- **内存预算**：`RecordingBackend` 在内存累积单 scenario 的 request/response（典型多轮 ≤ 数十 KB），测试结束即写盘释放；不跨 scenario 累积。
- **超时设置**：record / live 模式沿用 `AnthropicAPIBackend` 既有 timeout；replay 无网络、无超时概念。
- **写盘**：原子 overwrite（写临时文件 + rename），避免录制中断留下半写 cassette。

## Security & Secrets

- **新密钥**：不引入。record / live 复用既有 `ANTHROPIC_API_KEY`（仅本地 opt-in 路径读取，CI 默认 replay 不读）。
- **检测门禁**：request 与 response 写盘前都过 `detect_sensitive_text`（同源于 lint 的 `CASSETTE_SENSITIVE_PATTERNS`，比 runtime `redact_text` 更宽：sk- key / Bearer / JWT / credential= / `/Users|/home` 路径 / `.ssh` / IPv4 / email / hostname-FQDN）；命中即 fail 不落盘（检测-拒绝，非静默 scrub）。
- **攻击面**：不扩大。`RecordingBackend` 在 test-support 层，生产运行时与 daemon 永不加载；cassette 内容是过了检测门禁的合成对话，提交 git 前再过 lint。
- **合规**：`HOSTLENS_ALLOW_REAL_TARGET_RECORD=1` 是显式危险开关，文档标注其风险（真实拓扑可能入 cassette），默认关闭。

## Cost / Quota Impact

- **CI**：零 API 消耗（默认 replay，不读 `ANTHROPIC_API_KEY`）—— 这正是本提案的核心收益之一。
- **record**：一次性，录一个 scenario = 该 scenario 的真实多轮调用（典型几次 `messages_create`），仅在开发者本地显式 `HOSTLENS_LLM_MODE=record` 时发生。
- **live**：开发者本地调试用，按真实多轮计费，不写盘、不进 CI。
- **配额**：对 Anthropic 配额净影响 ≈ 录制频次（低频、人工触发），远低于「每次 CI 都真调」的反面方案。

## Demo Path

无需 SSH、无需付费 API（replay 路径优先）：

```bash
pip install -e ".[dev]"

# 1) CI 默认路径：全套测试走 replay，零 API 消耗
pytest -m 'not live'                                   # 全绿；LLM 测试用 PlaybackBackend

# 2) lint 守门：全部 cassette 过 secret-scan（recorder 与 lint 同源 pattern）
python scripts/cassette_lint.py                        # exit 0

# 3) 读 recorder 的输入治理与三态切换（面试现场可展示「为什么不脱敏 request」）
sed -n '/录制流程/,/EOF/p' tests/fixtures/cassettes/README.md

# 4)（可选，本地 opt-in，需 ANTHROPIC_API_KEY）录一个示范 scenario 再回放，证明录-放闭环
HOSTLENS_LLM_MODE=record ANTHROPIC_API_KEY=sk-... pytest tests/agent/test_planner_replay.py -v
python scripts/cassette_lint.py                        # 录完即过 lint（同源 pattern）
pytest tests/agent/test_planner_replay.py -v           # 默认 replay 回放刚录的 cassette，结果稳定
```
