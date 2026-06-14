## 修改需求

### 需求:`Finding` Pydantic 模型必须严格四字段且是 Finding SOT

`hostlens.reporting.models.Finding` 必须是 Pydantic v2 模型（`model_config = ConfigDict(extra="forbid", frozen=True)`），含**恰好**以下字段——四个 M1 核心字段（不变）+ 三个 M3 add-only 身份字段（全部带默认值）+ 一个本提案新增 add-only 来源字段（带默认值，旧构造方与旧 JSON 零改动可加载）：

核心字段（M1，不变）：

- `severity: Severity`
- `message: str`（min_length=1）
- `evidence: list[Evidence] = []`
- `tags: list[Tag] = []`（M1 finding DSL 不生产 tags；用于 M5 Notifier `only_if` 路由；每个 tag 约束 `^[a-z][a-z0-9_-]*$`）

M3 add-only 身份字段（用于 diff 指纹与根因假设引用；见 §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹）：

- `id: str | None = None`（确定性内容指纹；`from_inspector_results` 自动计算；直接构造或 legacy JSON 缺省为 None）
- `inspector_name: str | None = None`（产出该 finding 的 Inspector name；工厂从所属 `InspectorResult.name` 填充）
- `inspector_version: str | None = None`（Inspector version；工厂从 `InspectorResult.version` 填充；diff 版本对齐用）

本提案 add-only 来源字段（用于多 target / fleet Report 标注每条 finding 的来源 target；见 §需求:多 target Report 必须由确定性 fleet 组装路径产出）：

- `target_name: str | None = None`（产出该 finding 的来源 target 名；默认 `None` → 旧构造方 / 旧 JSON 零改动可加载；多 target 组装路径给每条 flatten 出的 finding 盖来源 `InspectorResult.target_name`；单 target 路径可留 `None` 或盖单值。**禁止**纳入 `compute_finding_id` 指纹——保单 target finding id 跨 run 稳定，见下「不纳入指纹」约束）

`extra="forbid"` 仍生效。`hostlens.reporting.models.Finding` 是 **唯一 SOT**；以下 import path 必须是 type alias re-export，**禁止**独立定义：

- `hostlens.inspectors.result.Finding` = `from hostlens.reporting.models import Finding as Finding`
- `hostlens.tools.schemas.run_inspector.FindingSummary` = `FindingSummary = Finding`

**`target_name` 不纳入 `compute_finding_id`**：指纹仍恒为 `sha256(f"{inspector_name}\x00{inspector_version}\x00{message}")[:16]`（见 §需求:`Finding.id` 必须是确定性 severity-agnostic 内容指纹，本提案**不改**该指纹定义）；`target_name` 是 add-only 标注字段，**禁止**进入指纹，否则同一检查项跨 target 会得到不同 id、破坏 per-target regression diff 的同 id 锚点。

#### 场景:Finding 字段集严格（核心四字段 + M3 身份字段 + 来源字段，拒绝未声明字段）

- **当** 试图 `Finding(severity="info", message="x", evidence=[], tags=[], extra="y")`
- **那么** 必须 raise `pydantic.ValidationError`（extra=forbid）

#### 场景:Finding 默认 evidence 与 tags 为空 list

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.evidence == []` 且 `finding.tags == []` 必须均为 True

#### 场景:Finding 仅核心字段时身份字段与来源字段默认 None

- **当** 构造 `Finding(severity="info", message="ok")`
- **那么** `finding.id is None` 且 `finding.inspector_name is None` 且 `finding.inspector_version is None` 且 `finding.target_name is None` 必须均为 True

#### 场景:单 target 路径构造 finding 不带来源 target_name

- **当** 在单 target 路径构造 `Finding(severity="info", message="ok")`（不传 `target_name`）
- **那么** `finding.target_name is None` 必须为 True（向后兼容,单 target 不强制盖来源标注）

#### 场景:Finding 接受显式来源 target_name

- **当** 构造 `Finding(severity="warning", message="cpu high", target_name="aliyun-bj")`
- **那么** 必须成功且 `finding.target_name == "aliyun-bj"`

#### 场景:Finding 接受显式身份字段

- **当** 构造 `Finding(severity="warning", message="cpu high", id="abc123", inspector_name="linux.cpu.top_processes", inspector_version="1.0.0")`
- **那么** 必须成功且三个身份字段按传入值保存

#### 场景:Finding 接受 Evidence 实例列表

- **当** 构造 `Finding(severity="critical", message="db down", evidence=[Evidence(kind="command_output", command="ping db", stdout="", stderr="timeout", exit_code=1)])`
- **那么** 必须成功且 `finding.evidence[0].kind == "command_output"`

#### 场景:Finding 接受 tags 列表

- **当** 构造 `Finding(severity="warning", message="cpu high", tags=["cpu", "perf"])`
- **那么** 必须成功且 `finding.tags == ["cpu", "perf"]`

#### 场景:Finding 拒绝 dict 形式 evidence

- **当** 试图 `Finding(severity="info", message="x", evidence={"key": "value"})`（dict 而非 list）
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 拒绝 list 中混入非 Evidence 元素

- **当** 试图 `Finding(severity="info", message="x", evidence=["not an evidence"])`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 拒绝非字符串 tags

- **当** 试图 `Finding(severity="info", message="x", tags=[123, None])`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:Finding 是 frozen 不可变

- **当** 构造 `f = Finding(severity="info", message="x")` 后试图 `f.severity = "critical"`
- **那么** 必须 raise `pydantic.ValidationError` 或 `TypeError`（Pydantic v2 frozen 行为）

#### 场景:Finding type alias 路径必须可 import

- **当** 执行 `from hostlens.inspectors.result import Finding as F1; from hostlens.tools.schemas.run_inspector import FindingSummary as F2; from hostlens.reporting.models import Finding as F3`
- **那么** `F1 is F3` 与 `F2 is F3` 必须均为 True（type alias，不是子类）

#### 场景:legacy 缺身份字段与来源字段的 dict 可加载

- **当** 执行 `Finding.model_validate({"severity": "info", "message": "x"})`（旧 schema 产出的 finding，无 id/inspector_name/inspector_version/target_name）
- **那么** 必须成功且四个 add-only 字段均为 None（add-only 向后兼容）

#### 场景:target_name 不改变 finding id

- **当** 两次以同 `inspector_name`/`inspector_version`/`message` 但不同 `target_name`（一个 `"a"` 一个 `"b"`）经 `compute_finding_id` 计算 id（指纹 helper 入参不含 target_name）
- **那么** 两次 `id` 必须**相同**（`target_name` 不参与指纹）

## 新增需求

### 需求:多 target Report 必须由确定性 fleet 组装路径产出

除既有单 target `Report.from_inspector_results`（target 单值）外,**必须**提供一条多 target（fleet）Report 组装路径,接受**跨多个 target** 的 `InspectorResult` 列表并组装成**一份** Report,供确定性巡检模式（见 `deterministic-inspection-mode` 能力）的逐 target 采集结果聚合使用。该路径**必须**:

- 接受多 target 的 `inspector_results`（每个 `InspectorResult` 携带自己的 `target_name`）。
- 把 `Report.target_name` 设为**确定性 fleet 标签**——由参与的 target 名按**确定性**规则派生（如有序 target 名 join），满足 `Report.target_name` 的 `min_length=1` 约束;同一组 target 同序输入**必须**派生同一标签（确定性、可复现）。
- 把 `meta.target_id` 设为**确定性 fleet id**——由有序 target_id 集合 + `schedule_name` 派生,使**不同 fleet**（不同 target 集合或不同 schedule）得到**不同** `target_id`,避免在 `ReportStore` 中撞 store key（per-target store key 复用既有 target_id-keyed 语义）。
- flatten findings 时给**每条** finding 盖**来源** `target_name`（取自该 finding 所属 `InspectorResult.target_name`）,使一份 fleet Report 内可按来源 target 区分 findings。

既有单 target `from_inspector_results` 行为**不变**（target 单值、不强制盖 finding 来源 target_name）。

#### 场景:多 target 组装产出一份 Report

- **当** 以 `targets=[a, b]` 的混合 `InspectorResult`（`a` 与 `b` 各自的结果各带其 `target_name`）经 fleet 组装路径组装
- **那么** **必须**产出**一份** `Report`,其 `inspector_results` 含 a 与 b 的全部结果,`findings` 是跨 a/b 的扁平视图

#### 场景:fleet Report 的 findings 带来源 target_name

- **当** fleet 组装路径 flatten `targets=[a, b]` 的 findings
- **那么** 来自 `a` 的 `InspectorResult` 的每条 finding `target_name == "a"`,来自 `b` 的每条 finding `target_name == "b"`

#### 场景:fleet target_id 由有序 target 集合与 schedule 确定性派生

- **当** 对同一组 `targets`（同序）+ 同一 `schedule_name` 两次组装 fleet Report
- **那么** 两次的 `meta.target_id` **必须相同**;而对**不同** target 集合或不同 `schedule_name` 组装时 `meta.target_id` **必须不同**（避免不同 fleet 撞 store key）

#### 场景:fleet target_name 标签确定性

- **当** 对同一组 `targets`（同序）两次组装 fleet Report
- **那么** 两次的 `Report.target_name` **必须相同**且满足 `min_length=1`

### 需求:fleet（多 target）Report 的 per-target regression diff 是非目标

多 target（fleet）Report 是 **notify 导向**的聚合产物;**per-target regression diff 仍只在 per-target（agent 模式）report 上做**。fleet Report 持有**单一** `meta.target_id`（fleet id),**无法**为其内含的每个 target 取 per-target baseline,故**禁止**期望对 fleet Report 做 per-target regression diff。`report-regression-diff` 的 target_id-keyed baseline 语义对 fleet Report **不适用**:fleet Report 的 baseline（若做）只能是「同 fleet id 的上一份 fleet Report」整体比对,**不**拆分到每个 target。本提案**不**为 fleet Report 实现任何 diff;regression diff 的既有 per-target 契约不变。

#### 场景:fleet Report 不期望 per-target baseline

- **当** 一份 fleet（多 target）Report 落盘后
- **那么** **禁止**对其执行 per-target regression diff（按各内含 target 分别取 baseline）;per-target diff 仅适用于 agent 模式的单 target report
