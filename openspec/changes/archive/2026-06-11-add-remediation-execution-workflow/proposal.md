## 为什么

M9 P1a（契约 schema）、P1b（Planner 拟方案）已合并归档。P2 是 M9 第三片、也是**整个 Hostlens 第一次真正改远端状态**——把 P1b 产出的 `RemediationPlan` 经人工审批后**实际执行**，并预备回滚。§4.5 的写操作硬约束分量在此最重。

按 M9 切分纪律，P2 是写代码风险最高的一片，必须**最后才碰、单独碰**，且实现上**dry-run 先行**：先把全部编排（加载 → 预览 → 审批门 → 倒序 rollback → audit → EUID/TTY 门）在 dry-run 下验证完毕，真实 `target.exec` 接通是**最后一个 task**——把「编排 bug」与「真实副作用」解耦验证，使真实写出错的窗口压到最小。

## 变更内容

- 新增 `src/hostlens/remediation/executor.py`：**确定性 Executor**，顺序执行 `RemediationPlan.steps`，每步 `precheck → forward → verify`；任一步未能成功推进则**倒序**对已成功 step 跑 `rollback_cmd`。Executor **不持 `LLMBackend`、不进 Tool Registry、不进 Agent loop**——自成子系统（类比 Notifier），经 `ExecutionTarget.exec`（既有 `Capability.SHELL`）执行，**不引入新 Capability / 不调任何 agent 工具**。
- 新增 `src/hostlens/remediation/approval.py`：独立 **`ApprovalGate`**——交互 `y/N` + `--yes`；`risk_level=="high"` 的 step 走**双重确认**。**与 `ToolContext` 里的 `ApprovalService` 严格分离**——后者永久保持 `NoopApprovalService`（agent-surface handler 永不触发审批，M9 不变量 3）。
- 新增 `src/hostlens/remediation/audit.py`：**append-only JSONL 审计日志**（`~/.local/share/hostlens/audit.log`，**永不轮转、永不删除**），每次 fix 记 who / when / target / plan hash / 逐 step 结果；失败区分三态——`precheck-blocked`（前提漂移、没碰）/ `forward-failed`（执行报错）/ `verify-failed`（执行了但结果不对）。
- 新增 CLI `hostlens fix <plan-file>`：编排 **① EUID==0 拒（最早门）→** ② `load_json` 加载 → ③ target 解析 → ④ 展示三元组 diff → ⑤ 审批门 → ⑥ 执行。**默认 `--dry-run`**（只展示、不执行、不写 audit）；`--yes` 跳过普通 `y/N` 交互、**但不跳过 high-risk 双确认**。
- **写操作硬约束**（全局 CLAUDE.md + §4.5）：**拒绝以 root 运行（EUID==0）**——P2 是首个真写操作，引入 EUID 拒绝；**非交互（无 TTY）缺 `--yes` 直接退出 1**（绝不默默执行）；默认 dry-run。
- `doctor` 增 `checks.remediation`（audit.log 目录可写 / 当前非 root，非致命提示）。

**执行语义**（基于 P1a 决策）：每步先跑 `precheck_cmd`（验证假设仍成立，抗审批延迟 TOCTOU）——失败=世界已漂移→**中止**（记 `precheck-blocked`，倒序 rollback 已成功 step）；再 `forward_cmd`（`ExecResult.exit_code==0` 为成功，超时判 `timed_out`）；再 `verify_cmd`。`rollback_cmd` 为 `None`（P1a 仅 high-risk 允许）→ 该 step 不可回滚→audit 记 `rollback-unavailable`、倒序继续（best-effort）。

## 功能 (Capabilities)

### 新增功能
- `remediation-execution-workflow`: 受控修复的**执行**契约——`Executor`（顺序执行 + 倒序 rollback + 三态结果）、`ApprovalGate`（交互/--yes/high 双确认）、audit JSONL、`hostlens fix` CLI（load → preview → approve → execute），及 EUID==0 / 非 TTY 无 --yes / dry-run 默认 三道写操作门。

### 修改功能
<!-- 无新增对外契约破坏。**「ToolContext.ApprovalService 永久 NoopApprovalService」这条不变量已在本 spec 的「ApprovalGate」需求显式声明**（P3 开发者读本 spec 即可见，不会误以为需替换 ApprovalService）。`tools/base.py` 的 NoopApprovalService docstring「M9 will replace」字面与此不变量相悖，但其更新属另一 capability（tool-registry-capability-layer）的文档澄清——**本提案不改该注释**，统一登记为 follow-up：另起「修改 tool-registry-capability-layer」增量 spec（tasks 2.2 同款表述）。不改 P1a remediation-plan-schema、不改 P1b remediation-planner-agent。 -->

## 影响

- **新增代码**：`remediation/{executor,approval,audit}.py`、`cli/fix.py`（或 `cli/` 下对应命令）、对应测试。
- **对外契约影响**：新增 **1 个 CLI 命令** `hostlens fix`。**不新增** Agent tool schema / MCP tool schema（执行**不**暴露给 Agent / MCP，agent 表面永久只读 M9 不变量 1）；不改 Inspector schema / Notifier Protocol / Schedule manifest。不改 P1a/P1b 契约。
- **依赖**：无新增第三方依赖（`ExecutionTarget` / Typer / 标准库 json 已有）。
- **上游依赖**：P1a `remediation-plan-schema`（`RemediationPlan` / `load_json`）、P1b 产出 plan。**下游**：M9 退出闭环达成；P3（飞书卡片审批）在此 `ApprovalGate` 上扩远程触发。

## 架构不变量对齐（M9）

1. **Agent 表面永久只读** —— P2 不注册任何 agent/mcp surface 的执行工具；`hostlens fix` 是人类 CLI、不经 Agent loop；`tools_adapter` M2 gate 不放开。
2. **Remediation 自成子系统，不进 Tool Registry** —— Executor 是 CLI 触发的写通道（类比 Notifier），不被任何 adapter 投影成可执行工具、不持 `LLMBackend`、不进 loop。
3. **审批门与 ToolContext 分离** —— 真 `ApprovalGate` 在 `remediation/`、给 Executor/CLI 用；`ToolContext.ApprovalService` 永久 `NoopApprovalService`（绝不把真审批塞进 ToolContext，否则给所有 handler 开写后门）。
4. **不引入受限写 API / 不加新 Capability** —— 写经既有 `ExecutionTarget.exec`（`Capability.SHELL`）；安全边界是「审批 + audit + rollback + 非 root」，不是 capability 限制。

## 写操作机制（审批 / 回滚 / 非 root，本节为强制项）

**审批**：`hostlens fix` 默认 `--dry-run` 只展示 plan 三元组 diff、不执行。执行需过 `ApprovalGate`：
- 交互（TTY）：逐 plan 展示后 `y/N` 确认；含 `risk_level=="high"` step 时**双重确认**（第二次须输入确认短语，如 finding_id，防手滑）。
- 非交互（无 TTY）：缺 `--yes` **直接退出 1**（绝不默默执行）；给 `--yes` 视为已通过普通 `y/N`。**但 `--yes` 不足以批含 high-risk step 的 plan**——含 high-risk 的 plan 在非交互下即便有 `--yes` 也**拒绝执行（退 1）**，强制最危险类（`rm -rf`/`kill -9`/改 systemd）必须人眼在场过双确认，杜绝自动化路径无人值守跑高危。

**回滚**：任一步「未成功推进」（precheck 拒 / forward 报错或超时 / verify 失败）→ **倒序**遍历**已成功**的 step、跑其 `rollback_cmd`。`rollback_cmd=None`（P1a 仅 high-risk step 允许）→ 记 `rollback-unavailable`、倒序继续（best-effort，不中断其余 rollback）。rollback 本身的成败也写 audit。

**非 root**：`hostlens fix` 的 `os.geteuid()==0` 拒绝是**最早的门**——在 `load_json` / target 解析 / **任何 plan 内容渲染之前**拒绝退出（继承全局 CLAUDE.md「写操作必须拒绝 root」；避免 sudo 制造 root-owned 文件 / 扩大 blast radius）。dry-run 入口同样最早拒 root。**先于 preview** 是安全要点：plan 命令可能含 `redact_text` 漏脱的 flag 形密钥，若 root 拒绝发生在 preview 之后会先把 plan 内容打到 stdout/stderr 泄漏。

## Non-Goals（非目标）

- ❌ **不做飞书卡片远程审批** —— 属 P3（`add-remediation-lark-approval`，experimental）；P2 的 `ApprovalGate` 为其留扩展点但不实现远程触发。
- ❌ **不把执行暴露给 Agent / MCP** —— agent 表面永久只读；`hostlens fix` 是人类 CLI，不进 Agent loop、不上 MCP。
- ❌ **不替换 `ToolContext.ApprovalService`** —— 它永久 `NoopApprovalService`；真审批是 `remediation/` 下独立 `ApprovalGate`。
- ❌ **不做 plan 自动生成 / Planner→落盘 wiring** —— plan 由 P1b 产、`hostlens fix` 取**已落盘的 plan 文件**（经 `load_json`）；P1b 输出→plan 文件的持久化 wiring 是薄缝（demo 用 P1b plan 的 `model_dump_json` 落一个文件即可），不在 P2 核心范围。
- ❌ **不做 scheduler 自动 fix** —— `hostlens fix` 人类显式调用；调度自动修复属远期、需更强审批模型。
- ❌ **不引入新 Capability / 受限写 API** —— 走既有 `ExecutionTarget.exec`。
- ❌ **不改 P1a/P1b 契约** —— 只消费 `RemediationPlan`；若实现中发现缺字段，走对应增量 spec。

## Failure Modes

1. **precheck 失败（世界已漂移）** → 中止该 plan、不执行 forward，倒序 rollback 已成功 step，audit 记 `precheck-blocked`。fail-closed（抗 TOCTOU），非崩溃。
2. **forward 报错 / 超时 / 断连 / exec 抛 TargetError** → `exit_code != 0` / `exit_code is None`（断连或超时）/ `timed_out` / 或 `exec` 抛 `TargetError`（传输层失败）——一律判失败、捕获异常**不冒 traceback**、倒序 rollback，audit 记 `forward-failed`（`exit_code` 可为 null）。
3. **verify 失败（执行了但结果不对）** → forward 成功但 verify 未成功，倒序 rollback（**含本步**），audit 记 `verify-failed`——最危险类（改了状态但没达预期），rollback 必须覆盖它。
4. **rollback 本身失败 / rollback exec 抛异常** → audit 记该 rollback 失败、**继续**倒序其余（不让一个 rollback 失败阻断其余）；最终退出码反映「执行失败且回滚不完整」。
5. **audit.log 不可写**（目录权限 / 磁盘满）→ 执行前 doctor / 入口预检；若执行中写失败，**优先保证不静默吞**——记 stderr + 非零退出（审计不可丢是写子系统底线）。

## Operational Limits

- **并发预算**：单 plan 内 step **严格顺序**执行（rollback 依赖顺序，禁并发）；多 plan 顺序处理。
- **内存预算**：plan + 各 step 的 `ExecResult`（stdout/stderr 截断上限沿用 `ExecutionTarget` 既有约束）小对象，可忽略。
- **超时设置**：每步 `exec` 复用 `ExecutionTarget` 的 per-call timeout；plan 级总时长 = `estimated_duration_seconds` 仅作展示参考，不做硬超时（执行中途砍断更危险）。precheck/verify 也各受 per-call timeout。

## Security & Secrets

- **新密钥**：无。
- **脱敏（best-effort，诚实声明残留面）**：`forward_cmd` 等可能含敏感串。audit 写入与 CLI 预览前过 `core/redact.py` `redact_text`——它覆盖 `key=value` / `Bearer` / JWT / `sk-` 形态，但**不覆盖 CLI flag 形密钥**（`redis-cli -a <pw>` / `mysql -p<pw>` / `https://user:pw@host` URL userinfo）。这些会原样进 audit.log（永不删除 → 永久留痕泄漏面）。**不假装完全脱敏**：建议 plan 作者别把密钥写进 `forward_cmd` 明文、走 `exec` 的 `env` 参数注入；残留面登记为已知限制，更强脱敏（命令级密钥识别）留后续。
- **攻击面**：P2 **引入真实写攻击面**（任意 shell 经 `exec` 执行）——M9 最重一片。边界三件套：**人工审批**（plan 逐字过人眼，high-risk 双确认且非交互不可批）+ **非 root**（EUID==0 拒）+ **append-only 两段式 audit**（intent+result，事后可追责）。无新网络/凭据面；`hostlens fix` 不上 MCP、不经 Agent，杜绝远程/LLM 触发执行。

## Cost / Quota Impact

- **Token 消耗**：0 —— P2 不调任何 LLM（plan 已由 P1b 产出；执行纯确定性）。
- **API 调用频次**：0（Anthropic）。
- **对配额影响**：无。

## Demo Path

5 分钟内本地 reproduce（无 SSH、无付费 API；用 `local` target + 临时目录，**可逆 medium-risk** plan 故 `--yes` 可非交互跑、且演示 rollback）：

```bash
pip install -e ".[dev]"
pytest tests/remediation/test_executor.py tests/cli/test_fix.py -v   # 全绿（dry-run + 真实执行 + rollback + audit）

# 前置：注册 local target（plan.target_name 须能在 targets.yaml 解析，否则退 3）
hostlens target add --type local --name local    # 或提供含 local 的 ~/.config/hostlens/targets.yaml

# 离线 demo：可逆「归档旧日志」plan（mv 到备份，rollback 可还原 → medium-risk）
mkdir -p /tmp/hostlens-demo/log && echo data > /tmp/hostlens-demo/log/old.gz
cat > /tmp/demo-plan.json <<'JSON'
{"finding_id":"demo-disk","target_name":"local","rationale":"归档 /tmp/hostlens-demo/log 旧日志",
 "estimated_duration_seconds":2,
 "steps":[{"description":"归档旧日志（可逆）","precheck_cmd":"test -f /tmp/hostlens-demo/log/old.gz",
   "forward_cmd":"mv /tmp/hostlens-demo/log/old.gz /tmp/hostlens-demo/log/old.gz.bak",
   "rollback_cmd":"mv /tmp/hostlens-demo/log/old.gz.bak /tmp/hostlens-demo/log/old.gz",
   "verify_cmd":"test -f /tmp/hostlens-demo/log/old.gz.bak","risk_level":"medium"}]}
JSON
hostlens fix /tmp/demo-plan.json                # 默认 dry-run：展示三元组 diff、零执行、不写 audit
hostlens fix /tmp/demo-plan.json --yes          # 审批后执行：precheck→forward→verify、写 audit
tail -2 ~/.local/share/hostlens/audit.log       # 看 intent + result 两段式审计记录
```

预期：dry-run 只展示不动文件、audit.log 不变；`--yes` 后执行（precheck 通过→mv→verify 通过）、audit 先 intent 后 result 记 `forward-ok/verify-ok`；以 root 跑则**拒绝退出**；若改 plan 的 `verify_cmd` 为必失败值可观察**倒序 rollback**（mv 还原）。high-risk plan 在非交互 `--yes` 下会被拒（须交互双确认）。这一条 demo 即 M9 退出闭环证据。
