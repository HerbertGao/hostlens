## 上下文

M9 P1a（`RemediationPlan`/`RemediationStep` + `load_json`）、P1b（Planner 产 plan）已合并归档。P2 实现**执行**——Hostlens 第一次真正改远端状态。

现有地基（复用）：
- `RemediationPlan` / `RemediationStep`（`remediation/models.py`）：precheck/forward/rollback/verify 三元组 + risk_level，全 fail-closed；`load_json` 拒重复键。
- `ExecutionTarget.exec(cmd, *, timeout, env)` → `ExecResult`（`exit_code=None` 表示无 OS 退出码——超时**或**远端断连；判超时**一律看 `timed_out` 字段**、不看 exit_code）；传输层失败（auth/connection）`exec` **抛 `TargetError`** 而非返回 ExecResult。
- `NoopApprovalService`（`tools/base.py`）：M2 stub，永久 noop（P2 **不**替换它）。
- CLI 写门范本：`cli/notify.py`（`--yes` / 非 TTY 无 yes→exit 1 / TTY confirm；但 notify 只读豁免 root，**`fix` 是真写、须拒 root**）。
- `core/redact.py`：脱敏，复用于 audit / 预览。

约束：Python 3.11 async；`mypy --strict`；§4.5 写操作硬约束（plan→preview→approve→execute→rollback、默认 dry-run、非 TTY 无 yes 退 1、拒 root）。

## 目标 / 非目标

**目标：**
- 确定性 Executor（顺序执行 + 倒序 rollback + 三态结果），自成子系统。
- `ApprovalGate`（交互/--yes/high 双确认）与 audit JSONL。
- `hostlens fix` CLI：load → preview → approve → execute，默认 dry-run、拒 root。
- **dry-run 先行**：真实 `exec` 接通是最后一个 task。

**非目标：**
- 不做飞书远程审批（P3）、不暴露执行给 Agent/MCP、不替换 ToolContext.ApprovalService、不做 plan 生成/落盘 wiring、不做 scheduler 自动 fix、不加新 Capability、不改 P1a/P1b 契约。

## 决策

### 决策 1：Executor 自成子系统，确定性，不进 Registry / 不持 backend

`Executor`（`remediation/executor.py`）只依赖 `RemediationPlan` + `ExecutionTarget`，不持 `LLMBackend`、不进 `ToolRegistry`、不被任何 surface adapter 投影、不进 Agent loop。它是 CLI 触发的**写子系统**，与 Notifier（§4.4「不进 Tool Registry」）同类。落实 M9 不变量 2。执行经既有 `ExecutionTarget.exec`（`Capability.SHELL`），**不加新 Capability、不引入受限写 API**——安全边界是「审批 + audit + rollback + 非 root」而非 capability 限制（M9 探索阶段定论）。

### 决策 2：执行语义 —— precheck→forward→verify，基于 ExecResult 判定

每步：① `precheck_cmd`（若非 None）→ ② `forward_cmd` → ③ `verify_cmd`。**成功推进当且仅当该阶段 `ExecResult.exit_code == 0`**（用充要条件而非「非 0 即失败」，使 `exit_code is None`/断连/超时自动落入未成功，fail-safe 保守回滚）。`exit_code is None`（无 OS 退出码——超时或远端断连，base.py 契约允许）、`exit_code != 0` 一律未成功。**`timed_out` 不参与成功判定**（由 `exit_code is None` 已覆盖，因不变量 `timed_out ⇒ exit_code is None`），仅用于 audit 记录超时原因（`exit_code` 可为 `128+signum` 信号杀，故超时绝不用 exit_code 判）。判定分支只写 `if exit_code != 0`（与 spec SOT 一致，勿写 `or timed_out` 冗余）。**`exec` 抛 `TargetError`（传输层失败，base.py 明确 raise 非返回 ExecResult）必须被捕获、视同该阶段未成功**（绝不让 traceback 冒泡），forward-failed 记录 `exit_code` 可为 `null` + 传输错误摘要。precheck 是 P1a 决策 1 的 TOCTOU 守卫；P1a `high_requires_precheck` 保证 high-risk step 必有 precheck，故最危险类恒有前提守卫。

**替代方案**：跳过 precheck 直接 forward——否决：放弃抗审批延迟漂移（P1a 引入 precheck 的全部理由）。

### 决策 3：失败→倒序 rollback，best-effort，三态区分

任一步未成功推进（含该阶段 exec 抛 TargetError）→ 倒序遍历**已成功**的 step、跑 `rollback_cmd`。设计要点：
- **倒序**：后执行的先回滚（依赖顺序；rollback 也禁并发）。
- **回滚边界**：`verify` 失败时该步 forward 已改状态→**该步算需回滚（含本步）**；`forward` 失败时该步未成功推进、**不回滚本步**（回滚已成功的更早 step）。
- **best-effort**：`rollback_cmd is None`（P1a 仅 high-risk 允许）→ 记 `rollback-unavailable`、继续；某 rollback 自身失败（含其 exec 抛异常）→ 记失败、继续倒序其余（半回滚 > 不回滚）。
- **三态失败**：`precheck-blocked`（没碰）/ `forward-failed`（exit_code≠0/null/超时/传输错）/ `verify-failed`（执行了但结果不对，最危险，rollback 含本步）。三态写进 audit、可机械区分。

### 决策 4：ApprovalGate 独立于 ToolContext.ApprovalService

真审批 `ApprovalGate`（`remediation/approval.py`）给 Executor/CLI 用；`ToolContext.ApprovalService` **永久 `NoopApprovalService`**。理由（M9 探索定论）：把真 ApprovalService 塞进 ToolContext 等于给所有 agent-surface handler 开「请求审批后做写操作」的门，破坏「agent 表面永久只读」。`NoopApprovalService` 注释从「M9 will replace」澄清为「永久 noop」（文档澄清，不改行为）。`ApprovalGate` 接口为 P3 飞书远程审批留扩展点（同一 gate 抽象，远程 token 校验是另一实现）。

### 决策 5：写门 —— EUID==0 拒 / 非 TTY 无 --yes 退 1 / 默认 dry-run

镜像 `notify.py` 的 TTY/--yes 门，但 `fix` 是**真写**：
- **拒 root（最早门）**：`os.geteuid()==0` → 退出，**在 load_json / target 解析 / 任何 plan 内容渲染之前**（含 dry-run 入口——防误以为 dry-run 安全就 sudo；也防 root 拒绝前 preview 泄漏 plan 命令的 flag 形密钥；继承全局 CLAUDE.md）。P2 是首个真写操作，此 EUID 拒绝是新增（之前 notify 只读豁免）。
- **默认 `--dry-run` + flag 模型**：无独立「执行标志」——`--yes`（或交互确认）即执行信号；`--dry-run` 强制只预览、覆盖 `--yes`（不给执行信号时只展示不执行）。
- **非 TTY 无 `--yes` 退 1**：绝不默默执行。
- **high-risk 强制人眼在场（`--yes` 永不覆盖 high-risk 双确认）**：交互模式含 high-risk step 走**双重确认**（第二次输入确认短语，如 finding_id）；**给 `--yes` 时——TTY 下普通 plan 跳 `y/N` 直接执行，但含 high-risk 的 plan 仍必须走第二道确认短语**（`--yes` 只跳第一道 `y/N`、不跳 high-risk 第二道）；**非交互即便有 `--yes` 也拒绝执行含 high-risk 的 plan（退 1，无法输入短语）**。这是 F1/B2 安全决定：`--yes` 只覆盖普通 `y/N`、对 high-risk 双确认完全无效（交互/非交互对称），防自动化或手滑零双确认跑 `rm -rf`/`kill -9`。
- **退出码（沿用项目 `3>2>1>0`）**：0 成功；1 非 TTY 无 --yes/拒批/非交互 high-risk/执行失败；2 非法 plan（schema/重复键/malformed JSON/文件 IO）；**3 配置 / target 解析错误**（与 `inspect` 等既有 CLI 一致，不把配置错误塞进 2）。

### 决策 5b：target 解析 —— CLI 把 plan.target_name 解析为活体 ExecutionTarget

`plan` 携带 `target_name: str`，Executor 吃活体 `ExecutionTarget`。CLI 经既有路径解析：`load_targets_config` → `build_registry_from_config` → `registry.get(plan.target_name)`（与 `inspect` 同款）。`local` **不隐式存在**——裸环境无 `targets.yaml` 时 `registry.get('local')` 抛 KeyError，故 Demo Path 必须先有含 `local` 的 `targets.yaml`（`hostlens target add --type local` 或提供配置）。**捕获契约覆盖全部 target 解析异常（关键）**：`registry.get` 未命中抛 `KeyError`、schema 损坏抛 `pydantic.ValidationError`、YAML/env/docker 配置错抛 `ConfigError`、文件不可读/目录形抛 `OSError`（`path.read_text()` 未被 load_targets_config 包裹）——CLI 必须 `except (KeyError, TargetError, ConfigError, ValidationError, OSError)` 全捕获 → 退出码 3、无 traceback（只 catch TargetError 会漏其余冒 traceback；`inspect.py` 当前漏 `OSError`，P2 在 fix 入口补齐）。

### 决策 6：audit 两段式（intent+result）+ 三态 + best-effort 脱敏 + dry-run 不写

`remediation/audit.py` 追加写 `~/.local/share/hostlens/audit.log`（append-only、永不轮转/删除，JSONL）。**两段式**抗 mid-exec crash：执行**前**写 intent（who/when/target/plan-hash/started），执行**后**写 result（逐 step 三态 + rollback 结果）——即便执行中途 SIGKILL/断电也留「曾尝试执行」痕迹（缩小「改了状态却零记录」窗口）。`who = pwd.getpwuid(os.geteuid()).pw_name + 数值 uid`（**不用可被 `$USER` 伪造的环境变量**；`getpwuid` 抛 `KeyError`（容器 arbitrary UID）时回退 `str(geteuid())`、不崩）。**dry-run 不写 audit.log**（保持取证日志纯真实执行事件，dry-run 只 stderr 展示将写形态）。**脱敏 best-effort**：命令串过 `core/redact.py` `redact_text`，覆盖 `key=value`/`Bearer`/JWT/`sk-` 形态，**不覆盖 CLI flag 形密钥**（`-a <pw>` / `mysql -p<pw>` / `user:pw@host` URL）——属已知残留泄漏面（Security 节诚实记录 + 建议密钥走 env 注入）。**不可写不静默、区分时序**：执行前预检目录可写；**intent 写失败（exec 未开始、零副作用）→ 中止执行 + 非零退出**（损失为零）；**result 写失败（exec 已完成、副作用已发生）→ stderr 告知「副作用已发生但审计未完整落盘」+ step 摘要 + 非零退出**。退出码 1 的「拒批/非交互 high-risk」与「执行失败」靠 **stderr 机器可解析前缀**区分（`approval-rejected:` vs `execution-failed:`）。

### 决策 7：dry-run 先行 —— 真实 exec 是最后一个 task

实现纪律（落到 tasks 顺序）：先实现完整 dry-run 路径（load → target 解析 → preview → approval gate → 「假执行」打印命令序列 → EUID/TTY 门；dry-run **不**写 audit.log），全链路绿了，**真实 `ExecutionTarget.exec` 接通 + 真实 audit 落盘是最后一个 task**。dry-run 与真实共享同一编排、仅在「是否调 exec」与「是否写 audit.log」两处分叉。好处：把「编排 bug」（rollback 顺序、门、双确认、退出码）和「真实副作用」解耦验证——编排在 dry-run + 测试下抓全，真实接通时只剩「命令真的跑了」一个变量，真实写出错窗口压到最小。

### 决策 8：plan 来源 —— load_json 从文件，P1b→文件 wiring 是薄缝

`hostlens fix <plan-file>` 经 `RemediationPlan.load_json` 从 JSON 文件加载（P1a 给 P2 的拒重复键入口）。P1b 的 `RemediationPlannerResult.plans` → 落盘文件的 wiring 是**薄缝**：demo 用 plan 的 `model_dump_json` 写一个文件即可，不在 P2 核心。理由：P2 聚焦「给定 plan 文件 → preview→approve→execute→audit→rollback」的写路径；plan 持久化/选择策略留 P1b/后续。

## 风险 / 权衡

- [真实写攻击面引入] → 三件套兜底（人审 + 非 root + append-only audit）；执行不上 MCP/不经 Agent，杜绝远程/LLM 触发。本提案不追求「命令语义安全可机械证」——靠人审。
- [rollback 也可能失败、留半改状态] → best-effort + 全程 audit + 退出码反映「回滚不完整」；半回滚 > 不回滚。verify-failed 的 rollback 须含本步（最危险类）。
- [precheck 缩小而非消除 TOCTOU 窗口] → precheck 与 forward 间仍有毫秒级窗口（P1a 已诚实记录）；接受。
- [plan 文件来源薄缝可能让 demo「不够端到端」] → 接受：P2 核心是写路径；P1b→文件 wiring 与 demo CLI 接线一并属集成 follow-up（P1b 已登记 `wire-remediation-planner-into-demo-pipeline`）。
- [audit.log 永不轮转 → 长期增长] → 接受：审计不可丢是底线，轮转/归档属运维策略、不在 schema；单条记录小，增长可控。
- [`redact_text` 对 shell flag 形密钥（`-a <pw>`/`mysql -p<pw>`/URL userinfo）不覆盖 → audit 永久留痕泄漏面] → 脱敏降为 best-effort（覆盖 key=value/Bearer/JWT/sk- 形态）；Security 节诚实记录残留面 + 建议 plan 作者别把密钥写进 `forward_cmd` 明文、走 env 注入（`exec` 的 `env` 参数）。不假装完全脱敏。
- [mid-exec crash（SIGKILL/断电）状态已改但 result 未写] → 两段式 audit（intent 执行前写、result 执行后写）缩小窗口：crash 后至少留「曾尝试执行此 plan」痕迹。intent 与 result 间仍有 crash 窗口（不可完全消除），但比单段式（执行后才写）大幅改善。

## Migration Plan

无迁移。纯新增（`remediation/{executor,approval,audit}.py` + `cli/fix` + 测试）。`tools/base.py` 的 NoopApprovalService 注释澄清（若动）走「修改 tool-registry-capability-layer」增量 spec。回滚 = 删新增文件 + 摘 `hostlens fix` 命令注册（无下游消费方）。

## Open Questions

- 双重确认的「确认短语」具体形态：输入 plan 的 finding_id？还是固定 `yes-execute`？倾向输入 finding_id（强制看清楚在批哪个 plan），实现期定。
- audit 的 plan 标识：finding_id + 全 plan 内容 hash（sha256）足够追溯；是否还要记 plan 文件路径？倾向都记（路径便于复查、hash 防篡改）。
- `hostlens fix` 是否支持一次多 plan（一个文件含多 plan / 多文件）？倾向 P2 先单 plan 文件（最小、最安全）；多 plan 批量留后续。
