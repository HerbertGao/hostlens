# remediation-planner-agent 规范

## 目的
待定 - 由归档变更 add-remediation-planner 创建。归档后请更新目的。
## 需求
### 需求:`RemediationPlannerAgent` 必须复用 `AgentLoop` + 外部系统提示，串接于 Diagnostician 之后

系统必须提供 `RemediationPlannerAgent`——继 `PlannerAgent` / `DiagnosticianAgent` 之后的第三个 `AgentLoop`-backed Agent。它必须复用既有 `AgentLoop`（不另写 loop）、从外部 markdown 文件加载系统提示，且在编排上串接于 Diagnostician 之后：输入为带稳定 id 的 finding、根因假设与 target 名，产出为经 P1a `RemediationPlan` schema 校验的修复方案。系统提示模板缺失必须在构造期失败（fail-fast），不得延迟到 run 时。

#### 场景:系统提示模板缺失构造期失败
- **当** 构造 `RemediationPlannerAgent` 而其系统提示 markdown 资源缺失
- **那么** 构造立即抛出 `ConfigError`（与 `DiagnosticianAgent` 同款 fail-fast），不进入 run

#### 场景:finding 与根因假设作为修复输入
- **当** 以一组带稳定 id 的 finding、对应根因假设、target 名启动 `RemediationPlannerAgent`
- **那么** Agent 在 loop 中据此拟方案，产出绑定到这些 finding 的 `RemediationPlan`

### 需求:finding / 假设 / target 必须进 messages、禁止进 system，以保系统提示 byte-stable 命中 prompt cache

系统必须把每 run 变化的输入（finding、根因假设、target 名）放入首条 user message，禁止拼入 system 提示。系统提示（连同 `RemediationStep` schema 描述等固定内容）必须跨 run **字节稳定**，并在 `backend.capabilities.prompt_caching` 为真时由 loop 注入 `cache_control: ephemeral`；为假时禁止注入。

#### 场景:系统提示跨 run 字节稳定
- **当** 对两组不同的 finding 输入分别启动 planner
- **那么** 两次请求的 system 内容逐字节相同（差异只在 messages），使 prompt cache 可命中

#### 场景:prompt_caching 关闭时不注入 cache_control
- **当** backend 的 `capabilities.prompt_caching` 为 `False`
- **那么** loop 不在请求中注入任何 `cache_control` block（复用既有 `AgentLoop` 行为）

### 需求:`propose_remediation` 必须是纯结构化输出通道、用序号标签引用 finding、emit 即按 P1a schema 校验

系统必须提供 `propose_remediation` ToolSpec 作为 planner 的结构化输出通道：Agent 每为一个 finding 拟一份方案就调用一次，carry 方案体——`finding_label`（序号标签 `F1`/`F2`，引用首条 message 中列出的 finding，**非**真 `Finding.id`）、`rationale`、`estimated_duration_seconds`、`steps`（`list[RemediationStep]`，复用 P1a 模型）。该 ToolSpec 的 `side_effects` 必须为 `"none"`、`surfaces` 必须**仅含 `"agent"`**（禁止 `"mcp"` / `"cli"`）。

其 handler 必须 closure-bind per-run `FindingStore`，对 `finding_label` 做 **hit-check**（镜像 `correlate_findings_handler`）：标签悬空 → `ToolError`（结构化 envelope 回传模型自纠）；命中 → 回传不含真 id 的确认 ack。handler **不执行任何命令、不碰 target、不 resolve 真 id**（resolve 留 harvest）。

因 `steps` 复用 `RemediationStep`，emit 时 `ToolsAdapter.dispatch` 必须按 P1a **全部** step 不变量 `model_validate` 校验——`high_requires_precheck`、`rollback_none_requires_high`、命令字段非空白——任一违反即拒（adapter `→ TypeError`），结构化回传模型自纠。

#### 场景:每份方案一次调用并被 harvest
- **当** planner 为某 finding 调用一次 `propose_remediation`（`finding_label` 命中、方案体合法）
- **那么** 该调用成功、被编排层 harvest，产出一个绑定到该 finding 的 `RemediationPlan`

#### 场景:emit 违反 high_requires_precheck 被拒并回传自纠
- **当** `propose_remediation` 的 `steps` 含一个 `risk_level="high"` 但 `precheck_cmd=None` 的 step
- **那么** emit 被拒（消息含 `high_requires_precheck`），结构化回传模型，loop 不崩、模型可重拟

#### 场景:emit 违反 rollback_none_requires_high 被拒并回传自纠
- **当** `propose_remediation` 的 `steps` 含一个 `risk_level` 为 `"low"`/`"medium"` 但 `rollback_cmd=None` 的 step
- **那么** emit 被拒（消息含 `rollback_none_requires_high`），结构化回传模型自纠（故系统提示必须教模型「非 high step 必须给 rollback_cmd」）

#### 场景:悬空 finding_label 在 handler 被拒并回传自纠
- **当** `propose_remediation` 的 `finding_label` 引用一个不在 per-run `FindingStore` 中的标签（如 `F9`）
- **那么** handler 抛 `ToolError`、结构化回传模型自纠（**悬空在 emit 边界即被挡，永不抵达 harvest**——镜像 Diagnostician handler hit-check）

#### 场景:propose_remediation 不上 MCP
- **当** 用 MCP surface adapter 投影工具集
- **那么** `propose_remediation` 不出现在 MCP 工具列表中（`surfaces` 仅 `"agent"`）

#### 场景:handler 不执行任何命令
- **当** `propose_remediation` handler 被 dispatch
- **那么** 它只 hit-check 标签并回传 ack，**不调用 `target.exec`、不执行 `forward_cmd`/任何命令**（side_effects="none"，agent 表面只读不变量）

### 需求:P1b 禁止引入任何写工具，agent 表面保持永久只读

系统在本变更中禁止注册任何 `side_effects ∈ {"write","destructive"}` 或 `requires_approval=True` 的 ToolSpec；`agent/tools_adapter.py` 的 M2 dispatch gate 禁止放开。planner 的工具集必须经**专用装配函数** `register_remediation_planner_tools(registry, *, finding_store, target_name, ...)`（镜像 `register_diagnostician_tools` 成员、把 `correlate_findings` 换成 `propose_remediation`、closure-bind per-run `FindingStore` + `target_name`）装配，含且仅含只读三件：`request_more_inspection` + `list_inspectors`（复用）+ `propose_remediation`（`side_effects="none"`）；必须**排除 `correlate_findings`**（planner 不产假设）**与 `run_inspector`**（其 `RunInspectorInput` 要模型提供 `target_name`、违反 D-3「模型不触 target 名」；复核统一走 `target` 闭包绑定的 `request_more_inspection`，模型只给 `inspector_name` + `parameters`）。`list_inspectors` 供模型发现可用 inspector 名（module-level、无闭包）。禁止把 `propose_remediation` 放入 `register_default_tools`（那是无 closure 的共享只读集）。`FindingStore` 须 seed 自诊断阶段已盖稳定 id 的 canonical findings，与首条 message 的标签同源（标签 ↔ handler hit-check ↔ harvest resolve 三者读同一 store）。

#### 场景:planner 工具集全只读且排除 correlate_findings 与 run_inspector
- **当** 检视 `register_remediation_planner_tools` 装配出的工具集
- **那么** 工具集恰为 `request_more_inspection` / `list_inspectors` / `propose_remediation`，每个 `side_effects` ∈ `{"none","read"}`，无 write/destructive、无 `requires_approval=True`，且**不含 `correlate_findings`、不含 `run_inspector`**

#### 场景:planner 复用只读复核工具
- **当** planner 在拟方案前需复核 finding 现状
- **那么** 它用 `list_inspectors` 发现可用 inspector、用 `request_more_inspection`（`target` 闭包绑定，模型只给 `inspector_name`）复核现状，据返回的现状据实拟方案；`request_more_inspection` 中途 append 的新 finding 所得标签亦可被 `propose_remediation` 引用

### 需求:编排层必须 harvest 方案——resolve finding 标签 → 真 id、盖 target_name、构造 RemediationPlan，对悬空 fail-loud

系统必须提供编排层 helper `harvest_plans(loop, finding_store, target_name)`：遍历**成功**的 `propose_remediation` 调用，把 `finding_label` resolve 回真 `Finding.id`、盖上 `target_name`、用 P1a `RemediationPlan` 构造方案对象。因 handler 已 hit-check 保证标签命中，harvest 对悬空标签必须 **fail-loud（防御性 raise，理论不可达）**，**不得**「记录无效跳过」（那是 handler 不 hit-check 时的相反架构，本变更不取，与所镜像的 `harvest_hypotheses` fail-loud 一致）。harvest 时构造 `RemediationPlan` 确定成功（steps 已 emit 校验、finding_id/target_name 编排层盖非空）。harvest 必须零 wire 改动、零 `ToolContext` 改动。`target_name` 取自该 planner run 的单一 target（P1b 与 Diagnostician 同作用域＝单 target，`Finding` 不带 target 字段，无跨 target 情形）。

#### 场景:标签 resolve 回真 id 并盖 target
- **当** harvest 一个引用 `F1` 的成功 `propose_remediation` 调用、`F1` 命中 finding-store 中某 finding
- **那么** 产出的 `RemediationPlan.finding_id` 是该 finding 的真 id、`target_name` 是编排层盖上的该 run 的 target 名

#### 场景:多标签 resolve 到同一真 id 不去重
- **当** 两次成功 `propose_remediation` 分别引用 `F1`/`F2`，而二者经 `compute_finding_id`（排除 severity）resolve 到**同一**真 `Finding.id`
- **那么** harvest 不去重，产出两份 `finding_id` 相同的 `RemediationPlan`（继承 Diagnostician 同 id 不互覆盖语义）

#### 场景:harvest 对悬空标签 fail-loud（防御性）
- **当** harvest 理论上遇到一个悬空标签（正常路径不可达，因 handler 已 hit-check）
- **那么** harvest raise（防御性 fail-loud），**不**静默记录无效——契约上悬空必须在 handler 阶段就被挡住

#### 场景:harvest 不改既有复用工具 wire
- **当** planner 复用 `request_more_inspection` / `list_inspectors` 复核现状
- **那么** harvest / 标签 resolve 逻辑不改动这些既有工具的输入输出 wire、不改 `ToolContext`

### 需求:`RemediationPlannerResult` 必须聚合方案列表与编排后 status

系统必须提供 `RemediationPlannerResult`，聚合 harvest 出的 `list[RemediationPlan]`（可空）与 status。**status 是 planner loop 的 `terminal_status` 直传**（类型同 `LoopResult.terminal_status` 的 `_TerminalStatus`）——P1b 是单 loop、无第二 loop 需对账，**禁止引入 Diagnostician 的 `reconcile_status` 双 loop 机器**。当 loop 以降级终态结束（backend 超时 / 重试耗尽）时，status 直传该降级终态、已 harvest 的有效方案必须保留。当模型未产出任何 `propose_remediation` 调用时，方案列表为空、status 正常反映 loop 终态，不视为崩溃。

#### 场景:降级终态保留已 harvest 方案
- **当** planner loop 因 backend 重试耗尽以 `degraded_*` 终态结束、但此前已成功 harvest 一份方案
- **那么** `RemediationPlannerResult.status` 反映降级、已 harvest 的方案仍在结果中

#### 场景:无方案产出不视为崩溃
- **当** planner 以 `end_turn` 结束且全程未调用 `propose_remediation`
- **那么** `RemediationPlannerResult` 返回空方案列表 + 正常 status，编排层据此决定，不抛异常

#### 场景:诊断 ok 但 findings 空则不启动 planner
- **当** `run_remediation_planning` 的上游诊断成功（status==ok）但 findings 为空
- **那么** 不启动 planner loop（无 finding 无从拟方案），返回空方案列表，不消耗 LLM 调用
