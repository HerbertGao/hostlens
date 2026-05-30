## 上下文

M2.1（`add-llm-backend-protocol`）已把 cassette 的**回放半边**做完并归档进 `llm-backend-protocol` spec：

- `PlaybackBackend`：request-key = `SHA256(json.dumps({model, messages, tools_count}, sort_keys=True, ensure_ascii=False))`；cassette 的 `request` 字段已是投影后 canonical 子集；miss → `CassetteMiss`（绝不回落真 API）；JSON 格式 / schema 校验。
- `scripts/cassette_lint.py`：secret-scan（内联一套比 runtime `redact_text` 更宽的 `SENSITIVE_PATTERNS`）+ `--check-schema-drift` 软警告。
- `create_backend` 工厂：`backend.type=playback` + `cassette_path`。

**录制半边缺失**：新增 cassette 只能手写完整 `MessageResponse`，多轮 tool-use 场景极易写出语义错的假数据。本设计补齐「录 → 检测门禁 → 回放」闭环。

本设计的所有关键决策经 Codex + 另一 Claude 双评审收敛；两处被评审纠正的方向（多轮 cassette 不能「一文件一 record」、mode 不进生产工厂）已固化进下方 Decisions。

## 目标 / 非目标

**目标：**
- 一个薄 `RecordingBackend`：包 `AnthropicAPIBackend`，内存收集单 scenario 全部 LLM calls，原子 overwrite 写盘，写盘前对 request+response 跑检测门禁（命中 fail 不落盘，非 scrub）。
- `HOSTLENS_LLM_MODE=record|replay|live` 三态，仅在 `llm_cassette(name)` fixture 内分派。
- `core.redact` 暴露与 lint 同源的 `CASSETTE_SENSITIVE_PATTERNS` / `detect_sensitive_text`。
- CI 默认 replay，零 API 消耗。

**非目标：**
- 不改 `PlaybackBackend` keying 契约 / cassette 格式 / `create_backend`。
- 不 scrub（静默替换）request / `messages`（命中只 fail，不改写）。
- `RecordingBackend` 不进生产 backend 矩阵（pytest-only）。
- 不实现 M2.8 的 8 个 incident cassette（只交付基建 + ≥1 示范）。
- 不做 CLI 录制工具 / 可观测面板。

## 决策

### D-1 张力1：录制输入治理（方案 A），而非脱敏 request（B/C）

request-key 把 `messages` 进 hash。若 record 时脱敏 messages，则 record 端 `hash(scrubbed)` 与 replay 端活请求 `hash(raw)` 永不相等 → 必然 `CassetteMiss`。

**选 A：cassette 测试只跑合成 target / 合成 tool_result，真实 hostname/IP/路径/用户名不得进 `messages`。** request 侧不 scrub、不改 keying。安全由三道防线保证：(1) 合成输入前置治理（字节稳定）；(2) `RecordingBackend` 写盘前对 request+response 检测门禁（命中 fail，非 scrub）；(3) `cassette_lint` 提交门禁。

- **否决 B（对称脱敏）**：把 request-key 从「协议输入」变成「脱敏器输出」，未来任一 pattern 改动让全部 cassette miss；且 scrub 必须确定性幂等（同一 IP 在不同上下文 scrub 结果可能不一致）—— 太脆。
- **否决 C（只信任测试输入、不治理）**：request 字段会提交 git，lint 只能防 commit 不能防本地落盘；把安全全压到事后兜底。
- **Codex 点出的隐坑（已纳入 spec）**：即便 FakeTarget，`tool_result` 也可能带 tmp path / 当前用户名 / hostname / 时间戳 / 随机 id；而 `tool_result` 会进**下一轮 request `messages`**。两个后果：
  1. **检测门禁覆盖 request + response 两侧**（review round-1 blocker）：只检测 response 不够——`tool_result` 进 request 后既是漏检的 secret 通道，也会被写进 cassette。故 record 写盘前对 **canonical request 与 response 都**跑 `detect_sensitive_text`，命中即 fail 不落盘。这是**检测门禁**而非 scrub：命中 fail，**禁止**任何 in-place 替换（scrub request 改 keying、scrub response 篡改真实响应）。
  2. **合成输入必须字节稳定**（review round-1 blocker）：非确定值（时钟/UUID/用户名/路径）进 messages 会让 record 端与下次 replay 端 hash 不一致 → `CassetteMiss`。故合成 fixture 必须冻结这些值；spec 加「record→replay 往返不 miss」场景守住。
- **真实 target 守门放在装配层而非 backend 层**（review round-1 blocker）：`messages_create` 签名无 target/ToolContext，`RecordingBackend` 在 API 层看不到 target，无法判定真实/合成。故守门改为 helper `guard_record_targets(target_registry, *, allow_real)`，由能看到 `target_registry` 的 fixture / record-aware context factory 在跑 scenario 前调用（解决原 Open Question「如何判定 target 真实」）。

### D-2 张力2：一文件一 scenario（多轮多 record）+ 原子 overwrite，而非「一文件一 record」或 upsert

最初倾向「一文件一 record + overwrite」被 Codex 纠正：**Hostlens Agent loop 天然多轮，一个 scenario 有多次 `messages_create`**，「一文件一 record」会直接卡住多轮 cassette。

**决定**：一文件 = 一 scenario；`RecordingBackend` 内存按序收集该 scenario 全部 `(request, response)`；scenario 结束**原子 overwrite 整个 JSONL**（temp file + rename）；**禁止 append**。

- 多轮各 turn 的 `messages` 逐轮增长 → request-key 天然互不相同，不会自冲突。
- **否决 upsert by key**：与 JSONL 顺序格式不搭（要么退化成读-改-写全文件，要么逼改 JSON array）；overwrite 全文件已经覆盖「重录」语义，无需 upsert 复杂度。
- 重复 key 只可能来自手写误编辑 → 交给 `cassette_lint` 检测（D-4），`PlaybackBackend`「取第一个匹配」的既有行为不动。
- **多实例覆盖防护**（review round-1 major）：同一 run 内多个 recorder 指向同一 `cassette_path`（parametrize 未并入 name / rerun / xdist）会让 teardown 时最后写入者吞掉其他实例数据且静默无错。故 record 模式维护进程内 active-path 注册表，同 path 第二个 active recorder → fail-fast；检测 `PYTEST_XDIST_WORKER` → 禁 record（跨进程注册表不可共享）；文档要求参数化测试把参数并入显式 name。

### D-3 张力3：`core.redact` 拆共享检测规则，但不动 `redact_text` runtime 语义

`cassette_lint.py` 与 `RecordingBackend` 必须用同一套敏感规则，否则「录完立刻 lint 红」。

**决定**：把 lint 内联的更宽规则下沉到 `hostlens.core.redact`，导出 `CASSETTE_SENSITIVE_PATTERNS` + `detect_sensitive_text(text) -> str | None`；lint 与 recorder 同源 import。

- **关键约束（Codex）**：**不**把这套更宽规则并进 `redact_text()` 默认行为。runtime 日志脱敏允许保留 HOME / 路径等非 secret 信息，与 cassette 提交门禁标准不同。两套语义分开：`redact_text`（runtime masking，不变）vs `detect_sensitive_text`（cassette 检测，新增）。
- `redact_text` 现有 `__all__ = ["redact_text"]` 扩成含两个新符号；lint 删掉内联 `SENSITIVE_PATTERNS`。

### D-4 request-key 算法抽共享 helper，三处同源；重复 key 检测放进 lint

最初想「lint 复制 keying 算法、playback 一行不改」，被 review round-1 纠正：recorder（写 canonical request）、lint（重复检测）、playback（回放匹配）三处复制同一算法，任一处对 `ensure_ascii` / 投影细节漂移就会「lint 说无重复但 playback 实际冲突」或「recorder 写的 key 与 playback 读的不一致」。

**决定**：抽无副作用 helper `hostlens.agent.cassette_key.request_key_for_payload(model, messages, tools_count) -> str`，playback / recorder / lint 三处**共用**。`PlaybackBackend` 改为调用该 helper 是**行为等价重构**（算法字面不变），用 golden test 守住「重构前后对同一 payload 的 hash 完全一致」。

- 这放宽了原非目标「playback.py 一行不改」→「不改 keying **算法/契约**（playback 改调共享 helper，行为等价 + golden 守住）」。换来 keying 单一来源、漂移由构造消除，比「两份实现 + golden 比对」更稳。
- 同文件内重复 key 检测仍放 `cassette_lint.py` secret-scan 阶段（lint 本就遍历每行），用同一 helper 算 key，exit 1。

### D-4b recorder 产物必带 `tools_schema_hash`

M2.1 把 `tools_schema_hash` 定为**可选**只为兼容手写旧 cassette；它是 schema-drift 检测的唯一依据（keying 只用 `tools_count`）。recorder 没有兼容包袱，故其产物**必须**每条都写 `tools_schema_hash`，否则新录 cassette 天生失去 drift warning（review round-1 major）。**序列化口径对齐既有 CI**（review round-2）：用 `SHA256(json.dumps(tools, sort_keys=True))`（default `ensure_ascii`），与归档 `llm-backend-protocol` §drift 的 `--current-tools-hash` 计算口径一致——**不**用 `ensure_ascii=False`，否则非 ASCII tool schema 下 recorder 写入值与 CI 当前值分叉、误报 drift。（注意这与 request-key 的 `ensure_ascii=False` 是两套独立口径，各自对齐其消费方，互不要求一致。）

### D-5 `HOSTLENS_LLM_MODE` 只活在 `llm_cassette()` fixture，不进 `create_backend`

Codex 纠正的第二点：生产工厂已按 `Settings.backend.type` 分派，再叠一个全局 env mode 会制造**两个冲突的 backend 来源**。

**决定**：mode 解析 + backend 构造是 `llm_cassette()` fixture 的私有职责；`create_backend` 不读、不感知 `HOSTLENS_LLM_MODE`。这也让 `RecordingBackend` 天然留在 test-support 层（不进 `backend.type` 枚举 / 不进工厂矩阵 / 不实现 `BackendDiagnostics` / daemon 永不触达）。

### D-6 fixture 显式传名，不按 nodeid 自动映射

`llm_cassette("planner_health_check")` → `tests/fixtures/cassettes/planner_health_check.jsonl`。

- 否决 nodeid 自动映射：rename / 模块移动造成 cassette 路径无意义 churn；parametrize 名含特殊字符；测试拆分/合并使 cassette 生命周期不清；review 看不出测试绑定哪个语义场景。
- 可提供 helper 生成路径，但名称必须显式、语义化、稳定。

### D-7 `RecordingBackend` 放 test-support 层

放 `tests/support/`（或等价 test-only 位置），不放 `src/hostlens/agent/backends/`，强化「非生产 backend」边界。它 import 生产的 `AnthropicAPIBackend`（单向 tests → src），但生产运行时 / daemon 永不 import 它。

## 风险 / 权衡

- **合成 fixture 仍漏真实信息** → D-1 三道防线：合成输入治理（字节稳定）+ 写盘前 **request+response 检测门禁**（命中 fail 不落盘，非 scrub）+ lint 提交门禁。
- **`detect_sensitive_text` 误报**（合成数据里的合法 dotted token / 私网段被 hostname/IPv4 规则命中）→ 复用 M2.1 已调好的 lint 规则集（已对 model id / tool 名做过 FQDN 误伤规避），新增正/反例测试守住；误报时收紧规则而非放宽 cassette。
- **录制成本**（多轮真调）→ 仅本地 opt-in、人工低频触发；CI 永不录制。
- **`HOSTLENS_ALLOW_REAL_TARGET_RECORD=1` 被滥用** → 默认关闭、文档标注风险、即便开启 request+response 仍过检测门禁；request 侧真实数据入 cassette 的风险由「不开此开关」这一默认承担。
- **playback 行为等价重构引入回归** → golden test 钉死重构前后 hash 一致；M2.1 既有 playback 测试全绿作为回归网。

## Migration Plan

- 抽 `hostlens/agent/cassette_key.py` + 行为等价重构 `PlaybackBackend` 调用它（golden test 守住）。
- 新增 `RecordingBackend`（tests/support）/ `llm_cassette` fixture / `guard_record_targets` helper / `core.redact` 两个符号；`cassette_lint.py` 改 import 来源 + 加重复-key 检测。
- 既有 `list_inspectors_demo.jsonl` 与回放测试不受影响（replay 路径 / 格式不变）。
- 回滚：还原 `playback.py` 内联 keying、删除 `cassette_key.py` / fixture / RecordingBackend / guard helper / 两个 redact 符号，`cassette_lint.py` 恢复内联 patterns 即回到 M2.1 状态。

## Open Questions

- （已决策）示范 cassette 走「record 模式录一个 Planner 多轮 scenario」（证明多轮录-放闭环），合成 registry 带 `cassette-synthetic` 标记（tasks §6）；保留 fallback：若 apply 时 Planner 多轮 fixture 搭建成本过高，降级为单轮示范 + 留 M2.8 补多轮（tasks 6.5 已写明并要求在 PR 说明）。
- （已解决，review round-1 + round-3）「如何判定 target 真实」：由 `guard_record_targets` 在装配层判定——`ssh`/`docker`/`k8s` 一律真实；`local` 仅当 `TargetEntry.tags` 含固定标记 `"cassette-synthetic"` 才算合成（裸 local 指向真实本机，视为真实）。标记形态钉死为复用既有 `TargetEntry.tags`，不新增 target 属性，避免 apply 时临场选型。不在 backend 层做（看不到 target）。
