## 1. runbook 渲染能力（remediation-runbook capability）

- [x] 1.1 新增 `src/hostlens/remediation/templates/runbook.md.j2`：顶部「未执行」横幅 + plan 元信息（finding_id/target_name/rationale）+ 逐 step 四段（precheck/forward/verify/rollback）可复制命令块 + 每 step risk_level 标注；`None` 命令显式标「无」
- [x] 1.2 新增 `src/hostlens/remediation/runbook.py`：`render_runbook(plan: RemediationPlan) -> str`，确定性 Jinja2 渲染；命令经 `core/redact.py` `redact_text` 脱敏后再注入模板；无 LLM / 无 IO
- [x] 1.3 渲染失败 fail-closed：模板/字段异常 → 抛结构化错误（由 CLI 映射非零退出），绝不回退到执行

## 2. 风险分级分流（修改 hostlens fix 编排）

- [x] 2.1 在 `cli/fix.py` 编排中、`load_json` 之后插入分类步骤：`has_elevated = any(s.risk_level in {"medium","high"} for s in plan.steps)`
- [x] 2.2 `has_elevated` 为真 → 调 `render_runbook` 输出到 stdout（保留 `--out <file>` 落盘判断，依 design Open Question 决定是否纳入本次）→ 退出码 4；**不解析 target、不 preview、不进 ApprovalGate、不执行、不写 audit**
- [x] 2.3 确认 EUID==0 拒绝仍是最早门——在 load_json / 风险分流 / runbook 渲染（含打印任何 plan 命令）之前；medium/high plan 以 root 运行同样先拒退 1
- [x] 2.4 全 low plan 路径保持不变：target 解析 → preview → ApprovalGate → 执行 → 两段 audit
- [x] 2.5 退出码体系新增 4（runbook 已渲染未执行，策略性非错误）；与现有 0/1/2/3 优先级共存，扫 `cli/__init__.py` 确认 4 无冲突

## 3. ApprovalGate 移除 high-risk 双确认

- [x] 3.1 `remediation/approval.py`：移除 `high_risk` 分支与确认短语逻辑（分流后该分支永不可达）；保留非交互缺 `--yes` 退 1、`--yes` 跳过 y/N、ToolContext 分离
- [x] 3.2 `ApprovalRejected` 的 `high_risk_non_interactive` / `phrase_mismatch` reason 随之清理（确认无其他引用）

## 4. 测试

- [x] 4.1 新增 `tests/remediation/test_runbook.py`：四段命令渲染 / 横幅存在 / 脱敏 / 确定性（重复渲染逐字一致）/ 渲染零执行零 audit 不推通道 / 渲染失败 fail-closed
- [x] 4.2 改写 `tests/cli/test_fix.py`：medium plan → runbook+exit4 不执行 / high plan → runbook+exit4 不双确认 / medium/high 以 root 先拒 / 全 low plan 仍走审批执行（退出码 0）/ `--dry-run` 对 medium/high 为 no-op
- [x] 4.3 改写/删除 P2 中「high-risk 交互双确认后执行」「非交互+--yes+high-risk 被拒」「交互+--yes+high-risk 仍走确认」相关用例（这些路径已由 runbook 取代）
- [x] 4.4 新增/调整 `tests/remediation/test_approval.py`：审批门只见全 low plan；high-risk 双确认逻辑已不存在
- [x] 4.5 准备 demo fixtures：`tests/fixtures/remediation/{low,medium,high}-plan.json`

## 5. 文档与门禁

- [x] 5.1 `mypy --strict` + `ruff` 全绿；runbook 模块类型完整
- [x] 5.2 跑 Demo Path（proposal）：low → 审批/执行；medium/high → runbook+exit4；测试全绿
- [x] 5.3 复核：runbook 路径不 import `Executor`/`CommandRunner`、不调 `ExecutionTarget.exec`、不写 audit、不经 Notifier（红线对齐）
