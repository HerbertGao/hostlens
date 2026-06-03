## 新增需求

### 需求:per-run `InspectorResultCollector` 必须经 handler 闭包注入收集完整 `InspectorResult`

必须新增 per-run `InspectorResultCollector`（可变容器，`append(result)` + `snapshot() -> list[InspectorResult]`，**非 module-global**）。`register_default_tools` 与 `register_diagnostician_tools` 必须新增可选 `collector` 参数（默认 `None`，向后兼容 —— 不注入时行为同现状）；注入时，`run_inspector` 与 `request_more_inspection` 的 handler 必须 append **`InspectorRunner.run(...)` 返回的 `InspectorResult` 对象本身**（含真 `status` / `duration_seconds` / `version` / `findings`），**禁止** append 投影后的 `RunInspectorOutput`（后者已剥 status/version，会重新引入本提案要修的数据丢失）。append 必须在 **ok 与非 ok 两个分支都执行**（非 ok 的 `InspectorResult` 仍带真 status/version，须进 collector）。collector 必须经 handler **闭包**注入（镜像 `register_default_tools(clock=...)` / `FindingStore` 先例），**禁止**经 `ToolContext`（ADR-008 锁的 6 字段不变）、**禁止**改 `RunInspectorOutput` wire（LLM-facing 投影不变、cassette 全命中）。

#### 场景:collector 注入时 handler append 完整 InspectorResult

- **当** `register_default_tools(registry, collector=c)` 注入 collector 后，Planner 经 `run_inspector` 跑了 2 个 inspector
- **那么** `c.snapshot()` 必须含这 2 个完整 `InspectorResult`（各带 status/duration/version/findings），而 `run_inspector` 的 wire 输出（`RunInspectorOutput`）保持不变

#### 场景:不注入 collector 时行为不变

- **当** `register_default_tools(registry)` 不传 collector（默认 None）
- **那么** handler 不收集、行为与现状一致（向后兼容；`--inspector` 机械路径与既有 cassette 不受影响）

#### 场景:collector 不上任何 surface

- **当** 检视 collector
- **那么** 它必须**不是** Agent capability / 不在 wire / 不在 `ToolContext`，仅编排层与 handler 闭包持有

### 需求:collector 跨两阶段；FindingStore 在诊断前 seed，权威 Report 在诊断后 snapshot（两个时点）

`--intent` 编排必须用**同一个** collector 实例跨 Planner loop 与 Diagnostician loop 积累，按**两个不同时点**操作（不是「先组装 Report」）：

1. **诊断 loop 前** —— FindingStore seed：Planner loop 跑完后，collector 已有 Planner-phase 的完整 `InspectorResult`。编排层用这些（真 `InspectorResult.version`）经 `compute_finding_id(name, version, message)` 给 Planner findings 盖 id 并 seed `FindingStore`（诊断师须先有带标签 findings 才能引用）。**这一步不组装 Report**。
2. **诊断 loop 后** —— 权威 Report 组装：`request_more_inspection` 的 `InspectorResult` 也进同一 collector。`run_diagnosis` 返回后，编排层 `collector.snapshot()` 拿 Planner + 补查全量 → `from_inspector_results` 组装权威 `Report`。**禁止**在 Planner loop 后提前 snapshot 组装权威 Report（否则漏补查 result，`Report.inspector_results` 不完整、补查 finding 进不了 Report）。

`started_at` / `finished_at`：`LoopResult` 无时间戳，编排层必须在 Planner 启动前 / 诊断 loop 结束后各取一次 `datetime.now(UTC)` 传给 `from_inspector_results`。

#### 场景:补查 inspector 的 result 进入 Report

- **当** 诊断师经 `request_more_inspection` 补查了一个 inspector 并产 finding，编排层在诊断 loop 后 `snapshot()`
- **那么** `Report.inspector_results` 必须同时含 Planner 阶段与补查阶段的 `InspectorResult`，补查 finding 必须出现在 `Report.findings`

### 需求:`--intent` 路径必须经 `from_inspector_results` 组装忠实一等 `Report`

`--intent` 编排必须用 `Report.from_inspector_results(target_name, collector.snapshot(), intent=..., started_at=..., finished_at=..., status=<reconcile 覆盖值，见降级语义需求>, token_usage=...)` 组装一等 `Report`（注意签名：`target_name` 在前、`inspector_results` 在后，`started_at`/`finished_at` 必填 kwarg）：`compute_finding_id` 盖 finding id、`ReportMeta` 填 `status` / `duration_seconds` / `inspectors_used` / `token_usage`。`token_usage` 必须从 Planner + Diagnostician 两个 loop 的 `LoopUsage` **字段级求和**投影成 `TokenUsage`（含各自 `cache_read_input_tokens` 等，勿只取诊断 loop）。组装出的 `Report` 必带 `meta`（`ReportStore.save` 要求）。

#### 场景:intent 报告带忠实 meta

- **当** `--intent` 成功跑通并组装 Report
- **那么** `Report.meta` 必须非 None，含真实 `inspectors_used`（各 inspector 的 status/version/duration/finding_count）与从两 loop 汇总的 `token_usage`

### 需求:诊断师 hypotheses 必须投影进持久化 `Report.hypotheses`，且 id 统一、引用一致

`--intent` 编排必须把诊断师产出的 hypotheses 投影进 `Report.hypotheses`（当前恒 `[]`），并把诊断 narrative 投影进 `Report.metadata["diagnosis_narrative"]`（json 不丢、持久化可回取）。

**id 一致性由 `compute_finding_id` 内容确定性保证，非组装顺序**：FindingStore seed（诊断前，D-2 步骤 1）与 Report 组装（诊断后，D-2 步骤 2）用的 version **同源**——都是 collector 里真 `InspectorResult.version`。同 `(name, version, message)` → 同 id，故 FindingStore 里盖的 id 与最终 `Report.findings` 里的 id **天然相等**；诊断师 `supporting_findings`（引用 FindingStore 标签 → resolve 出的真 id）与 `Report.findings` id 一致。**`--intent` 路径禁止再走 `stamp_planner_findings` 的 registry-反查-version 退路**（该退路因 wire 丢 version 才存在；collector 已供给真 version）——该函数唯一调用点是 `--intent`，移除后变死代码，本提案一并删除（见影响）。组装后必须做不变量校验：每个 `hypotheses[*].supporting_findings` id 必须 ∈ `Report.findings` id 集；不满足必须 **fail-loud**（CLI `internal: ... → exit 2`），禁止持久化含悬空引用的报告。

#### 场景:supporting_findings 引用 Report 中存在的 id

- **当** 诊断师产出引用某 finding 的 hypothesis，编排组装 Report
- **那么** `Report.hypotheses[*].supporting_findings` 的每个 id 必须能在 `Report.findings` 中找到（id 同源于 `from_inspector_results`）

#### 场景:悬空引用 fail-loud

- **当** 组装后某 `supporting_findings` id 不在 `Report.findings`（id 来源不一致的残余）
- **那么** 必须 fail-loud（`internal: ... → exit 2`），禁止持久化该报告

### 需求:降级 / 无结果两种 `Report` 语义必须明确

`--intent` 编排必须区分两种退化，**判据是「collector 是否真空（零 `InspectorResult`）」，不是「有无成功 inspector」**：

- **collector 非空（含全部非 ok）**：组装 `Report`。非 ok 的 inspector run（timeout/target_unreachable/requires_unmet/exception）的 `InspectorResult` 带真 status/version，**照样进 collector + Report**。持久化（若 `--persist`）；退出码按 `Report.meta.status` 映射。
- **collector 真空（零 `InspectorResult`）**：Planner 一次 `run_inspector` 都没成功调成（如 `failed_api_unavailable` 在任何工具前、或模型从不调 run_inspector）。**禁止产 `Report`**，走既有 no-result 路径（stderr 一行降级原因 + stdout 空 + exit 2 + **不 persist**）。持久化入口必须显式处理「无 Report」（不静默 skip 成假成功）。

**`meta.status` 合并机制（钉死，非 Open Question）**：`ReportMeta.status: ReportStatus` 接受 `degraded_*`/`empty_response`；`from_inspector_results(status=...)` 在 `status` 非 None 时**逐字采用**、为 None 时由 `_derive_report_status(snapshot)` 推导。编排层据 agent loop 的 `reconcile_status(...)` 决定传什么：
- reconcile 产 `degraded_*`/`empty_response` → 传 `status=<该降级值>`（覆盖 inspector 推导）。
- reconcile 产 `ok` → 传 **`status=None`**（让 `_derive_report_status` 按其自身 §9 规则推 `ok`/`partial`）。**禁止**显式传 `ok`（会绕过 partial 推导、掩盖 inspector 级数据丢失）。
- 注意 `_derive_report_status` 的实际语义（不可过度断言「有非 ok 就 partial」）：全 ok → `ok`；非 ok **仅** `timeout` 且至少一个 `ok` → 仍 `ok`（§9「单 inspector timeout 不降级」）；出现 `target_unreachable`/`exception`/`requires_unmet` 或**全部** `timeout` → `partial`。

#### 场景:降级但有 inspector 结果仍组装并可持久化

- **当** Planner 在若干 inspector 成功后 `degraded_token_budget`
- **那么** collector 非空 → 组装 Report、`meta.status=degraded_token_budget`（reconcile 覆盖）、可经 `--persist` 入库、exit 2

#### 场景:全非 ok inspector → partial Report 持久化

- **当** Planner 跑的 inspector 全部非 ok（如 target_unreachable）、loop terminal_status=ok、collector 非空
- **那么** 必须组装 Report、`meta.status=partial`（reconcile=ok → 传 `status=None` → `_derive_report_status` 据 target_unreachable 推 `partial`；非 blanket 覆盖）、可持久化

#### 场景:collector 真空不产 Report 不持久化

- **当** Planner `failed_api_unavailable`、collector 为空（零 InspectorResult）
- **那么** 禁止产 Report、禁止 persist，stderr 一行降级 + stdout 空 + exit 2
