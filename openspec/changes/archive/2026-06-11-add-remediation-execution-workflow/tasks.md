## 1. Executor 编排骨架（dry-run 模式，无真实 exec）

- [x] 1.1 新建 `src/hostlens/remediation/executor.py`：`Executor`（依赖 `RemediationPlan` + `ExecutionTarget`，不持 backend/不进 Registry）；定义执行结果模型（逐 step 三态 + rollback 结果）
- [x] 1.2 顺序执行编排：每步 `precheck → forward → verify` 判定——**成功充要 `exit_code==0`**（`None`/断连/`timed_out` 一律未成功，超时看 `timed_out` 不看 exit_code）；**`exec` 抛 `TargetError`（传输层失败）必须捕获、视同该阶段未成功、绝不冒 traceback**。**先用可注入「命令执行器」抽象**（dry-run 实现只记录不真跑），真实 `target.exec` 接通留 task 5
- [x] 1.3 三态失败判定：`precheck-blocked`（precheck 未成功 → 中止、不执行 forward）/ `forward-failed`（`exit_code` 可 null/超时/传输错）/ `verify-failed`
- [x] 1.4 倒序 rollback：任一步未成功推进 → 倒序遍历已成功 step 跑 `rollback_cmd`；**回滚边界：verify 失败含本步、forward 失败不含本步**；`rollback_cmd is None` → `rollback-unavailable` 继续；单个 rollback 失败（含 exec 抛异常）→ 记失败继续倒序其余
- [x] 1.5 dry-run 与真实共享同一编排，仅在「命令执行器」实现上分叉（dry-run 不调真实 exec、不改状态，但产出完整将执行的命令序列）

## 2. ApprovalGate

- [x] 2.1 新建 `src/hostlens/remediation/approval.py`：`ApprovalGate`（交互 `y/N` + `--yes`；`risk_level=="high"` plan 双重确认——第二次输入确认短语，短语形态见 design Open Questions）
- [x] 2.2 与 `ToolContext.ApprovalService` 严格分离：`ToolContext` 仍用 `NoopApprovalService`（不动）；`ApprovalGate` 独立、为 P3 远程审批留扩展点。**follow-up**：实现期更新 `tools/base.py` 的 `NoopApprovalService` 注释（"M9 will replace" → "永久 noop，真审批在 remediation/ApprovalGate"）需另起「修改 tool-registry-capability-layer」增量 spec（本提案不改该 spec 契约）
- [x] 2.3 `--yes` 只覆盖普通 `y/N`、**永不覆盖 high-risk 双确认**（交互/非交互对称）：非交互缺 `--yes` → 退 1；**TTY + `--yes` + high-risk → 仍走第二道确认短语**（`--yes` 只跳第一道 `y/N`）；**非交互 + `--yes` + high-risk → 拒绝执行（退 1，无法输入短语）**；退出码 1 的拒批 vs 执行失败靠 stderr 前缀（`approval-rejected:`/`execution-failed:`）区分

## 3. Audit 日志

- [x] 3.1 新建 `src/hostlens/remediation/audit.py`：append-only JSONL 写 `~/.local/share/hostlens/audit.log`（永不轮转/删除）；**两段式**——执行前写 intent（who/when/target/plan-hash/started，抗 mid-exec crash），执行后写 result（逐 step 三态 + rollback 结果）；`who = pwd.getpwuid(os.geteuid()).pw_name + 数值 uid`（**不用可伪造的 `$USER`**；`getpwuid` 抛 `KeyError`（容器 arbitrary UID）回退 `str(geteuid())` 不崩）；**dry-run 不写 audit.log**
- [x] 3.2 写入前命令串过 `core/redact.py` `redact_text`（**best-effort**：覆盖 key=value/Bearer/JWT/sk-，不覆盖 CLI flag 形密钥——属已知残留面，见 proposal Security）
- [x] 3.3 audit 不可写不静默且区分时序：执行前预检目录可写；**intent 写失败（exec 未开始）→ 中止执行 + 非零退出（零副作用）**；**result 写失败（exec 已完成）→ stderr 告知「副作用已发生但审计未完整落盘」+ step 摘要 + 非零退出**

## 4. CLI hostlens fix（load / preview / 写门，dry-run 默认）

- [x] 4.1 新建 `hostlens fix <plan-file>` 命令：`RemediationPlan.load_json` 加载——**捕获任意异常**（`json.JSONDecodeError`/`pydantic.ValidationError`/`ValueError` 重复键）+ 文件不存在/不可读/空 → 单行 stderr + 退码、**绝不 traceback**
- [x] 4.2 **target 解析**：`load_targets_config` + `build_registry_from_config` + `registry.get(plan.target_name)`（与 `inspect` 同款）取活体 target；`local` 不隐式存在；**捕获契约覆盖全部解析异常：`registry.get`→`KeyError`、schema 损坏→`pydantic.ValidationError`、YAML/env/docker 配置错→`ConfigError`、文件不可读/目录→`OSError`——必须 `except (KeyError, TargetError, ConfigError, ValidationError, OSError)` 全捕获**、target 未注册 / targets.yaml 损坏 / 不可读 → 退出码 **3**、无 traceback
- [x] 4.3 preview：展示每 step 的 `precheck/forward/rollback/verify` 三元组 diff（命令 best-effort 脱敏展示）
- [x] 4.4 写门：**EUID==0 拒绝是最早门**（排在 load_json / target 解析 / 任何 plan 内容渲染之前，dry-run 入口同样最早拒，防 root 拒绝前 preview 泄漏 flag 形密钥）；**默认 `--dry-run`**（只展示不执行不写 audit）；flag 模型 `--yes`/交互确认=执行信号、`--dry-run` 覆盖 `--yes`；接 `ApprovalGate`
- [x] 4.5 退出码契约（沿用项目 `3>2>1>0`）：0 成功；1 非 TTY 无 --yes / 非交互 high-risk / 拒批 / 执行失败（含回滚不完整）；2 非法 plan（schema/重复键/malformed/文件 IO）；**3 配置 / target 解析错误**（与 `inspect` 一致）
- [x] 4.6 `doctor` 增 `checks.remediation`（audit.log 目录可写 / 当前非 root，非致命）

## 5. 真实执行 + 真实 audit 接通（最后一个 task，dry-run-first 纪律）

- [x] 5.1 把 task 1.2 的「命令执行器」真实实现接通 `ExecutionTarget.exec`（per-call timeout 复用既有），并接通真实两段式 audit 落盘；**此前 1–4 的编排已在 dry-run + 测试下全绿**，本 task 只接通真实副作用 + 真实 audit 边界

## 6. 测试

- [x] 6.1 Executor（dry-run，无真实副作用）：全成功顺序;precheck 失败中止不碰 forward + precheck-blocked;forward-failed 倒序 rollback（不含本步）;verify-failed 倒序含本步;rollback-unavailable 不中断（**构造：`risk_level="high"` + `rollback_cmd=None` + 非 None `precheck_cmd`，满足 P1a `_validate_risk_invariants`**）;单 rollback 失败继续倒序;「已成功推进」= precheck/forward/verify 三阶段 exit_code==0
- [x] 6.2 ExecResult 判定：`exit_code==0` 成功 / 非 0 失败 / `exit_code is None`（断连）判失败 / `timed_out` 判超时（不误判 exit_code）/ **exec 抛 `TargetError` 捕获判失败不冒 traceback**
- [x] 6.3 ApprovalGate：非 TTY 无 --yes 退 1;**TTY + --yes + high-risk 仍走第二道确认短语**;**非交互 + --yes + high-risk plan 被拒（退 1）**;TTY 普通 y/N;high 交互双重确认（两道都过才执行);拒批 vs 执行失败 stderr 前缀区分;ToolContext.ApprovalService 仍 Noop
- [x] 6.4 audit：两段式 intent+result 写 JSONL;三态失败可机械区分;**`timed_out=True` 的 forward-failed 记录含 `timed_out: true`、与断连（`exit_code: null, timed_out: false`）可机械区分**;`who` 不被 `$USER` 伪造（mock 验来源 pwd）+ **getpwuid KeyError 回退 str(uid) 不崩**;命令 best-effort 脱敏（flag 形密钥穿透属已知残留）;**dry-run 不写 audit.log**;**intent 写失败中止执行（零副作用）/ result 写失败告知副作用已发生**（非零退出）
- [x] 6.5 CLI fix：默认 dry-run 不执行不写 audit;**`--dry-run --yes` 同给时 dry-run 优先（零执行零 audit）**;**EUID==0 在 load/preview 之前拒（mock geteuid，退 1，断言 stdout/stderr 不含 plan step 命令）**;非法 plan（malformed/重复键/schema/文件不存在）退 2 不 traceback;**target 未注册（KeyError）/ schema 损坏（ValidationError）/ 不可读·目录（OSError）退 3 不 traceback**;退出码契约各路径
- [x] 6.6 真实执行（local target + 临时目录，安全可逆）：precheck→forward→verify 全跑;失败触发真实 rollback;两段式 audit 记真实结果
- [x] 6.7 dry-run 与真实共享编排：dry-run 下 `ExecutionTarget.exec` 零调用 + audit.log 零追加断言（mock 计数）

## 7. 收尾验收

- [x] 7.1 `mypy --strict` 0 错误（新增模块）
- [x] 7.2 `ruff` 通过
- [x] 7.3 `pytest tests/remediation/ tests/cli/test_fix.py -v` 全绿
- [x] 7.4 跑 proposal「Demo Path」：临时目录 plan，dry-run 不删 / --yes 执行 / audit 记录 / root 拒绝
- [x] 7.5 `openspec-cn validate add-remediation-execution-workflow --strict` 通过
