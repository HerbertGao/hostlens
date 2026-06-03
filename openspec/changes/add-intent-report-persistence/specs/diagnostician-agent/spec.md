## 修改需求

### 需求:编排层必须给 Planner findings 盖稳定 id，零 wire / 零 ToolContext 改动

`--intent` 编排层必须给 findings 盖稳定 `id`，且 id 必须统一到一个来源以保 `hypotheses[*].supporting_findings` 与 `Report.findings` 一致（见 `agent-report-assembly` 能力）：

- **新机制（本提案）**：id 来自 per-run `InspectorResultCollector` 供给的**真 `InspectorResult.version`**，经 `compute_finding_id(name, version, message)` 盖（**不再**用 `InspectorRegistry.get(name)` 反查 version）。两个时点用同源 version、同函数、同输入，故 FindingStore seed 的 id 与最终 `Report.findings` 的 id 天然相等（**id 一致由内容确定性保证，非组装顺序**；详见 `agent-report-assembly` D-2/D-3）。`FindingStore` 在诊断 loop **前** seed（Planner-phase findings 经 `compute_finding_id` 盖 id），权威 `Report` 在诊断 loop **后**由 `from_inspector_results` 组装；二者 id 相等。`supporting_findings` 引用的真 id ∈ `Report.findings`。
- **`stamp_planner_findings` 删除**：其唯一调用点是 `--intent` 路径，改用 collector 真 version 后变死代码，本提案**删除该函数 + 其测试**（见提案影响）。其原 fail-loud（inspector 跑后被卸载致 registry 反查 `inspector_not_found`）的保护对象随之消失——version 在 run 时随 `InspectorResult` 落定，不再有事后反查、该场景不再可能触发。
- **不变项**：仍**零 wire 改动**（`RunInspectorOutput` 不变、cassette 全命中——collector 是 out-of-band）、**零 `ToolContext` 改动**（6 字段，ADR-008；collector 经 handler 闭包注入）。组装后必须做 id 一致性不变量校验（每个 `supporting_findings` id ∈ `Report.findings`），不满足 fail-loud（`internal: ... → exit 2`）。

#### 场景:id 同源于 from_inspector_results

- **当** Planner 跑了两个 inspector，编排层用 collector 的 `InspectorResult` 经 `from_inspector_results` 组装 Report
- **那么** finding id 必须由 `compute_finding_id`（用真 `InspectorResult.version`）盖出，`hypotheses[*].supporting_findings` 引用的 id 必须 ∈ `Report.findings`，无 registry 反查

#### 场景:盖章不改 run_inspector wire

- **当** 启用 collector 后回放既有 incident/demo/planner cassette
- **那么** `run_inspector` 的 tool_result 必须字节不变、cassette 全部命中（collector 是 out-of-band 内存收集，不上 wire）

### 需求:`DiagnosticianResult` 必须聚合 findings(带 id) / hypotheses / reconcile 后的 status

诊断 loop 必须产出 frozen 的 `DiagnosticianResult` 作为**编排层内部聚合**（不再是 `--intent` 的 CLI / 持久化表面契约——CLI 表面是 `Report`，见 `inspect-cli-command` 与 `agent-report-assembly`）。字段必须含：`narrative`（诊断 loop 的 `final_text`，降级路径下可能为空字符串）、`findings: list[Finding]`（带稳定 id 的 canonical 集合，id 同源于 `from_inspector_results`）、`hypotheses: list[RootCauseHypothesis]`（harvest 自 `correlate_findings`，`supporting_findings` 为 Report 的真 finding id）、`status: ReportStatus`（按 reconcile 规则得出）、`planner_result: PlannerResult`、`diagnostician_loop: LoopResult | None`（`None` 当且仅当诊断阶段被跳过）。编排层必须把 `DiagnosticianResult.hypotheses` 投影进持久化 `Report.hypotheses`，把 `narrative` 投影进 Report 渲染 / `metadata`（见 `agent-report-assembly`）。**不再禁止**组装 `reporting.models.Report` —— 本提案正是让 `--intent` 路径经 `from_inspector_results` 产出忠实 Report（取代 `add-diagnostician-agent` 的 Scope-Core「不产 Report」约束）。

#### 场景:无根因假设时 hypotheses 为空

- **当** 诊断师未调用任何 `correlate_findings` 且以 `end_turn` 带文本结束（`terminal_status=ok`）
- **那么** `DiagnosticianResult.hypotheses` 必须为空列表，投影出的 `Report.hypotheses` 也为空，其余字段正常

#### 场景:DiagnosticianResult 不再是 CLI 表面契约

- **当** `--intent --format json` 输出
- **那么** stdout 必须是 `Report` 的序列化（非 `DiagnosticianResult`）；`DiagnosticianResult` 仅作编排层内部聚合存在，不对外暴露为 json 顶层结构（见 `inspect-cli-command` 的 BREAKING 映射）
