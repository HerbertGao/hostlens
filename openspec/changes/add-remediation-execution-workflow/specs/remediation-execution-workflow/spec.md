## 新增需求

### 需求:Executor 必须确定性顺序执行 precheck-forward-verify，自成子系统

系统必须提供 `Executor`：接受一个经校验的 `RemediationPlan` 与一个**已解析的活体** `ExecutionTarget`，**严格按 `steps` 顺序**逐个执行，每步依次跑 `precheck_cmd`（若非 None）、`forward_cmd`、`verify_cmd`。Executor 必须是**确定性**的（不含 LLM、不含随机），**不持 `LLMBackend`、不进 Tool Registry、不进 Agent loop、不被任何 surface adapter 投影**——它是 CLI 触发的写子系统（类比 Notifier）。所有命令经 `ExecutionTarget.exec`（既有 `Capability.SHELL`）执行，禁止引入新 `Capability` 或受限写 API。

**一个阶段（precheck/forward/verify/rollback）成功推进当且仅当其 `ExecResult.exit_code == 0`**。成功**判定分支只看 `exit_code == 0`**（充要条件，非「非 0 即失败」）：`exit_code != 0`、`exit_code is None`（无 OS 退出码——超时或远端断连，`ExecResult` 契约 base.py 允许，`None != 0` 故自动未成功）一律判未成功，fail-safe 保守回滚。`timed_out` 字段**不参与成功判定**（由 `exit_code is None` 已覆盖），仅用于 audit **记录超时原因**（区分「超时」与「断连」——`ExecResult` 不变量 `timed_out is True ⇒ exit_code is None`；`exit_code` 可为 `128+signum` 信号杀，故超时绝不用 exit_code 判）。

`ExecutionTarget.exec` 在传输层失败（auth/connection/SFTP）时**抛异常**（`TargetError`，非返回 `ExecResult`，base.py 契约）。任一阶段 `exec` 抛异常必须被 Executor 捕获、视同该阶段「未成功推进」（触发对应 rollback/中止流程）、记入 audit，**绝不让 traceback 冒泡给用户**（沿用项目 CLI 惯例）。

#### 场景:全部 step 成功顺序执行
- **当** 对一个所有 step 的 precheck/forward/verify 都返回 `exit_code==0` 的 plan 执行
- **那么** 所有 step 按 `steps` 顺序执行、整体成功，无 rollback 触发

#### 场景:exit_code 为 None（断连）判未成功
- **当** 某步 `forward_cmd` 返回 `exit_code is None`（远端断连、非超时）
- **那么** 判该步未成功、倒序 rollback，audit 记 `forward-failed` 且其 `exit_code` 字段为 `null`（消费方须容忍 null）

#### 场景:exec 抛 TargetError 被捕获不冒 traceback
- **当** 某步 `forward_cmd` 的 `exec` 抛 `TargetError`（传输层失败）
- **那么** Executor 捕获、视同该步未成功、倒序 rollback、audit 记 `forward-failed`（含传输错误摘要）、以非零退出，**无 traceback**

#### 场景:Executor 不依赖 LLM / 不进 Registry
- **当** 检视 Executor 的依赖
- **那么** 它不持 `LLMBackend`、不在任何 `ToolRegistry`/surface adapter 中、仅依赖 `RemediationPlan` + `ExecutionTarget`

### 需求:precheck 失败必须中止该步并倒序回滚，不执行其 forward

系统必须在每步执行 `forward_cmd` 之前先跑 `precheck_cmd`（若非 `None`）。`precheck` 未成功推进（`exit_code != 0` / `None` / `timed_out` / `exec` 抛异常）表示**前提已漂移**（抗审批延迟 TOCTOU）：必须**中止整个 plan**、**不执行该步的 `forward_cmd`**、对此前已成功的 step 倒序回滚，并在 audit 记该步为 `precheck-blocked`。注：P1a 保证 `high-risk` step 必有非 None 的 `precheck_cmd`（`high_requires_precheck` 不变量），故**最危险的 step 恒有前提守卫**；`low`/`medium` 的 precheck 可选。

#### 场景:precheck 失败中止且不碰 forward
- **当** 第 k 步的 `precheck_cmd` 返回非 0
- **那么** 该步的 `forward_cmd` **不被执行**，plan 中止，第 1..k-1 步倒序回滚，audit 记第 k 步 `precheck-blocked`

### 需求:任一步未成功推进必须倒序回滚已成功的 step

**定义「step 已成功推进」= 该步 `precheck`（若非 None）、`forward`、`verify` 三阶段均 `exit_code==0`**。系统必须在任一步未成功推进（precheck 拒 / forward 未成功 / verify 未成功 / 该阶段 exec 抛异常）时，对**此前已成功推进**的 step **倒序**遍历、执行其 `rollback_cmd`。回滚覆盖集 = 「失败 step 之前三阶段全绿的 step」**加上当前 step（当且仅当其 `forward` 成功但 `verify` 未成功——`forward` 已改状态故须回滚）**。语义边界用「`forward` 是否已改状态」判定，而非笼统「已成功推进」：`verify` 失败 → 本步 forward 已改状态、**含本步**回滚；`forward` 失败 → 本步 forward 未完成、**不回滚本步**（回滚更早的全绿 step）。`rollback_cmd` 为 `None`（P1a 仅 `high-risk` step 允许；P1a 同时保证 high-risk 必有 precheck）时该 step 记 `rollback-unavailable` 并**继续**倒序其余（best-effort）。每个 rollback 的成败（含 rollback 的 exec 抛异常）必须写入 audit；某个 rollback 失败时**继续**倒序其余、不中断。

#### 场景:forward 失败触发倒序回滚（不含本步）
- **当** 第 3 步 `forward_cmd` 未成功（前两步已成功）
- **那么** 对第 2、第 1 步（倒序）执行 `rollback_cmd`，**不**回滚第 3 步（其 forward 未完成），audit 记第 3 步 `forward-failed` 及各 rollback 结果

#### 场景:verify 失败触发回滚（含本步）
- **当** 第 2 步 forward 成功但 `verify_cmd` 未成功
- **那么** 倒序回滚已成功 step（**含第 2 步自身的 rollback**，因其 forward 已改状态），audit 记 `verify-failed`

#### 场景:不可回滚的 step 记 rollback-unavailable 不中断
- **当** 倒序回滚时遇到一个 `rollback_cmd is None` 的 high-risk step
- **那么** 该 step 记 `rollback-unavailable`，回滚流程**继续**处理其余 step、不抛出中断

#### 场景:单个 rollback 失败不阻断其余回滚
- **当** 倒序回滚某 step 的 `rollback_cmd` 未成功（含 exec 抛异常）
- **那么** audit 记该 rollback 失败、回滚**继续**处理更早的 step，CLI 最终退出码反映「执行失败且回滚不完整」

### 需求:ApprovalGate 必须交互确认或 --yes，high-risk 强制人眼在场，且与 ToolContext 分离

系统必须提供独立 `ApprovalGate`（位于 `remediation/`，给 Executor/CLI 用），与 `ToolContext.ApprovalService` **严格分离**——后者永久保持 `NoopApprovalService`（agent-surface handler 永不触发审批）。审批规则——**`--yes` 只覆盖普通 `y/N`，永不覆盖 high-risk 的第二道确认短语**（high-risk 双确认是不可被 `--yes` 绕过的门，交互/非交互对称）：
- 交互（TTY）：逐 plan 展示后 `y/N` 确认；plan 含 `risk_level=="high"` step 时必须**双重确认**（第二次须输入确认短语，如 plan 的 `finding_id`，防手滑）。**给 `--yes` 时**：普通 plan 跳过 `y/N` 直接执行；但含 high-risk step 的 plan **仍必须走第二道确认短语**（`--yes` 不跳过 high-risk 双确认）。
- 非交互（无 TTY）：缺 `--yes` 必须**退出 1**（绝不默默执行）；给 `--yes` 视为已通过普通 `y/N`，**但 `--yes` 不足以批含 `high-risk` step 的 plan**——含 high-risk 的 plan 在非交互下**即便有 `--yes` 也必须拒绝执行（退出 1，无法输入确认短语）**，强制最危险类操作必须人眼在场。

#### 场景:非交互缺 --yes 退出 1
- **当** 无 TTY 且未给 `--yes` 调用 `hostlens fix`（非 dry-run）
- **那么** 立即退出码 1，**不执行任何命令**

#### 场景:非交互 + --yes + high-risk plan 被拒
- **当** 无 TTY 下带 `--yes` 执行一个含 `risk_level=="high"` step 的 plan
- **那么** 拒绝执行、退出 1（`--yes` 不绕过 high-risk 的人眼双重确认门）

#### 场景:交互 + --yes + high-risk 仍走第二道确认
- **当** TTY 下带 `--yes` 执行一个含 `risk_level=="high"` step 的 plan
- **那么** `--yes` 跳过第一道 `y/N`，但**仍必须**走第二道确认短语，短语正确才执行（`--yes` 永不覆盖 high-risk 双确认）

#### 场景:high-risk plan 交互双重确认
- **当** 交互模式（不带 --yes）审批一个含 high-risk step 的 plan
- **那么** 第一次 `y/N` 后还需第二次输入确认短语，二者都通过才执行

#### 场景:ApprovalGate 不污染 ToolContext
- **当** 检视 `ToolContext.approval_service`
- **那么** 它仍是 `NoopApprovalService`（拒绝一切），真审批只在 `remediation/` 的 `ApprovalGate`

### 需求:hostlens fix 必须默认 dry-run、拒绝 root、解析 target、稳健处理输入错误

系统必须提供 `hostlens fix <plan-file>` CLI，编排顺序为：**① EUID==0 拒绝（最早门）→** ② `RemediationPlan.load_json` 加载 → ③ `load_targets_config` + `build_registry_from_config` 解析 `plan.target_name` 为活体 `ExecutionTarget` → ④ 展示三元组 diff → ⑤ `ApprovalGate` → ⑥ 执行。

**EUID==0 拒绝必须是最早的门**：`os.geteuid()==0` → 在 **load_json / target 解析 / 任何 plan 内容渲染之前**立即拒绝退出（含 dry-run 入口）。理由：plan 命令可能含 `redact_text` 漏脱的 flag 形密钥（见 Security），root 拒绝若发生在 preview 之后会先把 plan 内容打到 stdout/stderr 泄漏。

命令必须**默认安全（无执行信号即不改远端状态）**：无 `--yes`、无交互确认时绝不执行——preview 展示每 step 的 `precheck/forward/rollback/verify` 三元组（best-effort 脱敏）**及 `estimated_duration_seconds`**（展示参考，不做硬超时）后，由 `ApprovalGate` 决定（非交互缺 `--yes` → 退 1；TTY → y/N 提示，默认不执行）。**flag 模型**：无独立「执行标志」——`--yes`（或 TTY 交互确认）即执行信号；`--dry-run` flag（默认 off）强制只预览、覆盖 `--yes`/交互确认。注：「默认 dry-run」指**默认行为安全**（preview + 无信号不执行，由 ApprovalGate 强制），而非 `--dry-run` flag 默认开——纯预览（连交互确认都不给）须显式 `--dry-run`。

**输入错误必须稳健、绝不 traceback**（沿用项目 CLI 惯例）：
- plan 文件不存在 / 不可读 / 空 → 单行 stderr + 退出码 2。
- `load_json` 抛**任意**异常（`json.JSONDecodeError` malformed / `pydantic.ValidationError` schema 违反 / `ValueError` 重复键）→ 单行 stderr + 退出码 2。
- `plan.target_name` 未在 `targets.yaml` 注册 / `targets.yaml` 损坏或不可读 → 退出码 **3**（与既有 CLI「config/target 错误=3」一致，如 `inspect`）。**捕获契约必须覆盖全部 target 解析异常**：`build_registry_from_config(load_targets_config(...))` 对 schema 损坏的 targets.yaml（未知 type / SSH 字段违规 / name 不匹配正则）抛 **`pydantic.ValidationError`**（非 ConfigError），YAML 语法 / env 占位 / docker host scheme 错抛 **`ConfigError`**，`path.read_text()` 对**不可读 / 目录形 targets.yaml** 抛 **`OSError`**（`PermissionError`/`IsADirectoryError`，未被 `load_targets_config` 包裹），`registry.get(name)` 未命中抛 **`KeyError`**（非 TargetError）——故 CLI 必须 `except (KeyError, TargetError, ConfigError, ValidationError, OSError)` 全部捕获映射为退出码 3、**无 traceback**（只 catch `TargetError` 会漏其余冒 traceback；注：`inspect.py` 当前同样漏 `OSError`，是项目既有对等缺口，P2 在 fix 入口补齐）。
退出码契约（沿用项目 `3 > 2 > 1 > 0` 优先级）：0 成功；**1** 非 TTY 无 --yes / 用户拒批 / 非交互 high-risk / 执行失败（含回滚不完整）——这几类共享 1，但 **stderr 必须带机器可解析前缀区分**（`approval-rejected:` 安全门拒 vs `execution-failed:` 执行失败），供脚本可靠区分；**2** 非法 plan（schema/重复键/malformed/文件 IO）；**3** 配置 / target 解析错误。

#### 场景:默认 dry-run 不执行
- **当** 显式 `--dry-run` 或 TTY 下未确认的 `hostlens fix <plan>`
- **那么** 展示 plan 三元组 diff，**不执行任何命令、不改状态、不写 audit.log**

#### 场景:--dry-run 覆盖 --yes（flag 优先级）
- **当** 同时给出 `--dry-run` 与 `--yes`
- **那么** **dry-run 优先**：只预览、零执行、不写 audit.log（`--yes` 被抑制，绝不因 `--yes` 而执行）

#### 场景:拒绝 root 且不泄漏 plan 内容
- **当** 以 `EUID==0` 运行 `hostlens fix`（含 dry-run）
- **那么** 在 **load_json / 打印任何 plan step 命令之前**拒绝并以非零退出（退出码 1，最早安全门）、提示非 root 运行；stdout/stderr **不含**任何 plan step 命令内容

#### 场景:非法 plan 文件被拒不 traceback
- **当** `hostlens fix` 加载一个含重复 JSON 键 / malformed JSON / 违反 P1a schema 的 plan 文件，或文件不存在/不可读
- **那么** 单行 stderr 错误、退出码 2，**无 traceback**，不进入执行

#### 场景:target 未注册或 targets.yaml 损坏/不可读退出码 3
- **当** `plan.target_name` 不在 `targets.yaml`（`KeyError`），或 schema 损坏（`pydantic.ValidationError`），或 YAML/env/docker-host 配置错（`ConfigError`），或文件**不可读/是目录**（`OSError`/`PermissionError`/`IsADirectoryError`）
- **那么** CLI 捕获 `(KeyError, TargetError, ConfigError, ValidationError, OSError)`、单行 stderr、退出码 3、**无 traceback**，不进入执行

### 需求:audit 必须 append-only JSONL、两段式（intent + result）、记三态、脱敏、不可写不静默

系统必须把每次**真实执行**的 `hostlens fix` 以**两段式**追加写入 `~/.local/share/hostlens/audit.log`（**append-only、永不轮转、永不删除**，JSONL 每行一条）：
- **intent 记录**（执行**前**写）：who / when / target_name / plan 标识（finding_id + plan 内容 sha256）/ phase=`started`。这保证即便执行中途进程崩溃（SIGKILL/断电）也留下「曾尝试执行此 plan」的取证痕迹（缩小「改了状态却零记录」窗口）。
- **result 记录**（执行**后**写）：逐 step 三态 + rollback 结果。

`who` 必须取 **`pwd.getpwuid(os.geteuid()).pw_name` + 数值 uid**（不用可被 `$USER` 伪造的环境变量）；**`pwd.getpwuid` 抛 `KeyError`（容器 arbitrary UID 无 passwd 条目）时回退到 `str(os.geteuid())`，不崩溃**。step 失败区分三态：`precheck-blocked` / `forward-failed`（含 `exit_code` 可为 null、timed_out、或传输错误摘要）/ `verify-failed`；rollback 结果（`rollback-unavailable` / rollback 成 / rollback 败）也须记。写入前命令串过既有脱敏（`core/redact.py` `redact_text`，**best-effort**，见下「已知残留」）。**dry-run 不写 audit.log**（保持取证日志纯真实执行事件；dry-run 仅 stderr/stdout 展示将写的记录形态）。

**audit 不可写不静默，且区分时序**：执行前预检目录可写。**intent 写失败**（exec 尚未开始、零副作用）→ **中止执行**、stderr 报错、非零退出（损失为零）。**result 写失败**（exec 已完成、副作用已发生）→ stderr 明确告知「副作用已发生但审计未完整落盘」+ 已执行 step 摘要、非零退出，**不**静默吞掉。

**已知残留泄漏面（显著声明）**：`redact_text` 不覆盖 **shell flag 形密钥**（`redis-cli -a <pw>` / `mysql -p<pw>` / `https://user:pw@host` URL userinfo）——这些会原样进 audit.log，而 audit.log **永不删除 → 永久留痕**。Plan 作者**必须**通过 `ExecutionTarget.exec` 的 `env` 参数注入密钥、禁止明文写进 `forward_cmd`。

#### 场景:成功执行两段式写 audit
- **当** 一个 plan 成功执行
- **那么** audit.log 先追加一行 intent（who/when/target/plan-hash/started），执行后再追加一行 result（各 step forward-ok/verify-ok）

#### 场景:三态失败被区分记录
- **当** 一次执行分别发生 precheck 拒 / forward 报错 / verify 失败
- **那么** audit result 对应记 `precheck-blocked` / `forward-failed`（含 `exit_code` 可 null **+ `timed_out` 字段：`true`=超时 / `false`=断连，消费方据此机械区分两种 `exit_code is None`**）/ `verify-failed`，可机械区分

#### 场景:audit 命令 best-effort 脱敏
- **当** plan 的 `forward_cmd` 含 `core/redact.py` 可识别形态的敏感串（`key=value` / `Bearer` / JWT / `sk-`）
- **那么** audit 写入与 CLI 预览展示均对该串脱敏；**注**：CLI flag 形密钥（如 `-a <pw>` / `mysql -p<pw>` / `user:pw@host` URL）`redact_text` 不覆盖，属已知残留泄漏面（见 Security）

#### 场景:audit 不可写不静默且区分时序
- **当** audit.log 所在目录不可写
- **那么** 执行前预检报错退出（退出码 1、未发生副作用——这是执行前安全门拦截）
- **当** intent 写失败（exec 尚未开始）
- **那么** **中止执行**、stderr 报错、非零退出（零副作用、零损失）
- **当** result 写失败（exec 已完成、副作用已发生）
- **那么** stderr 明确告知「副作用已发生但审计未完整落盘」+ 已执行 step 摘要、非零退出，**不**静默吞掉

### 需求:dry-run 与真实执行必须共享同一编排、仅在 exec 边界分叉

系统的 dry-run 与真实执行必须走**同一套编排**（顺序：**EUID 门（最早）→** 加载 → target 解析 → 预览 → 审批门 → 倒序结构），仅在「是否真正调用 `ExecutionTarget.exec`」与「是否写 audit.log」两处分叉：dry-run 下 `ExecutionTarget.exec` **零调用**、不改远端状态、不写 audit.log，但仍展示完整将执行的命令序列。EUID==0 门在两种模式下都是最早门（先于加载/预览）。这保证编排正确性（审批、rollback 顺序、门）可在 dry-run 下完整验证、与真实执行一致。

#### 场景:dry-run 展示完整命令序列但零执行零 audit
- **当** dry-run 一个多 step plan
- **那么** 展示每步将跑的 precheck/forward/verify/rollback 命令，但 `ExecutionTarget.exec` **零调用**、远端状态不变、audit.log **不被追加**
