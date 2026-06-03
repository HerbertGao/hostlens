# add-hypothesis-level-diff — Design

## 上下文

`reporting/diff.py` 的 `compute_diff(baseline, current)` 现按 severity-agnostic `Finding.id`（`compute_finding_id(name, version, message)` 的 content hash）对两 Report 的 findings 做集合差，产 `RegressionDiff{added, resolved, changed_severity, inspector_upgraded, diff_skipped_reason}`（闭 schema、frozen）。它有一套严格有序的防基线污染门（rule 0–6：meta 完整性 → per-target 隔离 → finding id 完整性 → baseline 状态门 → schema 对齐 → inspector 版本对齐 → 集合差）。

`Report.hypotheses: list[RootCauseHypothesis]` 自 `add-intent-report-persistence` / `wire-demo-to-report` 起已**随 Report 全量入库并渲染**，但 `compute_diff` **完全不读它**。`RootCauseHypothesis`（`models.py:286`，frozen、`extra="forbid"`）字段：
- `description: str` —— LLM 生成的自由文本。
- `confidence: Literal["low","medium","high"]`。
- `supporting_findings: list[str]` —— 引用的 `Finding.id`（intra-report 锚，content-derived 稳定）。
- `suggested_actions: list[str]`。

**关键约束**：`RootCauseHypothesis` **无 `id`**。要做 hypothesis 级 diff，必须先回答「两 run 的两条假设何时算同一条」——这正是本提案被从 `wire-demo-to-report` 拆出的原因。

CLI：`cli/reports.py:317 _render_diff` 把 `RegressionDiff` 渲成简洁文本（finding 段）；`reports diff` 支持显式两 run 与自动基线两模式。无 json diff 输出。

## 目标 / 非目标

**目标：**
- 为无 id 的 `RootCauseHypothesis` 设计**稳定、确定性、可离线验证**的匹配键。
- 扩展 `RegressionDiff` + `compute_diff` + `_render_diff`，在**同一套防污染门**下产 hypothesis 级 added / resolved / confidence_changed。
- 退化情形（空 `supporting_findings`、键碰撞）**显式处理不静默**。

**非目标：**
- description 的语义 / NLP / embedding / LLM 判同（见 D-1 否决）。
- 模糊 / Jaccard 近似匹配（v1 精确集合相等，见 D-1）。
- 给 `RootCauseHypothesis` 加 `id` 字段（牵动 data-model + 持久化迁移，超范围；键是 diff 引擎派生物）。
- 改 finding 匹配键 / store schema / CLI 命令签名 / json diff 导出。

## 决策

### D-1：匹配键 = `frozenset(supporting_findings)`（证据锚定，description 不入键）

把「同一根因假设」定义为「**引用同一组证据 finding 的假设**」：
```
hyp_key(h) = frozenset(h.supporting_findings)
```
- `supporting_findings` 是 content-derived 稳定 `Finding.id` 集合 → 键跨 run 稳定、顺序无关、可离线确定性复现。
- `frozenset` 使「引用顺序不同但集合相同」匹配（与 finding diff 的集合差语义一致）。

**为何 description 不入键**：`description` 是 LLM 自由文本，真实 `--intent` run **不可能逐字复现**（demo cassette 录死故确定，但生产路径必抖）。把它入键 = 同一根因每次都不匹配 = 全报成 added+resolved，diff 失去意义。description **仅作渲染展示**（`HypothesisFingerprint.description`），不参与匹配。

**否决的替代**：(a) description content hash —— 不稳定，已述；(b) 给模型加 `id` —— `compute_finding_id` 式 content hash 必含 description（否则同证据不同根因撞键），又回到 description 不稳定问题，且改 data-model 超范围；(c) Jaccard 近似 —— 引入阈值、破坏确定性与可测性，v1 不做（Non-Goal）。

**v1 接受的代价（诚实标注）**：若同一根因跨 run 引用的 finding 集合漂移了一个（某支持 finding 被 resolved），键变 → 同根因被记成 `hypothesis_resolved`(旧键)+`hypothesis_added`(新键)。这是证据锚定的必然代价，优于不可靠的近似匹配。spec / 渲染如实表述「added/resolved 基于证据集合，不是语义判同」。

### D-2：空 `supporting_findings` 的 hypothesis 不可锚定 → 计入 `hypothesis_unanchored`，不进 added/resolved

`supporting_findings` 为空（LLM 没引证据）的假设键为 `frozenset()`。若不排除：两侧所有空-support 假设塌缩到同一空键 → 大规模假碰撞。故：
- **keying 前排除** `supporting_findings` 为空的假设（两侧都排除）。
- 被排除的条数计入新字段 `hypothesis_unanchored: int`，**语义是两 run 合计的出现次数**（baseline 侧排除数 + current 侧排除数）：同一条空-support 假设若两侧都出现会计 2 次，故它是「未参与对比的假设出现次数总和」而非「去重假设数」。CLI 输出须如实表述「N 条（两 run 合计）」避免读者误解为 N 条不同假设。
- **绝不**把空-support 假设塞进 added/resolved（否则每次 diff 都假报）。

这把「无法对比」与「对比出无变化」明确区分（呼应 finding diff 用 `diff_skipped_reason` 区分「跳过」与「空 delta」的精神）。

**键取「假设原样声明的 `supporting_findings`」、不校验集合成员有效性**：`compute_diff` 不验证 `supporting_findings` 里的 id 是否真出现在该 Report 的 finding 集合中。理由：diagnostician（`diagnostician.py:276-289`）把 ordinal label 解析成**真实** `Finding.id` 后才落盘，正常路径不会产生悬空引用；且 diff 是纯集合比较，键只要跨 run **一致**即可，不要求键中 id 当前可解析（与 D-5「键不因 inspector 升级重写」同一立场）。即便构造出引用幽灵 id 的假设，其键仍跨 run 稳定比较、不破坏 added/resolved 正确性。故 v1 不加成员校验门（YAGNI）。

### D-3：键碰撞（一报告内多条假设引用同一 finding 集合）→ 碰撞安全 + 歧义键计数（不静默）

一份报告内两条假设若 `frozenset(supporting_findings)` 相同：
- **added/resolved 按键集合（set of keys）算、每键最多发一条 representative → 碰撞安全**：集合差只看「键在/不在」，结果不受同键多条影响；该键被新增/消解时，added/resolved 只发射**一条**确定性 representative（per-report 排序后第一条），同键其余假设被 collapse、不逐条发射、不单独计数。这保住「键在/不在」的核心正确性（无键级数据丢失），而**故意不**逐条体现同键多假设（v1 YAGNI；逐条列出留作未来独立提案）。注意：added/resolved 的 collapse 不计入 `hypothesis_ambiguous_keys`——该计数只服务于「confidence 比较因歧义被跳过」（下一条），与 added/resolved 的键级 collapse 是两件事。
- **confidence_changed 仅对两侧均「该键恰好 1 条」的键发射**：某侧该键 >1 条时 confidence 归属歧义 → **跳过该键的 confidence 比较**（不猜）。
- 为可重现，build 键映射前对每报告 hypotheses 按 `(sorted(supporting_findings), confidence, description)` 稳定排序，使确定性 representative 固定。其中 `confidence` 按**字符串字典序**参与排序（`"high" < "low" < "medium"`，**非** severity 高低序）——这只影响碰撞键 representative 的 tie-break，当前所有场景均不依赖该次序（碰撞 added 场景 confidence 相同、歧义键不发射 representative）；实现者**不要**把它「修」成 severity 序，否则破坏确定性预期。若两条碰撞假设连 description 都相同（完全相同行），它们退化为不可区分，稳定排序下任取其一都正确（degenerate-but-deterministic）。

**歧义键必须留痕（守「不静默」红线）**：被跳过 confidence 比较的歧义键数计入新字段 `hypothesis_ambiguous_keys: int`（统计**两侧任一为该键发生碰撞、因而该键 confidence 未比较**的证据键个数；按键去重计数，不是假设条数），CLI 明示一行「N 个证据集键因一报告内多条同键假设、置信度归属歧义，其 confidence 变化未计算」。这与 D-2 的 `hypothesis_unanchored` 同构：把「无法判定的变化」显式计数而非无声吞掉——否则 baseline 该键 1 条 `low`、current 该键 2 条其一 `high` 的真实置信度上升会零信号消失，直接违反提案通篇「绝不静默」基调。

**schema 取舍**：碰撞罕见（diagnostician 当前每场景只产 1 条假设，碰撞需两条引用完全相同证据集的假设）。为不让 schema 膨胀，v1 **不**为碰撞单列 hypothesis list（added/resolved 已碰撞安全保住核心正确性），仅以 `hypothesis_ambiguous_keys` 计数 + CLI 提示暴露歧义。`hypothesis_unanchored` 只统计**空-support**、`hypothesis_ambiguous_keys` 只统计**碰撞歧义键**，两者语义不交叉、互不混入。若未来需逐条列出歧义假设再独立提案细化（YAGNI）。

### D-4：`RegressionDiff` schema 扩展（纯加法、闭 schema）

新增投影模型 + 字段：
```python
class HypothesisFingerprint(BaseModel):       # frozen, extra=forbid
    confidence: Literal["low","medium","high"]
    supporting_findings: list[str]            # sorted+deduped Finding.id（渲染/审计用；键由其 frozenset 派生）
    description: str                          # 仅展示，不入键

class ConfidenceChange(BaseModel):            # frozen, extra=forbid
    supporting_findings: list[str]            # 匹配键的可读形式（sorted+deduped）
    from_confidence: Literal["low","medium","high"]
    to_confidence: Literal["low","medium","high"]
    description: str                         # current 侧 description（展示）

# RegressionDiff 追加（用 Field(default_factory=list) 与现有字段风格一致）：
    hypothesis_added: list[HypothesisFingerprint] = Field(default_factory=list)
    hypothesis_resolved: list[HypothesisFingerprint] = Field(default_factory=list)
    hypothesis_confidence_changed: list[ConfidenceChange] = Field(default_factory=list)
    hypothesis_unanchored: int = 0
    hypothesis_ambiguous_keys: int = 0
```
- 现有 finding 字段（`added`/`resolved`/`changed_severity`/...）**不动**，纯加法向后兼容。新列表字段沿用现有 `Field(default_factory=list)` 写法（非裸 `= []`），与 `RegressionDiff` 既有字段风格统一。
- `diff_skipped_reason` 闭集**不扩**：hypothesis diff 与 finding diff 共用同一跳过判定（D-5），跳过时三个 hypothesis 列表与 `hypothesis_unanchored` / `hypothesis_ambiguous_keys` 全为零值。
- **投影模型的 `supporting_findings` 渲染列表须 sorted+deduped**：键由 `frozenset(supporting_findings)` 派生（frozenset 天然去重、顺序无关，故 `["A","A"]` 与 `["A"]` 键相同、匹配正确），但用于展示/审计的 `HypothesisFingerprint.supporting_findings` / `ConfidenceChange.supporting_findings` 必须 `sorted(set(...))`，使重复 id 或顺序差异不致产生不确定的渲染输出。
- **两处排序用途不同，须分别明确，勿混为一谈**：
  - ①**representative 选取排序**（建键映射前，per-report）：对每报告 hypotheses 按 `(sorted(supporting_findings), confidence, description)` 稳定排序，使「同键多条」时确定性 representative 固定（碰撞场景的可重现性，见 D-3）。
  - ②**输出列表排序**（三列表发射前）：键唯一即可确定顺序，按 `sorted(supporting_findings)`（即键的可读形式）排序三列表，保证离线回放逐元素可复现。

### D-5：`compute_diff` 集成 —— 复用同一防污染门，finding 后追加 hypothesis 段

在现有 rule 0–6 之后、`return` 之前追加 hypothesis 集合差，**不新增门、不绕过门**：
- rule 0（meta None）/ rule 2（finding id None）/ rule 3（baseline 非 ok）/ rule 4（schema 不一致）任一触发 → 现有早返 `RegressionDiff(diff_skipped_reason=...)`，三个 hypothesis 列表与 `hypothesis_unanchored` / `hypothesis_ambiguous_keys` 取默认零值（**hypothesis diff 一起被跳过**，语义一致）。
- rule 1（跨 target）→ 现有 `ValueError`，hypothesis 同样不计算（整体拒绝）。
- 通过全部门后：对 `baseline.hypotheses` / `current.hypotheses` 按 D-1 键、D-2 排除空-support、D-3 确定性 representative 做集合差，产三个列表 + `hypothesis_unanchored` + `hypothesis_ambiguous_keys`。
- **inspector 版本对齐（rule 5）对 hypothesis 的影响 —— v1 行为 + 必须可见的 caveat**：finding diff 会排除「升级 inspector」的 findings。hypothesis 的 `supporting_findings` 可能引用被排除的 finding id —— v1 **不**因此重写 hypothesis 键（键用假设原样声明的 `supporting_findings`，不做 inspector-version 过滤）。理由：hypothesis 是对**当时**证据的诊断，重写键会引入跨 inspector 版本的隐式语义；保持「键 = 假设声明的证据集」最简单可解释。
  **但这带来一个失败模式**：当某 inspector 升级使其 finding 的 message（进而 `Finding.id`）变化时，引用该 finding 的 hypothesis 键会漂移 → 同一根因被记成 `hypothesis_resolved`(旧键)+`hypothesis_added`(新键) 一对，**而 finding 段却因 rule 5 排除显示「无变化」**——用户看到「finding 没变但根因诊断翻转」会困惑。v1 不改键行为（改键的成本与隐式语义更糟），但**不静默放过**：
  - **spec 增显式场景**（见 spec 需求 2）标注此 v1 行为，使实现者知道「键不重写」是有意设计、不是漏判。
  - **CLI 可见信号**：当 `RegressionDiff.inspector_upgraded` 非空时，hypothesis 段追加一行 caveat：「注意: 存在 inspector 版本变更, 部分 hypothesis 的 added/resolved 可能由证据 finding 的 inspector 升级（而非真实诊断变化）导致」。该 caveat 仅 gate 在 `inspector_upgraded` 非空（已有字段），无需新增字段或 finding→inspector 映射，是与现有 finding 段 `inspector 版本变更:` 行对齐的低成本诚实披露。

### D-6：CLI 渲染 —— `_render_diff` 追加 hypothesis 段

在现有 finding 段后追加（保持简洁文本风格）：
```
hypothesis_added (N):
  + {confidence}: {description}
hypothesis_resolved (N):
  - {confidence}: {description}
hypothesis_confidence_changed (N):
  ~ {from} -> {to}: {description}
```
- **匹配口径说明行（固定文案，可机械断言）**：hypothesis 段起始处固定输出一行 `hypothesis diff: 按 supporting_findings 证据集匹配, 非 description 文本`。该子串是 CLI「如实表述匹配口径」需求的**可断言锚点**（CliRunner 测 `assert "按 supporting_findings 证据集匹配" in out`），避免「如实表述」沦为不可测的主观要求。
- `hypothesis_unanchored > 0` 时输出一行提示：`未锚定假设 (N, 两 run 合计): 无 supporting_findings, 未参与对比`（措辞含「两 run 合计」，呼应 D-2 的计数语义，避免读者误解为 N 条不同假设）。
- `hypothesis_ambiguous_keys > 0` 时输出一行提示：`歧义键 (N): 一报告内多条同键假设, confidence 变化未计算`（D-3 不静默信号）。
- `inspector_upgraded` 非空时，hypothesis 段追加一行 caveat：`注意: 存在 inspector 版本变更, 部分 hypothesis 的 added/resolved 可能由证据 finding 的 inspector 升级导致, 非真实诊断变化`（D-5 可见性）。
- `diff_skipped_reason` 非空时维持现有「diff 跳过: ...」单行早返（hypothesis 段——含上述说明行/提示行——也不渲染，一致）。
- **added/resolved 的同键 collapse v1 不向用户披露**：每个 added/resolved 键只渲染其 representative 的单条 `confidence`/`description`，**不**加「该键尚有 N 条同键假设被折叠」提示（理由见 D-3 / spec 需求2：键级语义、碰撞罕见、verbose 披露留作未来）。这与 `hypothesis_ambiguous_keys`（confidence 歧义键计数）不同——后者**要**提示，前者 v1 有意不提示。

### D-7：离线确定性验证（沿用 demo `--persist` 闭环 + 机械单测）

- **e2e（离线确定性）**：两次 `demo run <scenario> --persist`（确定性回放 → 两 run 的 hypotheses **证据集键相同**，因 `supporting_findings` 解析为同一组 content-derived `Finding.id`；注意空 delta 的成因是**键相同**而非 description 逐字复现——description 不入键）→ `reports diff <a> <b>` 的 hypothesis 段全空（`hypothesis_added/resolved/confidence_changed` 均 0、`hypothesis_unanchored == 0`、`hypothesis_ambiguous_keys == 0`）。证 hypothesis diff 管线可离线确定性消费 demo 产出（非证「demo 能造出 hypothesis 变化」——同场景确定性回放造不出，与 finding 级同理）。**run_id 捕获**：测试在隔离 `XDG_DATA_HOME` 下经 **store API**（如 `ReportStore` 列举/查询）取两条 run_id，**不**解析 CLI 文本输出；diff 的机械断言也直接对 `compute_diff` 返回的 `RegressionDiff` 字段做（CLI 文本只在 CLI 渲染测试里断言固定子串，见 D-6）。
- **added/resolved/confidence_changed 非空路径（不依赖 LLM 的机械单测）**：对 `compute_diff` 的**单元测试**直接构造两个 hypotheses 不同的 `Report`（不同 supporting_findings 集 → added/resolved；同集不同 confidence → confidence_changed），断言三列表的具体值。这是非空路径的**真正覆盖**（demo e2e 只覆盖空 delta 路径）。
- **退化/门继承单测**：空 supporting_findings → `hypothesis_unanchored` 计数且不进 added/resolved；键碰撞 → added/resolved 仍正确、confidence 对歧义键跳过且 `hypothesis_ambiguous_keys` 计数；baseline 非 ok / 跨 target / schema 不一致 → hypothesis 段随 finding 段一起跳过/拒绝（含 `hypothesis_ambiguous_keys == 0`）。
- **inspector-skew 行为单测**：构造 baseline/current 间有 inspector 升级、且某 hypothesis 引用了被 rule 5 排除的 finding —— 断言其键不被重写、该假设照常参与集合差（D-5 v1 行为），且 CLI 在 `inspector_upgraded` 非空时输出 caveat 行。

## 风险 / 权衡

- **[同根因 finding 集漂移 → 假 added+resolved 对]**（D-1 代价）→ 文档化为 v1 已知局限；证据锚定优先于不可靠近似；模糊匹配留作未来独立提案。
- **[键碰撞致 confidence 歧义]**（D-3）→ added/resolved 碰撞安全（核心保住），confidence 对歧义键确定性跳过、并计入 `hypothesis_ambiguous_keys` + CLI 明示一行，罕见且**不静默**（歧义有可见信号，不无声吞掉真实置信度变化）。
- **[inspector 升级 × hypothesis 键不重写 → 与 finding 段语义不一致]**（D-5 代价）→ v1 不改键（改键引入更糟的跨版本隐式语义）；但 spec 增显式场景 + CLI 在 `inspector_upgraded` 非空时输出 caveat 行，使该取舍可见而非静默放过。
- **[空 supporting_findings 假设无法对比]**（D-2）→ 显式 `hypothesis_unanchored` 计数（两 run 合计语义）+ CLI 提示，不假报、不静默。
- **[schema 加字段破坏既有 RegressionDiff 消费者]** → 纯加法、默认零值，现有 finding 字段与 `diff_skipped_reason` 闭集不动；既有 diff 测试作回归守护。
- **[把 hypothesis diff 误读成「语义判同」]** → spec / 渲染如实表述「基于 supporting_findings 证据集合，非 description 语义」；与 D-1 的 liveness/honesty 措辞一致。

## 迁移计划

- 纯加法：`RegressionDiff` 加字段、`compute_diff` 加段、`_render_diff` 加段。无持久化 schema / 数据迁移（hypotheses 已入库）。
- 回滚 = revert 该 PR；不涉及存储格式变化（旧 Report 仍可被新 diff 读，hypotheses 缺省为 `[]` → 三列表空）。

## 遗留问题

（无。description 不稳定性、退化键、碰撞、门继承均在 D-1~D-5 收口；模糊匹配显式列 Non-Goal 留未来。）
