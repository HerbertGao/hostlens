## 重命名需求

- FROM: `### 需求:ApprovalGate 必须交互确认或 --yes，high-risk 强制人眼在场，且与 ToolContext 分离`
- TO: `### 需求:ApprovalGate 必须交互确认或 --yes，且与 ToolContext 分离`

## 修改需求

### 需求:ApprovalGate 必须交互确认或 --yes，且与 ToolContext 分离

系统必须提供独立 `ApprovalGate`（位于 `remediation/`，给 Executor/CLI 用），与 `ToolContext.ApprovalService` **严格分离**——后者永久保持 `NoopApprovalService`（agent-surface handler 永不触发审批）。

**`ApprovalGate` 只会授权全 low 风险 plan**：含 `risk_level ∈ {"medium","high"}` step 的 plan 在到达审批门之前已被 `hostlens fix` 分流到 runbook（见「hostlens fix」需求），永不进入审批门。审批规则——

- 交互（TTY）：逐 plan 展示后 `y/N` 确认；给 `--yes` 时跳过 `y/N` 直接执行。
- 非交互（无 TTY）：缺 `--yes` 必须**退出 1**（绝不默默执行）；给 `--yes` 视为已通过 `y/N`。

**high-risk 双确认已移除**：high-risk 的安全保证由原「审批门内双确认后执行」**升级为更强的「根本不代执行」**——含 high-risk（及 medium）step 的 plan 走 runbook 路径由人工执行，审批门不再见到任何 high-risk plan，故不再有第二道确认短语逻辑（移除即避免死代码：在分流之后该分支永不可达）。

#### 场景:非交互缺 --yes 退出 1
- **当** 无 TTY 且未给 `--yes` 调用 `hostlens fix`（非 dry-run，且 plan 为全 low）
- **那么** 立即退出码 1，**不执行任何命令**

#### 场景:--yes 跳过 y/N 直接执行全 low plan
- **当** 给 `--yes` 执行一个全 low plan
- **那么** 跳过 `y/N` 直接进入执行（low 风险无第二道确认）

#### 场景:ApprovalGate 永不见 high-risk plan
- **当** 一个含 `risk_level=="high"` 或 `"medium"` step 的 plan 进入 `hostlens fix`
- **那么** 该 plan 在到达 `ApprovalGate` 之前已被分流到 runbook；审批门收到的 plan 必为全 low，不存在 high-risk 双确认路径

#### 场景:ApprovalGate 不污染 ToolContext
- **当** 检视 `ToolContext.approval_service`
- **那么** 它仍是 `NoopApprovalService`（拒绝一切），真审批只在 `remediation/` 的 `ApprovalGate`

### 需求:hostlens fix 必须默认 dry-run、拒绝 root、解析 target、稳健处理输入错误

系统必须提供 `hostlens fix <plan-file>` CLI，编排顺序为：**① EUID==0 拒绝（最早门）→** ② `RemediationPlan.load_json` 加载 → **③ 风险分级分流**：`has_elevated = any(step.risk_level in {"medium","high"} for step in plan.steps)`；若 `has_elevated` 为真 → 渲染 runbook（`remediation-runbook` capability）到 stdout/`--out`、以退出码 **4** 结束，且**不解析 target、不 preview、不进 `ApprovalGate`、不执行、不写 audit** → ④（仅全 low plan 继续）`load_targets_config` + `build_registry_from_config` 解析 `plan.target_name` 为活体 `ExecutionTarget` → ⑤ 展示三元组 diff → ⑥ `ApprovalGate` → ⑦ 执行。

**风险分级分流（③）的语义**：plan 级风险取其 steps 的 max——任一 step 为 `medium`/`high` 即整 plan 走 runbook（**只出方案、AI 不代执行**）；理由是 step 间有顺序/回滚依赖，无法只执行 low step 而跳过 elevated step。分流发生在 target 解析与 preview **之前**，故 runbook 路径零执行、零 audit、不需要活体 target。

**EUID==0 拒绝必须是最早的门**：`os.geteuid()==0` → 在 **load_json / 风险分流 / 任何 plan 内容渲染（含 runbook）之前**立即拒绝退出（含 dry-run 入口）。理由：plan 命令（无论走执行还是 runbook）可能含 `redact_text` 漏脱的 flag 形密钥，root 拒绝若发生在任何 plan 内容渲染之后会先把内容打到 stdout/stderr 泄漏。

命令必须**默认安全（无执行信号即不改远端状态）**：对全 low plan，无 `--yes`、无交互确认时绝不执行——preview 展示每 step 的 `precheck/forward/rollback/verify` 三元组（best-effort 脱敏）**及 `estimated_duration_seconds`**（展示参考，不做硬超时）后，由 `ApprovalGate` 决定（非交互缺 `--yes` → 退 1；TTY → y/N 提示，默认不执行）。**flag 模型**：无独立「执行标志」——`--yes`（或 TTY 交互确认）即执行信号；`--dry-run` flag（默认 off）强制只预览、覆盖 `--yes`/交互确认。注：「默认 dry-run」指**默认行为安全**（preview + 无信号不执行，由 ApprovalGate 强制），而非 `--dry-run` flag 默认开。**`--dry-run` 仅作用于全 low plan 的执行路径**；medium/high plan 无论是否 `--dry-run` 都只渲染 runbook（本就零执行），`--dry-run` 对其为 no-op。

**输入错误必须稳健、绝不 traceback**（沿用项目 CLI 惯例）：
- plan 文件不存在 / 不可读 / 空 → 单行 stderr + 退出码 2。
- `load_json` 抛**任意**异常（`json.JSONDecodeError` malformed / `pydantic.ValidationError` schema 违反 / `ValueError` 重复键）→ 单行 stderr + 退出码 2。
- `plan.target_name` 未在 `targets.yaml` 注册 / `targets.yaml` 损坏或不可读 → 退出码 **3**（与既有 CLI「config/target 错误=3」一致）。**捕获契约必须覆盖全部 target 解析异常**：`build_registry_from_config(load_targets_config(...))` 对 schema 损坏的 targets.yaml 抛 **`pydantic.ValidationError`**，YAML 语法 / env 占位 / docker host scheme 错抛 **`ConfigError`**，`path.read_text()` 对不可读 / 目录形 targets.yaml 抛 **`OSError`**，`registry.get(name)` 未命中抛 **`KeyError`**——故 CLI 必须 `except (KeyError, TargetError, ConfigError, ValidationError, OSError)` 全部捕获映射为退出码 3、**无 traceback**。注：target 解析仅在全 low plan 路径（④）发生，medium/high plan 在 ③ 已退出，不触发 target 解析错误。

退出码契约（沿用项目 `3 > 2 > 1 > 0` 优先级，新增 **4**）：0 成功（全 low plan 执行成功）；**4** plan 含 `medium`/`high` step → 已渲染 runbook、**未执行**（策略性未执行，**非错误**，但取非 0 以便脚本 / Agent 机械区分「需人工接手」与「已执行」；在编排 ③ 退出，先于 1/2/3 的执行路径判定，但晚于 ① root 拒（1）与 ② load 失败（2））；**1** 非 TTY 无 --yes / 用户拒批 / 执行失败（含回滚不完整）——这几类共享 1，但 **stderr 必须带机器可解析前缀区分**（`approval-rejected:` 安全门拒 vs `execution-failed:` 执行失败）；**2** 非法 plan（schema/重复键/malformed/文件 IO）；**3** 配置 / target 解析错误。

#### 场景:medium plan 渲染 runbook 不执行
- **当** `hostlens fix` 加载一个含至少一个 `risk_level=="medium"` step 的 plan
- **那么** 渲染 runbook 到 stdout、退出码 4，**不解析 target、不 preview、不进 ApprovalGate、不执行任何命令、不写 audit.log**

#### 场景:high plan 渲染 runbook 不走双确认执行
- **当** `hostlens fix` 加载一个含 `risk_level=="high"` step 的 plan（即便给 `--yes`）
- **那么** 渲染 runbook、退出码 4，**不再有任何执行路径或双确认短语**（high-risk 由「不代执行」取代旧的「双确认后执行」）

#### 场景:medium/high plan 渲染前仍先拒 root
- **当** 以 `EUID==0` 运行 `hostlens fix` 一个 medium/high plan
- **那么** 在 load_json / runbook 渲染 / 打印任何 plan 命令之前拒绝并非零退出（退出码 1，最早安全门）；stdout/stderr **不含**任何 plan step 命令内容

#### 场景:全 low plan 不受分级影响仍走审批执行
- **当** `hostlens fix --yes` 一个全 low plan
- **那么** 正常经 target 解析 → preview → ApprovalGate → 执行 → 两段式 audit（现有 low 执行闭环不变，退出码 0 成功）

#### 场景:默认 dry-run 不执行
- **当** 显式 `--dry-run` 或 TTY 下未确认的 `hostlens fix <全 low plan>`
- **那么** 展示 plan 三元组 diff，**不执行任何命令、不改状态、不写 audit.log**

#### 场景:--dry-run 覆盖 --yes（flag 优先级）
- **当** 对全 low plan 同时给出 `--dry-run` 与 `--yes`
- **那么** **dry-run 优先**：只预览、零执行、不写 audit.log（`--yes` 被抑制）

#### 场景:拒绝 root 且不泄漏 plan 内容
- **当** 以 `EUID==0` 运行 `hostlens fix`（含 dry-run，任意风险级 plan）
- **那么** 在 load_json / 打印任何 plan step 命令（含 runbook 渲染）之前拒绝并以非零退出（退出码 1，最早安全门）；stdout/stderr **不含**任何 plan step 命令内容

#### 场景:非法 plan 文件被拒不 traceback
- **当** `hostlens fix` 加载一个含重复 JSON 键 / malformed JSON / 违反 P1a schema 的 plan 文件，或文件不存在/不可读
- **那么** 单行 stderr 错误、退出码 2，**无 traceback**，不进入分流/执行

#### 场景:target 未注册或 targets.yaml 损坏/不可读退出码 3
- **当** 全 low plan 的 `plan.target_name` 不在 `targets.yaml`（`KeyError`），或 schema 损坏（`pydantic.ValidationError`），或 YAML/env/docker-host 配置错（`ConfigError`），或文件不可读/是目录（`OSError`）
- **那么** CLI 捕获 `(KeyError, TargetError, ConfigError, ValidationError, OSError)`、单行 stderr、退出码 3、**无 traceback**，不进入执行
