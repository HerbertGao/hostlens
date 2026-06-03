## 修改需求

### 需求:`hostlens inspect --intent` 必须装配并运行 PlannerAgent，实时进度走 stderr、报告走 stdout

`--intent` 路径必须用 `create_backend(settings)`（**只调用一次**）+ 注册了默认工具**且注入 per-run `InspectorResultCollector`** 的 `ToolRegistry`（`register_default_tools(registry, collector=c)`）+ 产出含 target/inspector registry 的 `ToolContext` 的 context_factory 装配 `PlannerAgent`，以 CLI 端 observer 调 `PlannerAgent.run(intent, observer=...)`。Planner 返回后必须装配并运行 `DiagnosticianAgent`（复用**同一** backend + 同一 collector + 受限诊断师注册表 + 固定 target；backend 仍只注入 loop、禁止进 `ToolContext`，ADR-008）。**在诊断 loop 结束后**，编排层必须 `collector.snapshot()` 拿 Planner + 补查两阶段的全部 `InspectorResult`，经 `Report.from_inspector_results` 组装忠实一等 `Report`（id 同源、`meta` 含真实 inspectors_used/token_usage），并把诊断师 hypotheses 投影进 `Report.hypotheses`（见 `agent-report-assembly`）。该 `Report` 是 `--intent` 的最终产物（渲染 / 退出码 / 持久化都基于它）。

实时进度（Agent 逐轮工具调用 + 每轮 assistant 文本，**非** token 级流式）必须渲染到 **stderr**；Planner 与 Diagnostician 两段进度都走 stderr。最终报告输出到 **stdout**（或 `--output`）。二者分离，stdout 不被进度污染。`--intent` 字符串只能作为模型 user message，禁止进入任何 shell/命令渲染路径。CLI 边界必须把任何未预期异常（含从 loop 透传的非可重试 backend 错误如 `CassetteMiss`；含 id 一致性不变量校验失败的 fail-loud）包成一行 `internal: <kind>: <msg>` → exit 2，不泄露 traceback。

#### 场景:实时进度与报告分流

- **当** `--intent` 运行且 Planner 与 Diagnostician 各调用了若干工具
- **那么** stderr 必须出现两段逐轮/逐工具的实时进度，stdout 必须只含最终报告内容

#### 场景:backend 未配置报配置错误

- **当** `--intent` 运行但 backend 未配置（如缺 `ANTHROPIC_API_KEY`，`create_backend` 抛 `ConfigError`）
- **那么** 必须 exit 3 并在 stderr 给出一行配置错误提示（指向 `hostlens doctor`），不泄露 traceback

#### 场景:补查阶段后才组装 Report

- **当** 诊断师经 `request_more_inspection` 补查了 inspector
- **那么** `collector.snapshot()` 与 `Report` 组装必须在诊断 loop 之后发生，使 `Report.inspector_results` 含补查阶段的 `InspectorResult`

### 需求:`hostlens inspect --intent` 必须输出 narrative + findings 摘要 + 根因假设 + 遥测，支持 md/json

`--intent` 路径必须按 `--format` 渲染组装出的（已脱敏）`Report`：

- **json**：输出 `Report` 的 JSON 序列化（经 `redact_report_for_render` 脱敏，复用 `reporting/render_json`）。
- **md（不复用 `reporting/render_markdown`）**：`--intent` md 必须用一个 **intent 风格渲染器**（编排/CLI 层，对脱敏后的 `Report` 渲染）：诊断 narrative（来自 `Report.metadata["diagnosis_narrative"]`，markdown；降级路径下可能为空，渲染必须容忍空——不报错、不渲染空标题）+ `## Findings` 摘要（来自 `Report.findings`）+ `## 根因假设` 章节（来自 `Report.hypotheses`，每条含 description / confidence / 关联 finding 证据 / suggested_actions；空时 `_暂无根因假设_` 占位）+ 一行遥测（status / token usage —— 权威 `Report` 不携带 loop turns，两 loop 的 turn 计数已汇总进 `meta.token_usage`，故遥测行只出 status + token 总量）。**禁止**让 `--intent` md 走 `reporting/render_markdown` 的全量结构（它为 `--inspector` 机械报告设计：固定 `# Hostlens Inspection Report` 标题 + meta 表 + `## Inspector Results` 原始 inspector JSON dump；它确实渲染 `## 根因假设`，但不读 `metadata`/不渲染 narrative —— 复用会让 `--intent` md 版面剧变、倾泻 inspector 原始 JSON 且丢 narrative）。`reporting/render_markdown` **零改动**（`--inspector` 路径与 demo 无回归）。md 字符串同样必须经脱敏（对脱敏后的 Report 渲染，或渲染后过 `redact_text`）。

**BREAKING（旧 → 新映射）**：`--intent --format json` 之前输出 `DiagnosticianResult`，现输出 `Report`：`DiagnosticianResult.findings` → `Report.findings`；`.hypotheses` → `Report.hypotheses`；`.status` → `Report.meta.status`；`.narrative` → `Report.metadata["diagnosis_narrative"]`（json 不丢，持久化可回取）；`.planner_result` / `.diagnostician_loop` 不再出现在 json 顶层（loop 遥测汇总进 `Report.meta.token_usage`）。findings 为空时 md 只输出 narrative + 根因假设占位 + 遥测，不报错。

#### 场景:md 模式输出综述、findings 摘要与根因假设

- **当** `--intent --format md` 且诊断师产出了若干根因假设
- **那么** stdout 必须含诊断 narrative、`## Findings` 摘要、`## 根因假设` 章节（每条含证据与建议动作），并附遥测行；所有字符串经 `redact_report_for_render` 脱敏

#### 场景:无根因假设时显示占位

- **当** `--intent --format md` 但诊断师未产出任何根因假设
- **那么** stdout 的 `## 根因假设` 章节必须显示 `_暂无根因假设_` 占位，其余正常

#### 场景:json 模式输出可解析的 Report

- **当** `--intent --format json`
- **那么** stdout 必须是 `Report` 的合法 JSON（可被 `Report.model_validate_json` 往返解析），含 `meta` / `findings` / `hypotheses` / `metadata["diagnosis_narrative"]`；**不再**是 `DiagnosticianResult`

### 需求:`hostlens inspect --intent` 退出码沿用 4 值语义并由 DiagnosticianResult 映射

`--intent` 路径必须按组装出的 `Report` 映射退出码（标题「DiagnosticianResult」二字沿用 live 的 verbatim 标识符以避免归档 MODIFIED 匹配失败；本提案把退出码来源从 `DiagnosticianResult` 改为 `Report`）（与 `--inspector` 路径同一 4 值语义，优先级 3>2>1>0）：`Report.meta.status` ∈ {`ok`} 且无 critical finding → `0`；`status=ok` 且 ≥1 `Report.findings` 的 `severity=="critical"` → `1`；`status` ∈ 降级集合（`degraded_*` / `empty_response` / `partial`）→ `2`；参数互斥违规 / backend 配置错误 / `--output` 写失败 / `--format` 非法 / id 一致性不变量失败 → `3`/`2`（usage 类 3、内部一致性 fail-loud 经 internal → 2）。**collector 完全空（Planner `failed_api_unavailable`）的 no-result 特例**：不产 `Report`，CLI 走 no-result 路径——stderr 一行降级原因、exit `2`、stdout 空（不伪造空报告骨架）、**不 persist**。Planner 或 Diagnostician 降级时 CLI 禁止重试（重试收口在 loop），有 `Report` 时仍输出已收集的 findings + （可能为空的）hypotheses。

**消费约定**：脚本判定成功**必须看退出码（0/1）**，禁止用「stdout 是否为空」——no-result 路径 stdout 空 + exit 2，而健康巡检 findings 空但有 narrative/占位（stdout 非空）+ exit 0。

**实现约束（不破坏 demo）**：`--intent` 现用 `Report`——json 走 `reporting/render_json`，md 走**新增的 intent 风格 Report 渲染器**（不走 `reporting/render_markdown`）；退出码用**新增的 Report 版函数**（注意须含 `partial` → 2，非 `_compute_diag_exit_code` 的机械复制——`DiagnosticianResult.status` 永不为 `partial`，但 `Report.meta.status` 可为 `partial`）。既有 `_compute_intent_exit_code(PlannerResult)` / `render_planner_result` / `reporting/render_markdown` 仍被 `cli/demo.py` 的 `demo run` / `--inspector` 路径使用，**禁止**改其签名/行为（demo 不经 Report、不经 intent 渲染器，零回归）。

#### 场景:健康巡检退出 0

- **当** `--intent` 结果 `Report.meta.status=ok` 且无 critical finding
- **那么** 必须 exit 0

#### 场景:critical finding 退出 1

- **当** `status=ok` 且 `Report.findings` 含至少一条 `severity=="critical"`
- **那么** 必须 exit 1

#### 场景:降级退出 2 且仍输出报告

- **当** `Report.meta.status` 为 `degraded_*` / `empty_response` 且存在 `Report`
- **那么** 必须 exit 2，stderr 标注降级原因，stdout 仍输出 Report（findings + 可能为空的 hypotheses），CLI 未重试

#### 场景:partial（inspector 非 ok 推导）退出 2

- **当** loop terminal_status=ok 但有 inspector 非 ok 致 `Report.meta.status=partial`（区别于 loop 降级路径）
- **那么** 必须 exit 2（Report 版退出码函数须把 `partial` 纳入降级集合——这是相对 `_compute_diag_exit_code` 的新增行为，非机械复制），stdout 仍输出 Report

#### 场景:Planner API 不可达无结果退出 2

- **当** Planner `terminal_status=failed_api_unavailable`、collector 为空、不产 `Report`
- **那么** 必须 exit 2，stderr 一行降级原因，stdout 为空（不伪造空报告骨架），不 persist
