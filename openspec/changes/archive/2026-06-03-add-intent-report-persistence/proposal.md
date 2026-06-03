## 为什么

`add-diagnostician-agent`（M3.1）让 `hostlens inspect --intent` 产出根因假设，但**只活在 stdout**：intent 路径产 `DiagnosticianResult` 而非一等 `Report`，因此 `--intent --persist` 被显式拒绝、`--intent` 报告进不了 `hostlens reports list/show/diff`，`Report.hypotheses` 永远是 `[]`。M3 退出条件「**持久化报告**中能看到根因假设章节 + 对同一 target 跑两次能 diff」因此只完成了一半（`--inspector` 路径满足，`--intent` 路径不满足）。

根因是一个数据丢失：`run_inspector_handler` 内部构造了完整 `InspectorResult`（含 `status` / `duration_seconds` / `version`），但投影成 `RunInspectorOutput` 上 wire 时把这些丢了（故意，为保 cassette 稳定）；`Report.from_inspector_results` 是唯一能组装忠实 `ReportMeta` 的工厂，却拿不到 `InspectorResult`。`diagnostician-agent` 提案的 Scope-Core 明确把「忠实 ReportMeta + InspectorResult propagation + `--intent --persist` 解锁」拆给本 follow-up。

## 变更内容

- **新增 per-run `InspectorResultCollector` + 闭包注入**（镜像 `register_default_tools(clock=...)` / `FindingStore` 先例）：`run_inspector_handler` 与 `request_more_inspection` handler 在 collector 注入时，把**完整 `InspectorResult`** append 进去（wire 投影不变）。编排层在**诊断 loop 结束后** `collector.snapshot()` 拿到 Planner + 补查两阶段的全部 `InspectorResult`。**零 wire 改动、零 `ToolContext` 改动**（不碰 ADR-008 锁的 6 字段）。
- **intent 路径组装忠实一等 `Report`**：诊断 loop 后用 `Report.from_inspector_results(target_name, collected_results, intent=..., started_at=..., finished_at=..., status=<reconcile 覆盖值>, token_usage=...)` 组装（注意签名：target_name 在前、started_at/finished_at 必填）—— 自带 `compute_finding_id` 盖 id + 真 `ReportMeta`（status/duration/inspectors_used + `token_usage` 从两 loop `LoopUsage` 字段级求和投影）。
- **`Report.hypotheses` 填充**：诊断师 hypotheses 落进**持久化的** `Report.hypotheses`（当前恒 `[]`；诊断师可合法不产假设，空也是有效报告）。
- **id 一致性靠 `compute_finding_id` 内容确定性**：FindingStore 在诊断 loop **前** seed（用 collector Planner-phase 的真 `InspectorResult.version` 盖 id），权威 Report 在诊断 loop **后**组装；同 version 源 + 同函数 + 同输入 → id 天然相等（**非靠组装顺序**）。`--intent` **不再走 `stamp_planner_findings` 的 registry 反查 version 退路**（该退路因 wire 丢 version 而存在）—— 该函数唯一调用点是 `--intent`，移除后死代码，本提案**删除它 + 其测试**。加 post-assembly 不变量校验：每个 `hypotheses[*].supporting_findings` id ∈ `Report.findings` id。
- **`Report` 成为 `--intent` 的 CLI / 持久化主对象**：退出码、持久化、json 都转向 `Report`；md 用 **intent 风格 Report 渲染器**（不复用 `reporting/render_markdown` —— 它会让 md 版面剧变 + 倾泻 inspector 原始 JSON；narrative 进 `Report.metadata["diagnosis_narrative"]`）；`DiagnosticianResult` 退为编排层内部聚合（不再是 CLI 表面契约）。
- **解锁 `--intent --persist` + 进 reports/diff**：移除 `--persist` 对 `--intent` 的拒绝；`--intent` 报告自然进 `ReportStore` + `reports list/show/diff`。

## 功能 (Capabilities)

### 新增功能
- `agent-report-assembly`: agent（`--intent`）路径把 loop 的 `InspectorResult` 收集（per-run collector 闭包注入、跨 Planner+诊断两阶段、诊断 loop 后 snapshot）并经 `from_inspector_results` 组装忠实 `Report`、合并诊断师 hypotheses 进 `Report.hypotheses`、统一 id 盖章 + supporting_findings↔findings id 一致性不变量、以及降级/无结果两种 Report 语义。

### 修改功能
- `inspect-cli-command`: `--intent` 路径从「产 `DiagnosticianResult` 直出 stdout、拒绝 `--persist`」改为「组装并渲染/持久化一等 `Report`」。**BREAKING**：`--intent --format json` 输出从 `DiagnosticianResult` 改为 `Report`（字段名/嵌套层级变化，spec 写明旧→新映射）；退出码改由 `Report` 映射；`--persist` 对 `--intent` 解锁。
- `diagnostician-agent`: `run_inspector` / `request_more_inspection` handler 在 collector 注入时 append 完整 `InspectorResult`；`--intent` 编排路径的 hypotheses `supporting_findings` 改为引用 Report 组装出的 finding id（不再 `stamp_planner_findings` 反查 version）；`DiagnosticianResult` 由 CLI 表面契约降为内部聚合。
- `report-persistence`: 移除「`--intent`/demo 路径缺 `inspector_results` 无法 `from_inspector_results` 故无法 persist」的边界注 —— `--intent` 路径现可经 collector 供给 `InspectorResult` 组装可持久化 Report。

## 影响

- **修改代码**：`src/hostlens/cli/_intent.py`（collector 装配 + 诊断前 seed FindingStore + 诊断后 snapshot/Report 组装 + hypotheses/narrative 投影 + 不变量校验 + **新增 intent 风格 Report md 渲染器**）、`src/hostlens/cli/inspect.py`（`--intent` 改 Report 退出码(含 partial)/持久化、移除 `--persist` 拒绝、**更新 `--persist` help 文案 + `inspect_cmd` docstring 退出码注释**以反映 intent 现产 Report）、`src/hostlens/tools/default_tools.py`（`register_default_tools(collector=...)` + `run_inspector_handler` append 真 InspectorResult）、`src/hostlens/tools/diagnostician_tools.py`（`request_more_inspection` append + `register_diagnostician_tools(collector=...)`；**删除死代码 `stamp_planner_findings`**）、`src/hostlens/agent/diagnostician.py`（编排 helper：supporting_findings 引用 Report id）。
- **删除代码**：`stamp_planner_findings` + 其测试（唯一调用点 `--intent` 移除后死代码）。
- **不改**：`reporting/render_markdown`（`--inspector`/demo 零回归）、`reporting/diff.py`（finding-diff 不变）、`RunInspectorOutput` wire、`ToolContext`。
- **新增代码**：`InspectorResultCollector`（放 `src/hostlens/tools/` 或 `reporting/`，实现阶段定）；`cli/_intent.py` 内的 **Planner-finding seed-helper**（遍历 collector Planner-phase snapshot 经 `compute_finding_id` 盖 id seed FindingStore，替代被删的 `stamp_planner_findings`，**禁止 registry 反查**）；**intent 风格 Report md 渲染器**。
- **零 wire / 零 ToolContext 改动**：`RunInspectorOutput` 不变（所有 cassette 命中），`ToolContext` 仍 6 字段（ADR-008）。
- **复用既有**：`Report.from_inspector_results` / `ReportMeta` / `TokenUsage` / `Report.hypotheses`（report-data-model）、`ReportStore`（report-persistence）、`compute_diff`（report-regression-diff，不改）。

## 非目标 (Non-Goals)

- **不**把 `demo run` 接入 Diagnostician / 忠实 Report（仍 Planner-only）：要重录 8 份 demo cassette + 改 `demo-cli-command` 契约，与本提案的「真实 intent 路径持久化」正交，拆为独立 follow-up。
- **不**扩展 regression diff 覆盖 hypotheses：`compute_diff` 仍是 finding-id based、行为不变；hypotheses 只随 Report 入库（持久化可见），**hypothesis regression diff 是未来独立提案**。下游消费方勿期待 `reports diff` 展示 hypotheses 变化。
- **不**改 `--inspector` 机械路径（它本就产忠实 Report、能 persist）、不改 `reporting/render_markdown`（`--intent` md 用独立 intent 风格渲染器，不复用它）。`stamp_planner_findings` 无 `--intent` 之外的调用点，本提案直接删除（非「保留不改」）。
- **不**做 render_html（M3.4 单列）；不引入新 LLM backend / extended-thinking。

## 对外契约影响

| 契约面 | 影响 |
|---|---|
| Agent tool schema (`RunInspectorOutput` wire) | **不变**（collector 是 out-of-band，wire 投影不动；cassette 全命中）|
| `ToolContext` | **不变**（6 字段，ADR-008；collector 经 handler 闭包注入，非 ToolContext）|
| `Report` / `ReportMeta` / `TokenUsage` 模型 | **不变**（复用既有；本提案首个让 `--intent` 路径产忠实 meta + 填 `hypotheses` 的消费方）|
| CLI `hostlens inspect --intent` | **BREAKING**：`--format json` 输出 `Report`（非 `DiagnosticianResult`）；退出码改由 `Report` 映射；`--persist` 解锁。`--inspector` 路径不变 |
| CLI `hostlens reports list/show/diff` | 行为不变；`--intent` 报告自然纳入（新增数据来源，非契约改）|
| `register_default_tools` / `register_diagnostician_tools` 装配签名 | 新增可选 `collector=` kwarg（向后兼容，默认 None = 不收集，行为同今天）|

## Agent 行为变更：Prompt Caching 策略与 Token 影响

- 本提案**不改 Planner / Diagnostician 的 prompt 或 loop 调度**：collector 是纯被动收集（handler 在已有执行后多 append 一个内存对象），不影响系统提示 byte-stability、不增 LLM 调用、不动 `cache_control` 注入。prompt cache 行为与 `add-diagnostician-agent` 一致。
- 唯一 token 相关变化：`Report.meta.token_usage` 现从 Planner + 诊断两个 loop 的 `LoopUsage` 汇总投影（之前 intent 路径不产 meta），纯记录、不影响调用。

## Failure Modes

1. **Planner 降级但已收集到部分 `InspectorResult`**（如 token_budget/max_turns 在若干 inspector 成功后触顶）：collector 非空 → 组装 status=`degraded_*` 的 Report（meta.status 反映降级），仍持久化。退出码 2。
2. **Planner `failed_api_unavailable`（collector 完全空，无任何 inspector 成功）**：**不产 Report**（无可信 meta），走既有 no-result 路径（stderr 降级行 + stdout 空 + exit 2 + 不 persist）。`_persist_report` 必须显式处理 None，不静默 skip 成「假成功」。
3. **hypotheses.supporting_findings 引用了 Report 里不存在的 finding id**（id 来源不一致的残余）：post-assembly 不变量校验捕获 → fail-loud（`internal: ... → exit 2`），不持久化悬空引用的报告。
4. **补查（request_more_inspection）的 InspectorResult 与 Planner 的同名同 message**：`from_inspector_results` 不去重，两条都进 Report.inspector_results + findings（id 由 compute_finding_id 可能相同——与 diagnostician 提案 FindingStore label 唯一键的处理一致，Report 层按既有 flatten 语义保留）。
5. **持久化写失败**（SQLite 锁/磁盘）：沿用 `ReportStore` 既有 orphan/错误路径；CLI 报错退出，渲染输出仍照常到 stdout。

## Operational Limits

- **并发/内存**：collector 是 per-run 内存列表，持有 N 个 `InspectorResult`（N = 本次 intent 跑过的 inspector 数，通常个位数）；不增并发、不放大峰值。
- **超时**：无新超时面（collector append 是内存操作）。Report 组装/持久化是 loop 后的同步 CPU + 一次 SQLite 写。
- **存储**：`--intent` 报告现入 `ReportStore`，沿用 report-persistence 的保留策略 / 脱敏 JSON 存储 / 大报告告警阈值。

## Security & Secrets

- **不引入新密钥**。collector 持有的 `InspectorResult` 含未脱敏 findings/evidence，但**仅在进程内内存**；持久化前经 `ReportStore` 既有的 `redact_report_for_render` 边界脱敏（与 `--inspector` 路径一致），渲染同样过脱敏边界（diagnostician 提案已建立）。**无新泄露面** —— intent 报告现走与 inspector 报告**相同**的持久化脱敏标准。
- collector 不上任何 surface（非 Agent 可调用能力、非 wire、非 ToolContext）。

## Cost / Quota Impact

- **零额外 LLM 调用 / 零额外 token 消耗**：collector 纯被动收集既有执行结果。Report 组装 + 持久化是本地 CPU + SQLite，无 API。
- CI 全程沿用既有 cassette（wire 不变，无需重录），零真实配额。

## Demo Path

5 分钟、无 SSH / 无付费 API（测试级 cassette replay）reproduce：

```bash
# 沿用 tests/cli/test_inspect_intent.py pattern：monkeypatch create_backend 注入
# authored FakeBackend，--intent 跑通后断言报告入库 + diff
pytest tests/cli/test_inspect_intent_persist.py -q
```

该测试断言：`hostlens inspect <target> --intent "..." --persist` 产出忠实 `Report`（meta.status/inspectors_used/token_usage + 非空 `hypotheses`）、入 `ReportStore`；`hostlens reports show <run_id>` 能取回含根因假设的报告；对同一 target 跑两次 `hostlens reports diff` 输出 finding 级 added/resolved。Live：真 key 下 `hostlens inspect <target> --intent "为什么响应变慢" --persist` 后 `hostlens reports list <target>` 可见。
