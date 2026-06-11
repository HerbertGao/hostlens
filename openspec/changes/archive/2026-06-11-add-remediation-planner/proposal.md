## 为什么

M9 P1a 已冻结 `RemediationPlan` / `RemediationStep` 契约（已归档）。P1b 是 M9 第二片：**让 Agent 为一个 finding 产出一个修复方案**——但只「想」、不「做」。它是继 Planner（M2.4）、Diagnostician（M3）之后的**第三个 `AgentLoop`-backed Agent**，消费 Diagnostician 的 findings + 根因假设，用 **structured output** 产出 `RemediationPlan`，**绝不执行任何 step**。

为什么是 Agent 而非静态模板：修复方案依赖「**当前**远端状态」。Planner 在 T1 看到的 finding，到产出方案时世界可能已变；Agent 可以先用只读 Inspector 复核 finding 现状（如磁盘是否仍满），再据实拟方案——这是「理解意图 + 推理诊断」定位在修复侧的延伸。

P1b 是 P1↔P2 门控的前置：必须先证明 Planner 产出的方案质量足够「值得被执行」，才解锁 P2 的写路径（见 `TODO.md` M9 门控）。

## 变更内容

- 新增 `RemediationPlannerAgent`（`src/hostlens/agent/remediation_planner.py`），镜像 `DiagnosticianAgent`：复用 `AgentLoop` + 外部系统提示，串接于 Diagnostician 之后。输入 = finding（带稳定 id）+ 根因假设 + target 名；输出 = `RemediationPlan`（经 P1a schema 校验）。
- 新增 structured-output ToolSpec **`propose_remediation`**（`src/hostlens/tools/schemas/propose_remediation.py` + handler）：Agent 每为一个 finding 拟方案就调一次，carry 方案体（`rationale` + `steps: list[RemediationStep]` + `estimated_duration_seconds`）；`finding` 用**序号标签**（`F1`/`F2`，镜像 Diagnostician D-9）引用。handler **closure-bind per-run `FindingStore` 对标签 hit-check**（悬空 → `ToolError` 自纠，镜像 `correlate_findings`）、回传不含真 id 的 ack；真 id 由 harvest resolve。`side_effects="none"`、`surfaces={"agent"}`（**不上 MCP**——只读 agent 表面，无执行通道）。
- 新增专用装配函数 **`register_remediation_planner_tools`**（镜像 `register_diagnostician_tools` 成员，把 `correlate_findings` 换成 `propose_remediation`）：装配 `request_more_inspection` + `list_inspectors`（复用）+ `propose_remediation`，closure-bind per-run `FindingStore` + `target_name`，**排除 `correlate_findings` 与 `run_inspector`**（后者要模型给 target_name、破 D-3）；不进 `register_default_tools`。复核统一走 `request_more_inspection`（target 闭包绑定、模型只给 inspector_name）。
- 编排层 helper（`remediation_planner.py` 内）：`harvest_plans`（遍历成功 `propose_remediation` 调用，resolve 标签 → 真 id、盖 `target_name`、构造 P1a `RemediationPlan`；对悬空标签 **fail-loud**——因 handler 已 hit-check 保证不悬空，镜像 `harvest_hypotheses`）+ `run_remediation_planning`（控制流缝：**仅当诊断成功（status==ok）且 findings 非空才启动**，不做 severity 启发式筛选）。
- `RemediationPlannerResult`（聚合 `list[RemediationPlan]` + planner loop 的 `terminal_status` **直传**——单 loop 无需 reconcile）。
- 系统提示 markdown（`agent/prompts/remediation_planner_system.md`）：教模型评 `risk_level`（`rm -rf` / `kill -9` / 改 systemd → `high`）+ `RemediationStep` **全部三条不变量**：`high ⟹ precheck`、**`非 high ⟹ 必须给 rollback_cmd`**、命令字段非空白（否则模型每次 emit 被弹）。

**关键不变量**：`propose_remediation` 的 handler **只 hit-check 标签 + 记录拟议、不执行**（side_effects="none"）；P1b **不引入任何写工具**，agent 表面保持永久只读（M9 不变量 1）。harvest 出的 `RemediationPlan` 是**数据**，执行属 P2。

## 功能 (Capabilities)

### 新增功能
- `remediation-planner-agent`: 第三个 `AgentLoop`-backed Agent + `propose_remediation` 结构化输出通道 + 编排层 harvest/stamp/控制流。定义「给定 finding → 产出经校验的 `RemediationPlan`（不执行）」的 Agent 行为契约。

### 修改功能
<!-- 无。复用 request_more_inspection / list_inspectors（零改动）；不改 P1a remediation-plan-schema（只消费）；不改 agent-loop / agent-tool-adapter 契约。 -->

## 影响

- **新增代码**：`agent/remediation_planner.py`、`tools/schemas/propose_remediation.py` + handler、`agent/prompts/remediation_planner_system.md`、对应测试 + LLM cassette。
- **对外契约影响**：新增 **1 个 Agent tool schema** `propose_remediation`（仅 `"agent"` surface，**不进 MCP tool schema**、不进 CLI）。不改 Inspector schema / Notifier Protocol / Schedule manifest / 既有 CLI 命令 / P1a remediation-plan-schema。`ToolContext` 零改动（planner 复用既有只读依赖）。
- **依赖**：无新增第三方依赖。
- **上游依赖**：P1a `remediation-plan-schema`（已归档冻结）。**下游解锁**：P2（`add-remediation-execution-workflow`，消费 harvest 出的 `RemediationPlan`）。

## 架构不变量对齐（M9）

1. **Agent 表面永久只读** —— `propose_remediation` `side_effects="none"`、无执行；P1b 不引入任何 write/destructive 工具，`tools_adapter` 的 M2 gate 不放开。
2. **Remediation 自成子系统，不进 Tool Registry 作为执行能力** —— `propose_remediation` 是 Agent 的「输出通道」（如 `correlate_findings`），产出数据而非触发执行；harvest 出的 `RemediationPlan` 不被任何 surface 投影成可执行工具。
3. **审批门与 ToolContext 分离** —— P1b 不碰 `ApprovalService`（审批属 P2）；`ToolContext` 零改动。
4. **不引入受限写 API / 不加新 Capability** —— planner 只调只读工具，不调 `target.exec`。

## Non-Goals（非目标）

- ❌ **不执行任何 step** —— Executor / `target.exec` / dry-run / rollback 全属 P2。`propose_remediation` 只记录拟议。
- ❌ **不审批、不接飞书卡片、不写 audit** —— 分属 P2 / P3。
- ❌ **不自动触发修复** —— P1b 只在被显式调用时为 finding 拟方案；「哪些 finding 自动拟方案」的策略最小化（本提案默认：编排层对传入的 finding 逐个拟，不做严重度筛选启发式），筛选策略留 P2/后续。
- ❌ **不在 schema 层自动推断 risk_level** —— risk 评定由 planner **prompt** 引导（P1a 已划此为 Non-Goal）；schema 只校验「high ⟹ precheck」结构不变量。
- ❌ **不改 P1a `remediation-plan-schema`** —— 只消费冻结契约；若实现中发现缺字段，走「修改 remediation-plan-schema」增量 spec，不在本提案私自扩。
- ❌ **不让 planner 调写工具或 LLM-in-execution** —— planner 的 LLM 调用只产出方案数据，不进任何执行路径。

## Prompt Caching 策略与 Token 影响

镜像 Diagnostician（spec `diagnostician-agent` §需求:findings 进 messages 不进 system）：

- **系统提示 + `RemediationStep` schema 描述**（固定）→ `cache_control: ephemeral` 缓存。系统提示 markdown 跨 run **字节稳定**，命中 prompt cache。
- **finding + 根因假设 + target**（每 run 变）→ 进**首条 user message**，绝不进 system（否则破坏 system 的 byte-stability、cache 永不命中）。
- **capability 检查**：`backend.capabilities.prompt_caching=False` 时，loop 端不注入 `cache_control`（既有 `AgentLoop` 行为，复用）。
- **Token 影响**：单 finding 拟方案约 1 次 LLM 往返（可能 + 1-2 次只读复核 inspector 调用）。系统提示缓存后，多 finding 批量拟方案的增量 token ≈ 每 finding 的 messages + 输出，cache hit 显著降本。

## Failure Modes

1. **模型产出违反 P1a 不变量的方案**（high 缺 precheck、非 high 却 rollback=None、命令空白）→ `ToolsAdapter.dispatch` 按 `RemediationStep` `model_validate` 校验**全部三条不变量**，emit 即拒（adapter `→ TypeError` → loop catch → 脱敏 envelope），结构化回传模型自纠。
2. **悬空 finding 标签**（模型引用不存在的 `F9`）→ **handler hit-check 阶段**抛 `ToolError`、结构化回传模型自纠（镜像 `correlate_findings` handler hit-check；悬空永不抵达 harvest，harvest 对悬空 fail-loud 仅为防御性）。
3. **模型不产出任何方案**（end_turn 无 `propose_remediation` 调用）→ `RemediationPlannerResult` 返回空 plan 列表 + 相应 status，编排层据此决定（不视为崩溃）。
4. **LLM backend 超时 / 重试耗尽** → 复用 `AgentLoop` 的降级终态（`degraded_*`），status 反映，已 harvest 的方案保留。
5. **复核 inspector 调用失败** → 复用 `request_more_inspection` 的 status 暴露机制；planner 可据失败 status 调整或保守拟方案。

## Operational Limits

- **并发预算**：单 planner loop 顺序跑（复用 `AgentLoop`）；多 finding 批量拟方案的并发由编排层控制，默认不并发（避免 token 突发）。
- **内存预算**：方案是小对象（数个 step）；harvest 累积的 `list[RemediationPlan]` 与 finding 数同阶，可忽略。
- **超时设置**：复用 `AgentLoop` 的 per-call timeout（既有 60s）+ token 预算；复核 inspector 调用复用 `InspectorRunner` 超时。

## Security & Secrets

- **新密钥**：无。
- **脱敏**：拟议方案的 `forward_cmd` 等可能含敏感命令，但 P1b 只产出**数据**、不渲染外发（脱敏属 P2 audit / P3 卡片）。planner 的 LLM 调用经既有 backend，cassette 录制需走既有脱敏（复用）。
- **攻击面**：**不扩大** —— planner 只调只读工具 + 产出数据，无任何执行路径；`propose_remediation` handler 不 eval 命令、不碰 target。真正的写攻击面由 P2 引入并以审批 + 非 root + audit 防护。

## Cost / Quota Impact

- **Token 消耗**：每 finding 约 1 次主 LLM 往返 + 0-2 次只读复核；系统提示 + schema 缓存后增量主要是 messages + 输出。
- **API 调用频次**：与待拟方案的 finding 数同阶；批量默认顺序、不并发突发。
- **对 Anthropic 配额影响**：低；测试走 cassette replay，CI 零真实 API 调用。

## Demo Path

5 分钟内本地 reproduce（无 SSH、无付费 API，cassette replay 优先）：

```bash
pip install -e ".[dev]"
pytest tests/agent/test_remediation_planner.py -v   # cassette 回放，全绿
# 离线 demo：喂一个 /var/log 占满的 finding → planner 产出 RemediationPlan → 打印（不执行）
# 经既有 hostlens demo 子命令接线（与 Diagnostician demo 同款 cassette 路径，实现期接），不引入 agent 模块级 __main__
```

预期：打印一个 `RemediationPlan`（finding 绑定、含 `precheck → forward → verify` 三元组的有序 step、risk_level 已评、估时），**全程无任何命令被执行**——证明「Agent 会拟方案但只读」。该 demo 的录像即 P1↔P2 门控证据。
