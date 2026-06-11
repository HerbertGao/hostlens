## 1. propose_remediation 结构化输出通道

- [x] 1.1 新建 `src/hostlens/tools/schemas/propose_remediation.py`：`ProposeRemediationInput`（`extra="forbid"`/`frozen`：`finding_label: str(min_length=1)` / `rationale: str` / `estimated_duration_seconds: StrictInt(ge=0)` / `steps: list[RemediationStep](min_length=1)`，复用 P1a `RemediationStep`）+ `ProposeRemediationOutput`（不含真 id 的 ack）
- [x] 1.2 实现 `propose_remediation` handler：**closure-bind per-run `FindingStore`，对 `finding_label` hit-check**（悬空 → `ToolError` 自纠，镜像 `correlate_findings_handler`），命中回传 ack；**不调 `target.exec`、不执行任何命令、不 resolve 真 id**
- [x] 1.3 声明 ToolSpec：`side_effects="none"`、`surfaces={"agent"}`（禁 mcp/cli）、`requires_approval=False`、`sensitive_output` 显式声明
- [x] 1.4 新增专用装配函数 `register_remediation_planner_tools(registry, *, finding_store, target_name, ...)`（镜像 `register_diagnostician_tools` 成员，换 `correlate_findings`→`propose_remediation`）：装配 `request_more_inspection` + `list_inspectors` + `propose_remediation`，closure-bind store+target，**排除 `correlate_findings` 与 `run_inspector`**（后者要 target_name、破 D-3）；**不进 `register_default_tools`**；给 `request_more_inspection` 一个 planner description 变体（句尾 `correlate_findings` 提及替换为 `propose_remediation`，避免泄漏 planner 没有的工具名）

## 2. RemediationPlannerAgent

- [x] 2.1 新建 `src/hostlens/agent/remediation_planner.py`：`RemediationPlannerAgent`（复用 `AgentLoop`，外部系统提示 + 构造期 fail-fast 抛 `ConfigError`，镜像 `DiagnosticianAgent`）
- [x] 2.2 新建系统提示 `src/hostlens/agent/prompts/remediation_planner_system.md`：教 risk_level 评定（`rm -rf`/`kill -9`/改 systemd → high）+ **`RemediationStep` 全部三条不变量**：`high ⟹ precheck`（抗 TOCTOU）、**`非 high ⟹ 必须给 rollback_cmd`**（只有 high 可省 rollback）、命令字段非空白；固定内容、byte-stable
- [x] 2.3 finding / 根因假设 / target 名进首条 user message（**禁进 system**，保 prompt cache byte-stable）；序号标签 `F1`/`F2` 列在 message
- [x] 2.4 planner 工具集经 `register_remediation_planner_tools` 装配，恰为 `request_more_inspection` + `list_inspectors` + `propose_remediation`；确认全只读（`side_effects ∈ {none,read}`）、无 write/destructive、无 requires_approval、**不含 `correlate_findings`、不含 `run_inspector`**
- [x] 2.5 planner 持的 `FindingStore` seed 自诊断阶段 canonical findings（与首条 message 标签同源；标签 ↔ handler hit-check ↔ harvest resolve 三者读同一 store）

## 3. 编排层 harvest / 控制流

- [x] 3.1 `harvest_plans(loop, finding_store, target_name)`：遍历**成功** `propose_remediation` 调用 → resolve `finding_label` → 真 `Finding.id`、盖 `target_name`、构造 P1a `RemediationPlan`；对悬空标签 **fail-loud（防御性 raise，理论不可达——handler 已 hit-check）**，**不**记录无效跳过（镜像 `harvest_hypotheses`）；多标签 resolve 同真 id 不去重
- [x] 3.2 `run_remediation_planning(...)`：控制流缝——**仅当诊断成功（status==ok）且 findings 非空才启动**（镜像 `run_diagnosis`，不引入 severity 启发式筛选）；零 wire / 零 ToolContext 改动
- [x] 3.3 `RemediationPlannerResult`：聚合 `list[RemediationPlan]`（可空）+ planner loop `terminal_status` **直传**（单 loop，**不引入 `reconcile_status`**）；降级终态保留已 harvest 方案；无方案不视为崩溃

## 4. 测试（含 LLM cassette）

- [x] 4.1 新建 `tests/agent/test_remediation_planner.py` + `__init__.py`（若需）+ cassette：喂 finding+假设 → planner 调 `propose_remediation` → harvest 出 `RemediationPlan`（绑定真 id、盖 target）
- [x] 4.2 emit 违反不变量：(a) high 缺 precheck → 拒（含 `high_requires_precheck`）；(b) 非 high 却 rollback=None → 拒（含 `rollback_none_requires_high`）；均结构化回传、loop 不崩
- [x] 4.3 悬空标签 `F9` → **handler hit-check 阶段**抛 `ToolError`、结构化回传自纠（不抵达 harvest）；harvest 对悬空 fail-loud（防御性，单测直接喂悬空验 raise）
- [x] 4.4 系统提示跨两组输入 byte-stable；`prompt_caching=False` 时不注入 cache_control
- [x] 4.5 `propose_remediation` 不上 MCP（用 MCP adapter 投影确认不在列表）；planner 工具集全只读 + 不含 `correlate_findings` 断言
- [x] 4.6 降级终态保留已 harvest 方案；无 `propose_remediation` 调用 → 空方案列表 + 正常 status；多标签 resolve 同真 id 产多份同 finding_id plan 不去重
- [x] 4.7 handler 不执行：断言 dispatch `propose_remediation` 不触发任何 target.exec / 命令执行

## 5. 收尾验收

- [x] 5.1 `mypy --strict` 0 错误（新增模块）
- [x] 5.2 `ruff` 通过
- [x] 5.3 `pytest tests/agent/test_remediation_planner.py -v` 全绿（cassette 回放，CI 零真实 API）
- [x] 5.4 离线 demo（P1↔P2 门控证据）：demo 语义由端到端测试 `test_offline_demo_var_log_full_to_plan_no_execution` 覆盖（喂 /var/log 占满 finding → `run_remediation_planning` → `RemediationPlan`、全程零命令执行，可 `model_dump_json` 打印）。**注**：「经 `hostlens demo` 子命令」的 CLI 接线**经决定留 follow-up**——demo 走 cassette 回放架构（`PlaybackBackend` + `misses` 守卫），接 planner 需录一条 planner cassette（真 API）+ 扩 `run_diagnosis_pipeline`/render，超出本提案 planner 模块范围且无 spec 要求；planner 功能本身已完整（19 测试 + 全门绿）。follow-up：`wire-remediation-planner-into-demo-pipeline`
- [x] 5.5 `openspec-cn validate add-remediation-planner --strict` 通过
