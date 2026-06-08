# report-regression-diff 规范

## 目的

定义报告回归 diff 契约——`RegressionDiff` 建模 added / resolved / changed_severity 与版本对齐、`compute_diff` 以 severity-agnostic 指纹做集合差且防基线污染、`reports diff` CLI 支持显式两 run 与自动基线两种模式、diff 可离线确定性验证(机械 Report 路径)、hypothesis 级 added / resolved / confidence_changed(按 `supporting_findings` 证据键)。

## 需求
### 需求:`RegressionDiff` 模型必须建模 added/resolved/changed_severity 与版本对齐信息

`hostlens.reporting.diff.RegressionDiff` 必须是 Pydantic v2 模型（`extra="forbid"`），含字段（对齐 §10 Diff 输出结构，字段名与 §10 一致）：

- `baseline_meta: BaselineRef | None`（实际用作基线的 run 的 `BaselineRef`，从 `baseline.meta` 投影。**当且仅当 `baseline.meta is not None` 时非 None**（**与 `current.meta` 是否为 None 无关**——含所有跳过子情形：`baseline_not_ok`/`schema_changed`/finding-id 缺失/`current.meta is None`，只要 `baseline.meta` 在就投影）；**仅 `baseline.meta is None` 时为 `None`**（无 baseline meta 可投影）。**沿用 §10 的 `baseline_meta` 名**，不叫 `baseline_ref`——避免与 `ReportMeta.baseline_ref`（报告自记的基线引用）撞名）
- `added: list[FindingFingerprint]`（current 有、baseline 无）
- `resolved: list[FindingFingerprint]`（baseline 有、current 无）
- `changed_severity: list[SeverityChange]`（两边同 finding id 但 severity 变化）
- `inspector_upgraded: list[str] = []`（baseline 与 current 间 version 不同的 inspector name，信息项；**全提案统一用此名**，不用 `changed_inspector_version`）
- `dst_boundary_crossed: bool = False`（M3 恒 False，DST 窗口对齐占位，属 M4）
- `diff_skipped_reason: Literal["baseline_not_ok", "schema_changed", "missing_finding_ids"] | None = None`（闭集，防 CLI 渲染漂移；**不含** `baseline_unavailable`——「无基线」由 CLI 直接输出文本、不构造 `RegressionDiff`，见 `hostlens reports diff` 需求）

其中 `FindingFingerprint` 至少含 `{id: str, inspector_name: str | None, severity: Severity, message: str}`；`SeverityChange` 至少含 `{id: str, from_severity: Severity, to_severity: Severity, message: str}`。

#### 场景:RegressionDiff 拒绝未声明字段

- **当** 试图构造 `RegressionDiff(..., not_a_field="x")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:diff_skipped_reason 是闭集

- **当** 试图构造 `RegressionDiff(..., diff_skipped_reason="whatever")`
- **那么** 必须 raise `pydantic.ValidationError`（仅接受三个 Literal 值或 None）

### 需求:`compute_diff` 必须以 severity-agnostic 指纹做集合差且防基线污染

`hostlens.reporting.diff.compute_diff(baseline: Report, current: Report, *, force: bool = False) -> RegressionDiff` 必须按以下规则（对齐 §10 基线选取）：

0. **meta 完整性前置**：若 `baseline.meta is None` 或 `current.meta is None`（legacy 1.0 / orphan / 外部导入报告）→ 返回 `diff_skipped_reason="missing_finding_ids"`、空 added/resolved/changed_severity。**禁止解引用为 None 的那一侧 meta**（否则 None deref 爆 `AttributeError`）；但 `baseline_meta` 仍按字段规则处理——`baseline.meta is not None` 时**照常**从 `baseline.meta` 投影 `BaselineRef`（即便 `current.meta is None`），`baseline.meta is None` 时为 None。这类报告 finding 也必 `id=None`，归同一跳过类别。
1. **per-target 隔离**：`baseline.meta.target_id != current.meta.target_id` → raise `ValueError`（**禁止**跨 target diff）。
2. **finding 身份完整性**：若 baseline 或 current 含 `Finding.id is None` 的 finding（legacy schema 1.0 报告 / 直接构造未走工厂）→ 返回 `diff_skipped_reason="missing_finding_ids"`、空 diff（**禁止**用 None id 做集合差，否则碰撞/类型不符）。
3. **基线状态门槛**：`baseline.meta.status != "ok"` 且非 `force` → 返回 `diff_skipped_reason="baseline_not_ok"`，`added/resolved/changed_severity` 均空（**不**抛错）。
4. **schema 对齐**：`baseline.meta.report_schema_version != current.meta.report_schema_version` → 返回 `diff_skipped_reason="schema_changed"`，空 diff。
5. **inspector 版本对齐**：对每个 inspector，若 baseline 与 current 的 `version` 不同 → 该 inspector 的全部 finding **排除**出 added/resolved/changed_severity，inspector name 计入 `inspector_upgraded`。
6. **指纹集合差**（仅对版本对齐、且 id 非 None 的 inspector finding）：以 `Finding.id`（severity-agnostic 指纹）建两集合——`added` = 仅 current；`resolved` = 仅 baseline；`changed_severity` = 两边同 `id` 但 severity 不同。
7. `baseline_meta` 填基线的 `BaselineRef`（**当且仅当 `baseline.meta is not None` 时非 None**，见 RegressionDiff 字段说明；其 `inspector_versions` 由 `baseline.meta.inspectors_used` 投影 `name->version`，与 `latest_ok_baseline` 同源）；`dst_boundary_crossed` 恒 False（M3）。

#### 场景:同一报告 diff 自身无回归

- **当** `compute_diff(report, report)`（baseline=current，status=ok，findings 全有 id）
- **那么** `added == []` 且 `resolved == []` 且 `changed_severity == []`

#### 场景:current 新增 finding 进 added

- **当** baseline 含 finding 集 `{A}`，current 含 `{A, B}`（同 inspector 同 version，均有 id）
- **那么** `added` 必须含 B 的指纹，`resolved == []`

#### 场景:baseline 有而 current 无的 finding 进 resolved

- **当** baseline 含 `{A, B}`，current 含 `{A}`
- **那么** `resolved` 必须含 B，`added == []`

#### 场景:severity 变化进 changed_severity 而非 added+resolved

- **当** 同一 finding（同 id）baseline severity=`warning`、current severity=`critical`
- **那么** `changed_severity` 必须含该 id 的 `{from_severity:"warning", to_severity:"critical"}`，且该 finding **不**出现在 added/resolved

#### 场景:含 None id 的 finding 跳过 diff

- **当** baseline 或 current 任一 finding `id is None`（直构未走工厂）
- **那么** 返回 `diff_skipped_reason == "missing_finding_ids"`，added/resolved/changed_severity 均空

#### 场景:meta 为 None 的 legacy 报告跳过 diff 不 None-deref

- **当** baseline 或 current 任一 `report.meta is None`（legacy 1.0 / orphan 导入）
- **那么** 返回 `diff_skipped_reason == "missing_finding_ids"`、空 diff，且**不**因解引用 None 侧 `meta.target_id` 抛 `AttributeError`（前置规则 0 在任何对 None 侧 `.meta.` 访问前拦截）

#### 场景:current.meta 缺失但 baseline.meta 在仍投影 baseline_meta

- **当** `current.meta is None` 但 `baseline.meta is not None`（非对称缺失）
- **那么** 跳过 diff（`diff_skipped_reason == "missing_finding_ids"`、空 diff），但 `baseline_meta is not None`（从 `baseline.meta` 照常投影，**不**因 current.meta 缺失而置 None）；反之当 `baseline.meta is None` 时 `baseline_meta is None`

#### 场景:基线非 ok 时跳过 diff

- **当** `baseline.meta.status == "partial"` 且 `force=False`
- **那么** 返回 `diff_skipped_reason == "baseline_not_ok"`，added/resolved/changed_severity 均空

#### 场景:force 覆盖非 ok 基线

- **当** `baseline.meta.status == "partial"` 且 `force=True`
- **那么** 必须正常计算 diff（不因状态门槛跳过）

#### 场景:inspector 版本升级时其 finding 排除出 added/resolved

- **当** inspector `linux.disk.usage` baseline version=`1.0`、current version=`1.1`，两版本各有不同 finding
- **那么** `inspector_upgraded` 必须含 `linux.disk.usage`，且该 inspector 的 finding **不**出现在 added/resolved（避免版本升级被误报为全 resolved + 全 added）

#### 场景:跨 target diff 被拒绝

- **当** `baseline.meta.target_id == "host-a"` 而 `current.meta.target_id == "host-b"`
- **那么** 必须 raise `ValueError`

#### 场景:schema 版本不一致跳过 diff

- **当** `baseline.meta.report_schema_version != current.meta.report_schema_version`
- **那么** 返回 `diff_skipped_reason == "schema_changed"`，空 diff

### 需求:`hostlens reports diff` CLI 必须支持显式两 run 与自动基线两种模式

`hostlens reports diff` **必须**支持两种基线模式（显式两 run / 自动基线），从 `ReportStore` 取报告跑 `compute_diff` 并渲染结果：

- `hostlens reports diff <run_id_a> <run_id_b> [--force]`：a 作 baseline、b 作 current，从 store 取两份报告跑 `compute_diff`，渲染 added/resolved/changed_severity/inspector_upgraded。
- `hostlens reports diff --target <target> [--baseline last_success] [--force]`：current = 该 target **总序最大**（最新）run（经 `get_run` 还原为 `Report`）；baseline = `latest_ok_baseline(target, before_run_id=<current.run_id>)`（**必须排除 current 自身**），其 `run_id` 再经 `get_run` **还原为 `Report`** 才喂 `compute_diff`（`latest_ok_baseline` 返回 `BaselineRef`、无 findings，不能直接做 diff）。
- 任一 run 不存在 → stderr 单行错误 + 退出码 3，无 traceback。
- 无合格基线（首次 run / 全非 ok / 自动模式排除 current 后无更早 ok run）→ 输出「无可比基线」并退出码 0（非错误）。
- 退出码语义对齐既有 CLI（0 成功 / 3 not-found；diff 有回归不改变退出码，回归通过输出表达而非退出码）。

#### 场景:显式两 run diff 输出回归

- **当** 存在两份不同的 ok 报告，运行 `hostlens reports diff <a> <b>`
- **那么** 必须输出 added/resolved/changed_severity 三类结果

#### 场景:自动模式不把唯一 run 当自身基线

- **当** 某 target 只有一条 ok run，运行 `hostlens reports diff --target <target>`
- **那么** 必须输出「无可比基线」（current 被排除后无更早 ok run），退出码 0

#### 场景:未知 run 退出码 3

- **当** `hostlens reports diff <存在的 run> <不存在的 run>`
- **那么** stderr 单行错误，退出码 3，无 traceback

#### 场景:无基线时退出码 0

- **当** `hostlens reports diff --target <无 ok 历史的 target>`
- **那么** 输出「无可比基线」，退出码 0，无 traceback

### 需求:diff 必须可离线确定性验证（机械 Report 路径，不依赖 Agent）

回归对比必须可在无 SSH / 无付费 API / 无 Agent 下确定性验证。验收**走产出 `Report` 的机械路径**（`from_inspector_results`），**不**依赖 demo/Agent 的 `PlannerResult`：

- **CLI 空 diff**：用 `hostlens inspect <target> --inspector <确定性 inspector> --persist` 跑两次得两份内容相同的 `Report`。
- **集成 added/changed**：测试**直调 `InspectorRunner.run` + `ReplayTarget`（不复用 `_harness` 的 Agent 路径）**产出 `InspectorResult`，再经 `from_inspector_results` 组装两份 `Report`（baseline 无 critical；current 由 `linux.memory.pressure` + `linux.kernel.oom_killer` 产出含 critical 的 finding），断言 `compute_diff`。

#### 场景:同输入两次机械巡检 diff 为空

- **当** `hostlens inspect local-host --inspector hello.echo --persist` 跑两次（确定性输出 → 两份 Report 的 findings 逐字相同 → 指纹一致）→ `hostlens reports diff <run1> <run2>`
- **那么** `added`、`resolved`、`changed_severity` 必须均空

#### 场景:不同严重度场景 diff 出 added critical（集成测试，机械组装）

- **当** 测试**直调 `InspectorRunner.run` + `ReplayTarget`**（不走 Agent/`_harness`）组装两份 `Report`：baseline 无 critical、current 由 `linux.memory.pressure` + `linux.kernel.oom_killer` 产出含 critical 的 finding，对两者跑 `compute_diff`
- **那么** `added` 必须含该 critical finding 的指纹（验收在 `tests/incidents/test_diff_replay.py`，全程离线、不调 Agent / 不触 API）

### 需求:`RegressionDiff` 必须建模 hypothesis 级 added/resolved/confidence_changed

`RegressionDiff` 模型必须在现有 finding 级字段（`added` / `resolved` / `changed_severity` / `inspector_upgraded` / `dst_boundary_crossed` / `diff_skipped_reason`）之外，**新增** hypothesis 级对比字段（纯加法，既有字段语义不变）：`hypothesis_added` 与 `hypothesis_resolved`（各为 hypothesis 投影列表）、`hypothesis_confidence_changed`（键匹配但置信度变化的列表）、`hypothesis_unanchored`（一个非负整数：因 `supporting_findings` 为空而无法锚定、未参与对比的假设**出现次数**，**两 run 合计**——同一条空-support 假设若 baseline 与 current 都有则计 2 次，语义是「未参与对比的假设出现次数总和」而非「去重假设数」）、`hypothesis_ambiguous_keys`（一个非负整数：因一份报告内多条假设共享同一证据键、置信度归属歧义而**跳过 confidence 比较**的证据键个数，按键去重计数）。模型必须保持闭 schema（`extra="forbid"`、frozen）；新列表字段以 `Field(default_factory=list)` 声明（与既有字段风格一致）。当 `diff_skipped_reason` 非空时，三个 hypothesis 列表必须为空、`hypothesis_unanchored` 与 `hypothesis_ambiguous_keys` 必须均为 0（hypothesis diff 与 finding diff 同进同出，被跳过时一并为空）。hypothesis 列表的顺序必须确定（发射前按稳定键 `sorted(supporting_findings)` 排序），使离线回放逐元素可复现。hypothesis 投影携带 `confidence`、`sorted` 且去重后的 `supporting_findings`（可读匹配键 / 审计用）、`description`（仅展示，**不**参与匹配）。

#### 场景:RegressionDiff 拒绝未声明的 hypothesis 字段
- **当** 以一个未在闭 schema 中声明的额外键构造 `RegressionDiff`
- **那么** 构造必须因 `extra="forbid"` 失败（hypothesis 字段不放松闭 schema）

#### 场景:跳过 diff 时 hypothesis 字段为零值
- **当** `compute_diff` 因任一 `diff_skipped_reason`（如 `baseline_not_ok` / `schema_changed` / `missing_finding_ids`）跳过
- **那么** 返回的 `RegressionDiff` 的 `hypothesis_added` / `hypothesis_resolved` / `hypothesis_confidence_changed` 必须均为空列表，`hypothesis_unanchored` 与 `hypothesis_ambiguous_keys` 必须均为 0

#### 场景:hypothesis 列表确定性排序
- **当** 同一对 Report 两次调用 `compute_diff`
- **那么** 两次返回的 `hypothesis_added` / `hypothesis_resolved` / `hypothesis_confidence_changed` 列表必须逐元素相等且顺序一致（确定性，可离线回放复现）

### 需求:`compute_diff` 必须按 `supporting_findings` 证据键追加 hypothesis 集合差并继承防污染门

`compute_diff` 必须在现有 finding 级集合差之后、且**仅当**未触发任何 `diff_skipped_reason` 早返或跨 target `ValueError` 时，对 `baseline.hypotheses` 与 `current.hypotheses` 计算 hypothesis 级集合差。匹配键必须是 `frozenset(hypothesis.supporting_findings)`（引用的 `Finding.id` 集合，顺序无关）；**禁止**用 `description` 参与匹配（LLM 自由文本、跨 run 不稳定）。`supporting_findings` 为空的假设必须在建键前从两侧排除、其条数计入 `hypothesis_unanchored`，且**禁止**进入 `added` / `resolved`。`hypothesis_added` 为出现在 current 键集但不在 baseline 键集的键；`hypothesis_resolved` 为反之；`hypothesis_confidence_changed` 为键同时存在于两侧、但置信度不同的键。**added/resolved 按「键集合」计算且每个键最多发射一条 `HypothesisFingerprint`**：即使一份报告内多条假设共享同一键（碰撞），该键在 added/resolved 中也只发射**一条确定性 representative**（per-report 按 `(sorted(supporting_findings), confidence, description)` 稳定排序后的第一条）；同键的其余假设被 collapse、**不**逐条发射、**不**单独计数（这是 v1 有意取舍：added/resolved 以键为单位、碰撞安全保住「键在/不在」的核心正确性，逐条列出同键假设留作未来 YAGNI）。**v1 明确不向用户披露 intra-key 多重性**：CLI 对一个 added/resolved 键只展示该 representative 的单条 `confidence` / `description`，**不**提示「该键尚有 N 条同键假设被折叠」。此非「静默丢失变化信号」——added/resolved 是**键级**语义，键的新增/消解（唯一的 change signal）已被完整发射；被省略的只是 intra-report 的同键假设条数（不是跨 run 的变化）。该非披露是 v1 有意决定（碰撞罕见 + 键级语义自洽），逐条披露 / verbose 模式留作未来独立提案。一份报告内若多条假设共享同一键（碰撞），confidence 比较仅对两侧均「该键恰好一条」的键发射、对歧义键（任一侧该键 >1 条）确定性跳过；**被跳过 confidence 比较的歧义键个数必须计入 `hypothesis_ambiguous_keys`**（守「不静默」——歧义须有可见信号，禁止无声吞掉真实置信度变化）。hypothesis diff 必须**继承**与 finding diff 完全相同的防污染门——任一门（meta 完整性 / per-target 隔离 / finding id 完整性 / baseline 状态门 / schema 对齐）触发时 hypothesis 不计算；本提案**不**扩展 `diff_skipped_reason` 闭集。inspector 版本对齐（rule 5）对 hypothesis 键**无**过滤作用：键用假设原样声明的 `supporting_findings`，不因 inspector 升级而重写。该 v1 行为的已知后果是——某 hypothesis 若引用了被 rule 5 排除的 finding，其键可能随该 finding 的 id 漂移而产生由 inspector 升级（非真实诊断变化）驱动的 added/resolved 对，而 finding 段因 rule 5 排除显示无变化；此取舍不静默放过，必须由 CLI 在 `inspector_upgraded` 非空时输出可见 caveat（见「`hostlens reports diff` 必须渲染 hypothesis 段」需求）。`compute_diff` 同样**不**校验 `supporting_findings` 中的 id 是否真存在于该 Report 的 finding 集合（diagnostician 已保证解析为真实 `Finding.id`；键只要跨 run 一致即可，不要求当前可解析）。

#### 场景:current 新增引用新证据集的假设进 hypothesis_added
- **当** baseline 与 current 通过全部防污染门，current 含一条 `supporting_findings` 证据集在 baseline 任何假设中都不存在的假设
- **那么** 该假设必须进入 `hypothesis_added`，不进入 `hypothesis_resolved`

#### 场景:baseline 有而 current 无的假设进 hypothesis_resolved
- **当** baseline 含一条证据集键在 current 中不存在的假设
- **那么** 该假设必须进入 `hypothesis_resolved`，不进入 `hypothesis_added`

#### 场景:键相同置信度变化进 hypothesis_confidence_changed
- **当** 同一证据集键的假设在 baseline 为 `low`、在 current 为 `high`
- **那么** 必须产生一条 `hypothesis_confidence_changed`（`from_confidence=low` → `to_confidence=high`），且该键**不**进入 `hypothesis_added` / `hypothesis_resolved`

#### 场景:空 supporting_findings 的假设不可锚定
- **当** 任一侧含一条 `supporting_findings` 为空的假设
- **那么** 该假设必须被排除出匹配、计入 `hypothesis_unanchored`，且禁止出现在 `hypothesis_added` / `hypothesis_resolved` / `hypothesis_confidence_changed`

#### 场景:同证据集两次确定性巡检的 hypothesis diff 为空
- **当** 同一场景两次确定性回放产出**证据集键相同**的 hypotheses（`supporting_findings` 解析为同一组 `Finding.id`；description 是否逐字相同**无关**，因其不入键），对其两份 Report 调 `compute_diff`
- **那么** `hypothesis_added` / `hypothesis_resolved` / `hypothesis_confidence_changed` 必须均为空（证据集键集合相同 → 无变化），证 hypothesis diff 管线可离线确定性消费

#### 场景:碰撞键作为新增键时 added 每键只发一条 representative
- **当** current 含 2 条共享同一证据集键（confidence 相同、description 不同）的假设、该键在 baseline 不存在
- **那么** `hypothesis_added` 必须**恰好含 1 条** `HypothesisFingerprint`（该键的确定性 representative：per-report `(sorted(supporting_findings), confidence, description)` 排序后第一条），**不是** 2 条（added/resolved 以键为单位、每键一条）

#### 场景:碰撞歧义键的 confidence 变化计入 hypothesis_ambiguous_keys 而非静默
- **当** 某证据集键在 baseline 侧恰好 1 条假设（如 `confidence=low`）、在 current 侧有 2 条同键假设（碰撞，其一 `confidence=high`）
- **那么** 该键的 confidence 比较必须被跳过（不猜归属），且 `hypothesis_ambiguous_keys` 必须计入该键（不为 0），该置信度变化**禁止**被无声丢弃（added/resolved 仍按键集合正确——该键两侧都在故不进 added/resolved）

#### 场景:inspector 升级时 hypothesis 键不重写且照常参与集合差
- **当** baseline 与 current 间有 inspector 升级（rule 5 将其 findings 从 finding diff 排除），且某 hypothesis 的 `supporting_findings` 引用了被排除的 finding id
- **那么** 该 hypothesis 的键**不**被重写（仍用原样声明的 `supporting_findings`），该假设照常参与 hypothesis 集合差（可能因键漂移产生 added/resolved 对）——这是 v1 有意行为，不是漏判

#### 场景:基线污染门对 hypothesis 同样生效
- **当** baseline 状态非 ok 且未 `force`（或 schema 版本不一致、或含 None finding id）
- **那么** `compute_diff` 必须按现有规则设置对应 `diff_skipped_reason` 并使三个 hypothesis 列表、`hypothesis_unanchored` 与 `hypothesis_ambiguous_keys` 全为零值（hypothesis diff 不绕过门单独计算）

#### 场景:匹配仅基于证据集合而非 description 文本
- **当** 两份 Report 含证据集键相同、但 `description` 文本不同的假设
- **那么** 两者必须匹配为同一假设（不进 added/resolved；置信度相同则也不进 confidence_changed），证明匹配只看 `supporting_findings` 不看 `description`

### 需求:`hostlens reports diff` 必须渲染 hypothesis 段

`hostlens reports diff` 的文本渲染必须在现有 finding 段（added / resolved / changed_severity）之后追加 hypothesis 段：`hypothesis_added`（前缀 `+`，含置信度与 description）、`hypothesis_resolved`（前缀 `-`）、`hypothesis_confidence_changed`（前缀 `~`，`from -> to`）。hypothesis 段必须含一行**固定可断言**的匹配口径说明，其中必须出现子串 `按 supporting_findings 证据集匹配`（如实表述 hypothesis diff 基于证据集合而非 description 语义判同——以固定子串使该要求可机械断言，不沦为主观措辞）。当 `hypothesis_unanchored` 大于 0 时，必须输出一行明示「N 条（两 run 合计）假设无 supporting_findings、未参与对比」（措辞须含「两 run 合计」以免误解为 N 条不同假设）。当 `hypothesis_ambiguous_keys` 大于 0 时，必须输出一行明示「N 个证据集键因一报告内多条同键假设、置信度归属歧义，其 confidence 变化未计算」。当 `inspector_upgraded` 非空时，必须在 hypothesis 段追加一行 caveat，明示部分 hypothesis 的 added/resolved 可能由证据 finding 的 inspector 升级（而非真实诊断变化）导致。当 `diff_skipped_reason` 非空时，必须维持现有单行「diff 跳过」早返、不渲染 hypothesis 段（含上述说明行 / 提示行均不渲染）。hypothesis 段内各行必须按**固定规范顺序**输出（确定性、可机械断言）：匹配口径说明行 → `hypothesis_added` → `hypothesis_resolved` → `hypothesis_confidence_changed` → `hypothesis_unanchored` 提示 → `hypothesis_ambiguous_keys` 提示 → inspector caveat。固定子串 `按 supporting_findings 证据集匹配` 为**冻结契约文案**：改动该子串须同步更新本 spec 与对应 CLI 测试（视为契约变更，非随手改文案）。

#### 场景:diff 输出含 hypothesis 段
- **当** 一次 `reports diff` 的 `RegressionDiff` 含至少一条 `hypothesis_added`
- **那么** stdout 必须含 `hypothesis_added` 段并逐条列出其置信度与 description

#### 场景:hypothesis 段含固定匹配口径说明
- **当** 一次 `reports diff` 未被跳过（`diff_skipped_reason` 为空）
- **那么** stdout 的 hypothesis 段必须含固定子串 `按 supporting_findings 证据集匹配`（如实表述匹配基于证据集而非 description）

#### 场景:存在未锚定假设时显式提示
- **当** `RegressionDiff.hypothesis_unanchored` 大于 0
- **那么** stdout 必须输出一行明示有 N 条（两 run 合计）假设因无 `supporting_findings` 未参与对比（不静默忽略）

#### 场景:存在碰撞歧义键时显式提示
- **当** `RegressionDiff.hypothesis_ambiguous_keys` 大于 0
- **那么** stdout 必须输出一行明示有 N 个证据集键因同键多条假设、置信度归属歧义而未计算 confidence 变化（不静默忽略）

#### 场景:inspector 升级时输出 caveat
- **当** `RegressionDiff.inspector_upgraded` 非空
- **那么** stdout 的 hypothesis 段必须含一行 caveat，明示部分 hypothesis 的 added/resolved 可能由证据 finding 的 inspector 升级导致、非真实诊断变化

#### 场景:跳过 diff 时不渲染 hypothesis 段
- **当** `RegressionDiff.diff_skipped_reason` 非空
- **那么** stdout 必须维持现有「diff 跳过: <reason>」单行输出，且不渲染任何 hypothesis 段
