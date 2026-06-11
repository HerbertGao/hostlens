## 上下文

M9 P1a 已冻结 `RemediationPlan` / `RemediationStep`（归档）。本提案 P1b 实现「为 finding 拟方案、不执行」的 Agent。

现有地基（直接复用）：
- `AgentLoop`（`agent/loop.py`）—— 手写 tool-use 循环，返回通用 `LoopResult`，含降级终态、prompt-cache capability 检查。
- `DiagnosticianAgent`（`agent/diagnostician.py`）—— 第二个 AgentLoop agent 的**完整范本**：外部系统提示 + fail-fast、findings 进 messages 保 system byte-stable、`correlate_findings` 结构化输出通道（序号标签 + harvest）、`request_more_inspection` 复用 `InspectorRunner`、编排层 harvest/reconcile/控制流 helper。
- `ToolSpec`（`tools/base.py`）—— `side_effects` / `surfaces` / `requires_approval` 策略元数据；`tools_adapter.py` M2 gate 禁 write/destructive/approval 上 agent 表面。
- P1a `RemediationStep` / `RemediationPlan`（`remediation/models.py`）—— 全 fail-closed，复用。

P1b 几乎是 Diagnostician 的「平移」——把「产假设」换成「产方案」，把 `correlate_findings` 换成 `propose_remediation`。

## 目标 / 非目标

**目标：**
- 第三个 AgentLoop agent，产出经 P1a 校验的 `RemediationPlan`，**不执行**。
- 复用 Diagnostician 的结构化输出 + harvest + prompt-cache 模式，最大化一致性、最小化新概念。
- Agent 表面保持永久只读（M9 不变量 1）。

**非目标：**
- 不执行 / 审批 / 渲染 / audit（P2/P3）。
- 不改 P1a schema、不改 AgentLoop / tools_adapter 契约、不碰 ToolContext / ApprovalService。
- 不在 schema 层推断 risk_level（prompt 职责）。

## 决策

### 决策 1：第三个 AgentLoop agent，镜像 Diagnostician 的装配

`RemediationPlannerAgent` 复用 `AgentLoop`（不另写 loop——简历价值在「一个手写 loop 服务多个 agent」）。外部系统提示 markdown + 构造期 fail-fast（缺模板抛 `ConfigError`）。串接于 Diagnostician 之后：消费其 findings（带稳定 id）+ 根因假设。

**替代方案**：(a) 给 Diagnostician 加「产方案」职责 —— 否决：违反单一职责，且诊断与修复的 prompt / 输出契约不同。(b) 静态模板（无 LLM 拟方案）—— 否决：方案依赖当前远端状态，需 Agent 复核（见决策 4）。

### 决策 2：`propose_remediation` —— 一份方案一次调用，`steps` 复用 `RemediationStep`，emit 即校验 P1a 全部不变量

镜像 `correlate_findings`（一假设一调用）：planner 每为一个 finding 拟方案就调一次 `propose_remediation`，carry 整份方案体。**`steps` 字段直接复用 P1a `RemediationStep`**（非另造 mirror）——好处是 emit 时 `ToolsAdapter.dispatch` 按 `RemediationStep` `model_validate` 校验，模型产出违反不变量的 step 立即被拒（adapter `except Exception → raise TypeError`，loop `_dispatch_one` catch → 脱敏 error envelope → 模型自纠，既有路径），把 P1a 不变量执法前移到 emit 边界、零额外代码。

**注意 `RemediationStep` 有三层校验、emit 全执法**（非只 `high ⟹ precheck`）：① `high_requires_precheck`（high ⟹ precheck 非 None）② `rollback_none_requires_high`（rollback=None ⟹ high）③ 命令字段非空白（`command_must_not_be_blank`）。这意味着模型若产一个 `low`/`medium` step 却把 `rollback_cmd` 留 None，会**每次 emit 被弹**——故系统提示必须教全部三条（见决策 7），否则陷入自纠死循环。**注意执法路径**：emit 校验只在走 `ToolsAdapter.dispatch`（`model_validate`）时触发；裸 `ToolRegistry.dispatch` 只 `isinstance` 不校验——loop 既有走 adapter，实现期须确保不绕过。

输入 schema `ProposeRemediationInput`（`extra="forbid"`, `frozen`）：
- `finding_label: str`（序号标签，见决策 3）
- `rationale: str`
- `estimated_duration_seconds: StrictInt(ge=0)`
- `steps: list[RemediationStep]`（min_length=1）

`side_effects="none"`、`surfaces={"agent"}`；handler **closure-bind per-run `FindingStore`、对 `finding_label` 做 hit-check**（镜像 `correlate_findings_handler`：标签悬空 → `ToolError` → envelope → 自纠），命中则回传不含真 id 的 ack。

**替代方案**：逐 step 发射（多次 `propose_remediation_step`）—— 否决：plan 的 step 有序且绑定一个 finding + 共享 rationale/估时，整份发射更自然，且与「一假设一调用」一致。

### 决策 3：`finding_id` / `target_name` 由编排层盖，不由模型产（标签 handler hit-check、harvest resolve，镜像 D-2/D-8/D-9）

模型在 `propose_remediation` 里用**序号标签** `F1`/`F2` 引用 finding（镜像 Diagnostician D-9：模型只见标签、不见 16-hex 真 id，抗转写错误）。标签的两阶段处理**严格镜像 Diagnostician**：
- **handler 阶段 hit-check**（D-8）：handler 拿 closure-bound `FindingStore`，校验 `finding_label` 是否在 store 中；悬空 → `ToolError`（结构化 envelope → 模型自纠），命中 → ack。**故悬空标签在 emit 边界就被挡，永不抵达 harvest。**
- **harvest 阶段 resolve**（D-2）：harvest 只遍历**成功**调用，把已 hit-check 过的标签 resolve 成真 `Finding.id`。因 handler 已保证不悬空，harvest 对悬空 **fail-loud**（理论不可达，防御性 raise，镜像 `harvest_hypotheses` 的 fail-loud 处理）。

`target_name` 由编排层盖：**P1b planner run 与 Diagnostician 同作用域＝单 target**（`run_diagnosis_pipeline` 全程 `report_target_name == target_lookup_name`、`Finding` 不带 target 字段）；planner 为该 run 的单一 target 拟方案，`target_name` 即该 run 的 target，不存在跨 target 情形。

**同 id 碰撞继承 D-9 语义**：`compute_finding_id` 排除 severity，故同 message 不同 severity 的 finding 得同真 id；多个标签 resolve 到同一真 id 时，harvest **不去重**，产出多份 `finding_id` 相同的 `RemediationPlan` 属预期（与 `harvest_hypotheses` 同 id 不互覆盖一致）。

**好处**：模型不接触真 id / target 名，消除转写错误；`finding_id`/`target_name` 的 P1a 非空校验由「编排层盖真值」天然满足。

### 决策 4：专用工具装配 `register_remediation_planner_tools` + 只读复核

planner 的工具集 = `request_more_inspection` + `list_inspectors`（复用）+ `propose_remediation`，**与 `register_diagnostician_tools` 的成员同构**（把 `correlate_findings` 换成 `propose_remediation`）。拟方案前可用 `list_inspectors` 发现可用 inspector、用 `request_more_inspection` 复核 finding 现状（如「磁盘是否仍满」），据实拟方案——这是「为何是 Agent 而非静态模板」的核心，把 P1a 决策 1 的 precheck 动机（抗 TOCTOU）延伸到「拟方案时也先看一眼现状」。

**为何不用 `run_inspector`**：`RunInspectorInput` 要求模型提供 `target_name`，与 D-3「模型不接触 target 名、target_name 由编排层盖」直接冲突；且其 `agent_description` 引用 `list_targets`（planner 不应有）、`surfaces` 含 `mcp`。复核统一走 `request_more_inspection`——其输入仅 `inspector_name` + `parameters`、`target` 由装配时 closure-bind（模型不触 target 名，D-3 在复核路径同样成立）。

**专用装配函数**（非 `register_default_tools`）：`propose_remediation` 与 `request_more_inspection` 一样需 closure-bind per-run `FindingStore` + `target_name`，故镜像 `register_diagnostician_tools` 新增 `register_remediation_planner_tools(registry, *, finding_store, target_name, ...)`，装配上述三个工具、**排除 `correlate_findings`**（planner 不产假设）。`list_inspectors` 是 module-level（无闭包）。`register_default_tools` 不放 closure-bound 的 planner 工具。

**`FindingStore` seeding**：planner 持一个与首条 user message 标签**同源**的 `FindingStore`——seed 自诊断阶段已盖稳定 id 的 canonical findings（编排层从 `DiagnosticianResult` 取，复用 `_seed_findings_from_snapshot` 同款逻辑）。首条 message 列出的 `F1`/`F2` 标签、handler hit-check、harvest resolve 三者必须读同一个 store，否则解析全悬空。planner 内 `request_more_inspection` 若中途 append 新 finding（分配新标签），该新标签亦可被 `propose_remediation` 引用（镜像 Diagnostician「后续 turn 的 finding 可被引用」）。

**`request_more_inspection` 复用细节**：其既有 `agent_description` 提及 `correlate_findings`（planner 无此工具）——verbatim 复用会向 planner prompt 泄漏不存在的工具名。**决定**：planner 装配时给 `request_more_inspection` 一个 description 变体，把句尾对 `correlate_findings` 的提及替换为 `propose_remediation`（成本一行字符串，避免向模型暗示一个它没有的工具）；不接受该 prompt 噪音。tasks 1.4 据此实现。

**Agent 表面只读不变量**：上述三个工具 `side_effects` 全 ∈ `{"none","read"}`（`request_more_inspection`=read / `list_inspectors`=read / `propose_remediation`=none）；P1b 不注册任何 write/destructive/approval 工具，`tools_adapter` M2 gate 不放开。`propose_remediation` 的 `side_effects="none"` 是关键——它产出**数据**（拟议方案），不是执行。

### 决策 5：harvest / 控制流 —— 平移 Diagnostician 编排层

- `harvest_plans(loop, finding_store, target_name)`：遍历**成功** `propose_remediation` 调用 → resolve 标签（已 handler hit-check、保证命中）→ 盖 target_name → 构造 P1a `RemediationPlan`。**镜像 `harvest_hypotheses` 的 fail-loud**：因 handler 已挡悬空，harvest 遇悬空标签 raise（理论不可达的防御性 raise，非「记录无效跳过」——后者是 handler 不 hit-check 时的相反架构，本提案不取）。构造 `RemediationPlan` 此时确定成功（steps 已 emit 校验、finding_id/target_name 编排层盖非空），无 P1a 校验失败路径。
- `run_remediation_planning(...)`：控制流缝——**仅当诊断成功（status==ok）且 findings 非空才启动 planner**（镜像 `run_diagnosis` 仅判 Planner ok 的可机械判定谓词；**不引入「值得修复」之类需 severity 启发式的筛选**——那与 proposal 非目标一致、留 P2）。
- `RemediationPlannerResult`：聚合 `list[RemediationPlan]`（可空）+ status。**status 是 planner loop 的 `terminal_status` 直传**——P1b 是单 loop，无第二 loop 需对账，**不引入 Diagnostician 的 `reconcile_status` 双 loop 机器**（那是 overkill）。类型为 loop 的 `_TerminalStatus`（与 `LoopResult.terminal_status` 同）。

### 决策 6：prompt caching —— 平移 Diagnostician

系统提示 + `RemediationStep` schema 描述（固定）缓存；finding / 假设 / target（每 run 变）进首条 user message、绝不进 system，保 system byte-stable。`prompt_caching=False` 时 loop 不注入 cache_control（既有行为）。

**message 渲染格式**：finding 用 `F1`/`F2` 序号标签（复用 Diagnostician 的 `_render_findings_block`，与 seed 同源）；根因假设用 `H1`/`H2` 序号标签渲染（confidence + description + suggested_actions），**省略 `supporting_findings` 的真 id**——保持模型只见 `F`-标签、不触 16-hex 真 id（与 D-3 一致）。

### 决策 7：risk_level 由 prompt 引导，不由 schema 推断；prompt 须教 P1a 全部三条不变量

系统提示必须教模型 `RemediationStep` 的**全部**约束，否则模型会在 emit 反复被弹：
- `risk_level` 评定：`rm -rf` / `kill -9` / 改 systemd unit → `"high"`；幂等无害动作可 `low`/`medium`。
- `high_requires_precheck`：`high` step 必须给 `precheck_cmd`（抗 TOCTOU）。
- **`rollback_none_requires_high`**：`low`/`medium` step **必须给 `rollback_cmd`**；只有 `high` step 才可把 `rollback_cmd` 留 None（不可回滚 ⟹ 最高警觉）。
- 命令字段非空白：`forward_cmd`/`verify_cmd`/给出的 `precheck_cmd`/`rollback_cmd` 不可为空或纯空白。

schema 只校验结构不变量、不做内容启发式（P1a Non-Goal）。prompt 写错风险由 cassette 测试 + P1↔P2 门控（人判方案可执行）兜底。

## 风险 / 权衡

- [模型产出语法合法但语义糟糕的方案（如 precheck 检错前提）] → schema 拦不住语义；靠系统提示质量 + P1↔P2 人判门控（录像为证）+ 后续 P2 的 precheck 实际执行兜底。本提案不追求「方案语义正确性可机械证」。
- [`steps` 复用 `RemediationStep`：跨字段不变量（`high⟹precheck` / `rollback=None⟹high`）**不入** `model_json_schema()` 投影——模型从 tool schema 看不到它们] → 字段级约束（`risk_level` enum / `min_length`）随 schema 投影；跨字段不变量靠**系统提示引导 + emit-time 拒绝 round-trip** 双管执法。故决策 7 的 prompt 教学是必需的（不是可选优化）：不教全，模型对不变量「盲」、靠反复被弹学，自纠成本高。
- [planner 复核 inspector 增加 token / 延迟] → 复核是可选的（模型自决是否调），默认单 finding 1 主往返；批量顺序跑控制突发。
- [risk 评定靠 prompt，跨模型可能漂移] → 接受：cassette 锁回归；真要更强保证属后续（不在 P1b）。

## Migration Plan

无迁移。纯新增（`remediation_planner.py` / `propose_remediation` schema+handler / `register_remediation_planner_tools` 装配函数 / 系统提示 md / 测试 + cassette）。不改现有模块契约（`register_default_tools` / `register_diagnostician_tools` 不动）。回滚 = 删新增文件（planner 工具走独立装配函数，删之即摘，不污染默认/诊断工具集；下游 P2 尚未实现，无消费方）。

## Open Questions

- 「哪些 finding 值得拟方案」的筛选策略：本提案默认编排层对传入 finding 逐个拟（最小化）。是否按 severity / 是否有根因假设筛选？倾向留 P2（执行侧更清楚什么值得修），P1b 不预设启发式。
- 一个 finding 是否可能需要多份候选方案（不同修复路径）？倾向 P1b 一 finding 一方案（最推荐的那个）；多候选属后续增量。
