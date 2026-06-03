## 上下文

`add-diagnostician-agent`（M3.1）落地后，`--intent` 路径产 `DiagnosticianResult`（stdout-only），`--persist` 被拒、`Report.hypotheses` 恒 `[]`。根因：`run_inspector_handler`（`tools/default_tools.py`）内部用 `InspectorRunner.run` 得到完整 `InspectorResult`（含 status/duration/version），但投影成 `RunInspectorOutput` 时丢弃这些（故意，保 cassette 稳定）；`Report.from_inspector_results`（`reporting/models.py`）是唯一能组装忠实 `ReportMeta` 的工厂，需要 `InspectorResult` 列表，而 intent 路径拿不到。

已读源码确认的约束：
- `Report.from_inspector_results(target_name, inspector_results, *, ...)` 自带 `compute_finding_id` 盖 id + 填 `ReportMeta`（status 由 `_derive_report_status` 推、duration、inspectors_used、token_usage）；返回的 Report 带 `meta`，`ReportStore.save` 要求 `report.meta` 非 None。
- `register_default_tools(registry, *, clock=None)` 与 `register_diagnostician_tools(registry, *, finding_store, target_name, clock=None)` 已是「per-run 依赖经 handler 闭包注入」的先例。
- `request_more_inspection` handler 已直接持有 `InspectorResult`（用 `InspectorResult.version`）。
- `DiagnosticianResult` 当前是 `--intent` 的 CLI 表面（render/exit-code）；`stamp_planner_findings` 用 `InspectorRegistry.get(name).version` 反查（wire 丢 version 的退路）。
- `report-persistence` spec 明确标注「`--intent`/demo 缺 inspector_results 无法 from_inspector_results」。

## 目标 / 非目标

**目标：** `--intent` 路径产忠实一等 `Report`（真 meta + 填 `hypotheses`）→ 解锁 `--persist`、进 reports/diff，补齐 M3 退出条件的持久化半边。零 wire / 零 ToolContext 改动。

**非目标：** demo run 接 Diagnostician（仍 Planner-only，拆走）；regression diff 覆盖 hypotheses（finding-diff 不变，hypothesis-diff 独立未来提案）；改 `--inspector` 路径；render_html。

## 决策

### D-1：InspectorResult 经 per-run collector 闭包注入传播（不碰 wire / ToolContext）

新增 `InspectorResultCollector`（per-run 可变容器，append + snapshot）。`register_default_tools` / `register_diagnostician_tools` 增可选 `collector=None` kwarg；注入时 `run_inspector_handler` 与 `request_more_inspection` handler 在产出 `InspectorResult` 后 `collector.append(result)`。编排层持有同一 collector，loop 后 `snapshot()` 拿全部 `InspectorResult`。

- **替代（扩 ToolContext 加 sink）**：否决 —— 破坏 ADR-008 锁的 6 字段、改 tool-registry spec；ADR-008 本意虽是禁 LLMBackend，但「6 字段锁」是显式契约，append-sink 不值得破。
- **替代（加宽 RunInspectorOutput wire 带 status/version）**：否决 —— 改 LLM-facing tool_result → request-key hash 变 → 炸所有 cassette（diagnostician 提案极力避免的）。
- collector 默认 None（向后兼容：`--inspector` 路径、不注入时行为同今天）。collector 不上任何 surface（非 Agent capability、非 wire、非 ToolContext）。

### D-2：collector 两阶段；FindingStore 在诊断 loop 前 seed，权威 Report 在诊断 loop 后组装（两个不同时点，不矛盾）

时序有**两个不同动作、两个不同时点**，先前 D-2/D-3 把它们混为「先组装 Report」是错的：

1. **诊断 loop 前**（FindingStore seed）：Planner loop 跑完后，collector 已有 **Planner-phase** 的完整 `InspectorResult`。编排层用这些（真 `InspectorResult.version`）经 `compute_finding_id(name, version, message)` 给 Planner findings 盖 id 并 seed `FindingStore`（诊断师要先有带标签 findings 才能引用）。**这一步不组装 Report**，只盖 id seed store。
2. **诊断 loop 后**（权威 Report 组装）：诊断师补查的 `InspectorResult` 也进同一 collector。`run_diagnosis` 返回后，编排层 `collector.snapshot()` 拿 Planner + 补查全量 → `Report.from_inspector_results(...)` 组装**权威 Report**。**禁止**在 Planner loop 后提前 snapshot（否则漏补查 result）。

### D-3：id 一致性由 `compute_finding_id` 内容确定性保证（非组装顺序），不变量兜底

`compute_finding_id(inspector_name, version, message)` 是**纯内容哈希**：同 `(name, version, message)` → 同 id，与盖章发生在 seed 阶段还是 Report 组装阶段无关。两个时点（D-2 的 seed 与 Report 组装）用的 version **同源**——都是 collector 里**真 `InspectorResult.version`**（同一 `inspector_registry` 同一 run 落定）。故 Planner findings 在 FindingStore 里的 id 与最终 Report.findings 里的 id **天然相等**；诊断师 hypotheses 的 `supporting_findings`（引用 FindingStore 标签 → resolve 出的 id）与 Report.findings 的 id 一致。

- **不再用 `stamp_planner_findings` 的 registry 反查 version**：collector 已供给真 version，反查退路（wire 丢 version 才存在）在 `--intent` 路径**整条移除**。`stamp_planner_findings` 的唯一调用点是 `--intent`（`_intent.py:313`），移除后该函数变死代码 → **本提案一并删除它 + 其测试**（见影响）。其原 fail-loud（inspector 跑后被卸载致 registry 反查 not_found）的保护对象随之消失——version 在 run 时已随 `InspectorResult` 落定，不再有事后反查、不再可能触发该场景。
- **补查 finding 的 id**：request_more_inspection 的 `InspectorResult` 进 collector + 其 finding 经同 `compute_finding_id` 盖入 FindingStore；诊断后 snapshot 的 from_inspector_results 给同一 finding 盖同 id（同输入）→ 一致。
- **不变量校验（防护网）**：Report 组装后断言每个 `hypotheses[*].supporting_findings` id ∈ `Report.findings` 的 id 集；不满足 → fail-loud（CLI `internal: ... → exit 2`），不持久化悬空引用的报告。

### D-4：Report 成为 `--intent` 的 CLI / 持久化主对象；DiagnosticianResult 退为内部

`--intent` 的退出码、持久化、json 输出都转向 `Report`（json 见 D-6）。`DiagnosticianResult` 保留为诊断 loop 的内部返回聚合，其 `hypotheses` 经编排层投影进 `Report.hypotheses`、`narrative` 进 `Report.metadata["diagnosis_narrative"]`，不再对外暴露为 CLI 契约。

- **替代（Report 与 DiagnosticianResult 并存为双 CLI 契约）**：否决 —— 两个公开对象描述同一次 inspect，reports/list/show/diff 不知看哪个；契约面翻倍。
- **后果（BREAKING）**：`--intent --format json` 输出从 `DiagnosticianResult` 变 `Report`（字段名/层级不同）。spec 写明旧→新映射（见 D-6）。

### D-5：no-result（collector 真空）vs 降级（含全非 ok）两路；status 合并由 override 决定

- **collector 真空**（零 `InspectorResult` —— Planner 一次 run_inspector 都没成功调成，如 `failed_api_unavailable` 在任何工具前、或模型从不调 run_inspector）：**不产 Report**，走既有 no-result 路径（stderr 降级行 + stdout 空 + exit 2 + **不 persist**）。`_persist_report` 必须显式处理 None（不静默 skip 成假成功）。
- **collector 非空（含全部非 ok）**：组装 Report。**非 ok 的 inspector run（timeout/target_unreachable/requires_unmet/exception）`InspectorResult` 仍带真 status/version/duration，照样进 collector + Report**（no-result ≠「无成功 inspector」，而是「零 InspectorResult」）。
- **status 合并机制（移出 Open Question，钉死）**：`from_inspector_results(status=...)` 在 `status` 非 None 时逐字采用、为 None 时由 `_derive_report_status` 推（`models.py`）；`ReportMeta.status: ReportStatus` 接受 `degraded_*`/`empty_response`。编排层据 `reconcile_status(...)`：reconcile 降级（`degraded_*`/`empty_response`）→ 传 `status=<该值>`（覆盖）；reconcile `ok` → 传 **`status=None`**（让 `_derive_report_status` 按 §9 推 ok/partial），**禁止**显式传 `ok`。**勿过度断言「有非 ok inspector 就 partial」**：`_derive_report_status` 对「非 ok 仅 timeout 且至少一 ok」仍推 `ok`（§9），仅 `target_unreachable`/`exception`/`requires_unmet` 或全 timeout 才 `partial`。

### D-6：渲染 + `--format json` breaking 映射

- **json**：输出**完整 `Report`**（经 `redact_report_for_render` 脱敏）。旧→新映射：`DiagnosticianResult.findings`→`Report.findings`；`.hypotheses`→`Report.hypotheses`；`.status`→`Report.meta.status`；`.narrative`→`Report.metadata["diagnosis_narrative"]`（json 不丢、持久化可回取）；`.planner_result`/`.diagnostician_loop` 不再现于 json 顶层（loop 遥测汇总进 `Report.meta.token_usage`）。
- **md（关键修正）**：`--intent` md **不复用** `reporting/render_markdown`（它为 `--inspector` 机械报告设计：固定 `# Hostlens Inspection Report` 标题 + meta 表 + `## Inspector Results` 原始 inspector JSON dump；它**确实**渲染 `## 根因假设`，但**不读 `metadata`、不渲染诊断 narrative**——复用会让 `--intent` md 版面剧变 + 倾泻 inspector 原始 JSON 且丢 narrative）。`--intent` md 改用一个 **intent 风格 Report 渲染器**（在编排/CLI 层，对脱敏后的 `Report` 渲染）：诊断 narrative（从 `metadata["diagnosis_narrative"]`，容忍空）+ `## Findings` 摘要（`Report.findings`）+ `## 根因假设`（`Report.hypotheses`，空→`_暂无根因假设_`）+ 一行遥测。**不改** `reporting/render_markdown`（`--inspector` 路径零影响、不引版面 BREAKING）。
- token_usage：`ReportMeta.token_usage` = Planner + Diagnostician 两个 `LoopUsage` 的**字段级求和**投影成 `TokenUsage`（含各自 cache_read 等），勿只取诊断 loop。

### D-7：diff 不扩展到 hypotheses（写进非目标，防预期落差）

`compute_diff` 仍 finding-id based、不改。hypotheses 随 Report 入库（show 可见）但 `reports diff` 不展示 hypotheses 变化。spec 非目标显式写明，避免「存了 hypotheses 却 diff 不出来」的 reviewer/用户落差。hypothesis regression diff 留独立提案。

## 风险 / 权衡

- **[snapshot 时序漏补查 result]** → D-2 钉死「诊断 loop 后 snapshot」+ 测试断言补查 finding 进 Report.inspector_results。
- **[id 一致性]** → D-3 由 `compute_finding_id` 内容确定性 + 同 version 源保证（非组装顺序）+ 不变量校验 fail-loud 兜底。
- **[--format json breaking 无声破坏脚本消费方]** → D-6 spec 写明映射 + 标 BREAKING。
- **[no-result vs 全非 ok 混淆]** → D-5 钉死：no-result = collector 真空（零 InspectorResult）；全非 ok → 产 partial Report 并持久化。
- **[md 版面被 render_markdown 复用悄悄改版 + 倾泻 inspector JSON]** → D-6 `--intent` md 用 intent 风格 Report 渲染器、**不**走 render_markdown；render_markdown 零改动、`--inspector` 无回归。
- **[started_at/finished_at 来源]** → `LoopResult` 无时间戳；编排层在 Planner 启动前 / 诊断 loop 结束后各取一次 `datetime.now(UTC)` 作为 `from_inspector_results` 的 started_at/finished_at（参照 `inspect_cmd` 既有计时）。
- **[from_inspector_results 不去重，补查同名 inspector 产重复 finding（同 id 两条）]** → 沿用既有 flatten 语义；`compute_diff` 是 finding-id based，同 id 多条在单报告内属既有未定义行为 —— 本提案是首个可能触发它的消费方，记为**已知限制**（hypothesis/重复-id diff 行为留独立提案），不在本提案验收。
- **[collector 持未脱敏 InspectorResult 在内存]** → 仅进程内；持久化/渲染前过 `redact_report_for_render` 既有脱敏边界（json）/ intent 渲染器同样过脱敏；无新泄露面。
- **[`degraded_no_planner` 持久化后命名误导]** → 该 enum 沿用 add-diagnostician-agent，语义为「诊断阶段降级」非「无 Planner」；本提案首次将其落库进 `Report.meta.status`，`reports show` 读者需知此约定（已知限制，命名修正留后续）。

## Migration Plan

- 新增 `InspectorResultCollector`；`register_default_tools`/`register_diagnostician_tools` 加可选 `collector=`（向后兼容，默认 None）。
- 改 `_intent.py`（collector 装配 + Planner 阶段 seed FindingStore 用 collector 真 version + 诊断后 snapshot + Report 组装 + hypotheses/narrative 投影 + 不变量 + intent 风格 Report 渲染）、`inspect.py`（Report 退出码含 partial + 持久化、移除 `--persist` 拒绝、**更新 `--persist` help 文案与 `inspect_cmd` docstring 退出码注释**）。
- **删除 `stamp_planner_findings` + 其测试**（唯一调用点 `--intent` 移除后死代码；id 改由 collector 真 version 经 `compute_finding_id` 盖）。
- `DiagnosticianResult` 保留类型但降为内部；CLI 退出码/json/持久化基于 `Report`。**不改** `reporting/render_markdown`（demo + `--inspector` 零影响）。
- 三份已归档 spec 更新（inspect-cli-command / diagnostician-agent / report-persistence；report-persistence 的 MODIFIED 标题**保 live verbatim「机械巡检报告落盘」**、body 拓宽，避免归档匹配失败）+ 新建 agent-report-assembly spec。
- 回滚：collector 默认 None → 不注入即回到今天行为。
- feature branch `feat/add-intent-report-persistence`，PR squash（§5.1）。

## Open Questions

无未决项（narrative 落点见 D-6 = `metadata["diagnosis_narrative"]`；status 合并见 D-5 = reconcile 产 `degraded_*`/`empty_response` 则传该值覆盖、reconcile=ok 则传 `status=None` 交 `_derive_report_status` 按 §9 推 ok/partial，均已钉死）。
