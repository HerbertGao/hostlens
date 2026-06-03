# add-hypothesis-level-diff

## 为什么

`hostlens reports diff` 现在**只比 finding 级**：`compute_diff` 按 severity-agnostic `Finding.id` 做集合差，产 `added` / `resolved` / `changed_severity`（`reporting/diff.py`）。但 M3.1（`add-report-persistence-and-diff` / `add-intent-report-persistence`）已让 `Report.hypotheses`（根因假设）**随 Report 全量入库**，`wire-demo-to-report` 又让离线 demo 也产带根因假设的 Report —— 然而这些 hypotheses **入库但不参与对比**。

项目的差异化卖点正是「带根因假设的报告」。一个只比 finding、对根因诊断变化视而不见的回归工作流是**半成品**：用户在两次巡检间真正想知道的是「**根因诊断变了吗**」——某个根因假设这次消失/新出现了？置信度从 high 掉到 low 了？只看 finding 级 added/resolved 答不了这个问题。

本提案给 `reports diff` 增加 **hypothesis 级对比**，补上 M3 回归闭环最后一块。这是 `wire-demo-to-report` 与 `add-report-persistence-and-diff` 在各自 Non-Goals 里**显式拆出的独立后续提案**（用户已确认拆分）。

## 变更内容

- **设计 hypothesis 匹配键（本提案核心，见 design D-1）**：`RootCauseHypothesis` **无 `id` 字段**（仅 `description` / `confidence` / `supporting_findings` / `suggested_actions`，`models.py:286`）。`description` 是 LLM 生成的自由文本、跨 run **不稳定**（真实 `--intent` run 不可能逐字复现），**不能**作匹配键。匹配键必须锚定 **`supporting_findings`**（一组 content-derived 稳定 `Finding.id`）：键 = `frozenset(supporting_findings)`（引用证据集合，顺序无关）。这把「同一根因假设」定义为「引用同一组证据 finding 的假设」。
- **扩展 `RegressionDiff` 模型**：新增 `hypothesis_added` / `hypothesis_resolved`（按匹配键集合差）、`hypothesis_confidence_changed`（键匹配但 `confidence` 变化，镜像 `changed_severity`）、`hypothesis_unanchored`（空-support 假设计数）与 `hypothesis_ambiguous_keys`（因一报告内同键多条、confidence 归属歧义而**跳过 confidence 比较**的证据键数——守住「不静默」，见 design D-3）。沿用闭 schema（`extra="forbid"`、frozen）。
- **扩展 `compute_diff`**：在现有 finding 级 diff 之后，对两 Report 的 `hypotheses` 按匹配键做集合差，**复用同一套防基线污染门**（per-target 隔离、baseline 状态门、schema 对齐、`diff_skipped_reason` 闭集）——hypothesis diff 与 finding diff 在同一 `RegressionDiff` 里同进同出，跳过时两者一起为空。
- **扩展 `reports diff` CLI 渲染**（`_render_diff`，`cli/reports.py:317`）：在 finding 段后追加 hypothesis 段（`hypothesis_added` / `hypothesis_resolved` / `hypothesis_confidence_changed`）、`hypothesis_unanchored` / `hypothesis_ambiguous_keys` 的明示提示行，以及一行固定的「匹配口径」说明（如实表述基于 `supporting_findings` 证据集而非 description 语义）；`inspector_upgraded` 非空时追加一行 caveat（部分 hypothesis 变化可能由证据 finding 的 inspector 升级导致，见下「inspector 升级×hypothesis」）。保持现有简洁文本风格。
- **退化键的显式处理**：`supporting_findings` 为空的 hypothesis **不可锚定**（无证据键）——明确归入文档化的「unanchored」处理（见 design D-2 与 Failure Modes），不静默吞。

## 功能 (Capabilities)

### 修改功能

- **`report-regression-diff`**：`RegressionDiff` 模型需求（现：建模 finding 级 added/resolved/changed_severity → 改：**新增** hypothesis 级 added/resolved/confidence_changed 字段）、`compute_diff` 需求（现：仅 finding 集合差 → 改：在同一防污染门下**追加** hypothesis 按匹配键集合差）、`reports diff` CLI 需求（现：渲染 finding 段 → 改：追加 hypothesis 段）。

（**无新建 capability**：复用现有 `report-regression-diff`；`RootCauseHypothesis` 由 `report-data-model` 提供、本提案**不改其 schema**；hypotheses 已由 `report-persistence` 持久化、**不改 store**。）

## Non-Goals

- **语义 / NLP description 匹配**：不对 `description` 做文本相似度 / embedding / LLM 判同。匹配纯靠 `supporting_findings` 证据键。description 仅作渲染展示，不入键。
- **模糊 / Jaccard 近似匹配**：v1 是**精确证据集合匹配**（`frozenset` 相等）。「同一根因但引用 finding 集合漂移一个」会被记成 added+resolved 一对（见 Failure Modes）——这是 v1 诚实取舍，证据锚定优先于聪明但不可靠的近似。带阈值的相似度匹配留作未来（需要时再独立提案）。
- **不改 finding 匹配键**：`Finding.id`（severity-agnostic content hash）保持不变。
- **不新增 CLI 命令 / flag**：复用 `hostlens reports diff <a> <b>` 与自动基线模式；hypothesis 段总是随 diff 输出，不加开关。
- **不改 `RootCauseHypothesis` schema**：不给它加 `id` 字段（会牵动 `report-data-model` + diagnostician 投影 + 持久化迁移，超范围）；匹配键是 diff 引擎**派生**的，不落进模型。
- **不改 `reporting/store.py` schema**：hypotheses 已随 Report JSON 入库，diff 直接读。
- **不做 json diff 输出**：现 `reports diff` 只产文本（无 json diff payload），本提案沿用文本渲染，不引入结构化 diff 导出。
- **不改 demo / `--intent` 对外行为**：本提案只动 diff 引擎与 diff 渲染。

## 影响

**受影响代码**：
- `src/hostlens/reporting/diff.py`：`RegressionDiff` 加 hypothesis 字段 + **新建**两个投影模型 `HypothesisFingerprint` / `ConfidenceChange`（不复用 `FindingFingerprint` / `SeverityChange` —— 字段不同：hypothesis 无 `severity`、有 `confidence` + `supporting_findings`）；`compute_diff` 追加 hypothesis 集合差段。
- `src/hostlens/cli/reports.py`：`_render_diff` 追加 hypothesis 段渲染。
- `openspec/specs/report-regression-diff/spec.md`：三条需求的 delta。
- `tests/`：hypothesis 匹配键单测（精确集合匹配、confidence 变化、空 supporting_findings 退化、键碰撞）、`compute_diff` 集成、离线确定性（两次 `demo run --persist` → hypothesis delta 空）、防污染门继承（baseline 非 ok / 跨 target / schema 不一致时 hypothesis diff 也一起跳过/拒绝）。

**对外契约影响**：
- `RegressionDiff` schema **加字段**（向后兼容的纯加法；现有 finding 字段不动）。
- `reports diff` 文本输出**追加** hypothesis 段（演示/人类可读输出，无下游程序契约消费者）。
- 退出码语义不变（diff 命令的 0/3 边界沿用）。
- 不改 Inspector / Agent tool / MCP / Notifier / Schedule / store 任何 schema。

## Failure Modes

1. **空 `supporting_findings` 的 hypothesis（unanchorable）**：无证据键无法跨 run 匹配。处理见 design D-2——**不**塞进 added/resolved（否则每次都假报），而是计入 `hypothesis_unanchored`（**两 run 合计的总条数**：同一条空-support 假设若两侧都有会计 2 次，故语义是「两次巡检中无证据锚点的假设出现次数总和」而非「去重后的假设数」），CLI 显式提示「N 条假设（两 run 合计）无证据锚点、未参与对比」。绝不静默丢。
2. **同根因、引用 finding 集合漂移**：某支持 finding 这次被 resolved → hypothesis 的键变了 → 同一根因被记成 `hypothesis_resolved`(旧键)+`hypothesis_added`(新键) 一对。v1 接受（证据锚定的诚实代价，Non-Goal 模糊匹配）。文档/spec 如实标注此局限。
3. **键碰撞（一份报告内两 hypothesis 引用同一 finding 集合）**：同键 → 集合化时塌缩/歧义。处理见 design D-3：added/resolved 按键集合算、**每键最多发一条确定性 representative**（同键其余假设 collapse、不逐条发射也不单独计数——碰撞安全保住「键在/不在」核心正确性，逐条体现留作未来 YAGNI）；confidence 比较**仅对两侧均「该键恰好 1 条」的键发射**，对歧义键确定性跳过、并把跳过的键数计入 `hypothesis_ambiguous_keys` + CLI 明示一行（**不静默** —— 守住与 D-2 unanchored 同构的可观测性红线）。
4. **inspector 升级 × hypothesis 键（与 finding 段语义可能不一致）**：finding diff 在 rule 5 排除升级 inspector 的 findings；但 hypothesis 键用假设原样声明的 `supporting_findings`、**不重写**（design D-5：重写会引入跨版本隐式语义，v1 取「键 = 声明证据集」最简单可解释）。后果：某 hypothesis 引用了升级 inspector 的 finding 时，其键可能随 finding id 漂移 → 产生由 inspector 升级（而非真实诊断变化）驱动的 `hypothesis_added`/`hypothesis_resolved` 对，而 finding 段却因 rule 5 排除显示「无变化」。**处理（可见性，非改键行为）**：spec 增显式场景标注此 v1 行为；CLI 在 `inspector_upgraded` 非空时追加一行 caveat 提示「部分 hypothesis 变化可能由证据 finding 的 inspector 升级导致」，使该取舍对用户可见而非静默放过。
5. **hypotheses 为空（finding-only Report）**：两侧都无 hypothesis → 三个 hypothesis 列表均空，与现有「finding delta 空」同语义，合法不报错。
6. **基线污染门触发**：baseline 非 ok / 跨 target / schema 不一致 / 含 None finding id → 现有 `diff_skipped_reason` / `ValueError` 路径，hypothesis diff **与 finding diff 一起**被跳过/拒绝（不单独绕过门）。

## Operational Limits

- **并发 / 超时**：纯内存集合运算（个位数 hypotheses），无 IO、无 LLM、无网络，可忽略。
- **内存**：两 Report 的 hypotheses + 派生键集合，个位数量级。
- **资产**：无新增资产；复用已入库 Report 的 hypotheses。

## Security & Secrets

- **不引入新密钥 / 不触网**：diff 是纯本地集合运算。
- **脱敏**：hypothesis 渲染走与现有 diff 文本同样的本地输出；`description` / `suggested_actions` 已是诊断投影文本、与 Report 渲染同脱敏域，不新增暴露面。
- **不扩大攻击面**。

## Cost / Quota Impact

- **零 API 成本 / 零配额**：diff 不调 LLM、不回放 cassette，纯读已入库 Report 做集合差。

## Demo Path

```bash
# 离线复现 hypothesis 级 diff（无 SSH / 无 API key，沿用 wire-demo-to-report 的 --persist 闭环）
hostlens demo run cpu_saturation --persist
hostlens demo run cpu_saturation --persist
hostlens reports diff <a> <b>
#   → finding 段：空 delta（同场景确定性回放）
#   → hypothesis 段：hypothesis_added(0) / hypothesis_resolved(0) / hypothesis_confidence_changed(0)
#      （证明 hypothesis diff 管线可离线消费 demo 产出；同场景确定性 → 空 delta）
```

验收：两次同场景 `--persist` run → `reports diff` 的 hypothesis 段确定性输出空 delta（确定性回放下 hypotheses 逐字相同 → 键集合相同 → 无 added/resolved/confidence_changed），证 hypothesis diff 管线可离线确定性消费；added/resolved 的非空路径由对 `compute_diff` 的单元测试（构造不同 hypotheses 的两 Report）覆盖（同 finding 级 added 由单测覆盖的先例）。
