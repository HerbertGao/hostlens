## 为什么

M9 受控修复 P2 给了 `hostlens fix` 一条「任意风险级 plan 都能在审批后执行」的写路径（high-risk 仅加一道双确认）。但这把最危险的一类操作的最终决定权,交给了一个**对着 AI 生成的命令点头**的人——审批人未必真懂这串 shell 的 blast radius,而 **AI 在业务上下文不明朗时对中高风险修复的后果无法担责**。一个被橡皮图章批准的 medium/high plan,是当前设计里最不对称的风险点。

原计划 P3 是飞书卡片远程审批,但它只会把「点头执行」搬得更远、更不可控(评估见 `design.md` ADR)。正确方向不是让审批更花哨,而是**按风险分级决定 AI 代不代做**:琐碎的低风险自动修,有后果的中高风险只产出一份审过的 runbook、由人自己执行。这同时把 P2 引入的真实写攻击面收敛到只剩 low 风险。

## 变更内容

- **风险分级执行策略**(改写执行语义):
  - **low 风险 plan**:维持现有自动执行闭环——经本地 `ApprovalGate` 审批后由 `Executor` 执行(倒序 rollback + 两段式 audit 不变)。
  - **medium / high 风险 plan**:**只出方案、AI 不代做**。判据 `any(step.risk_level in {"medium","high"})`。这类 plan **不进审批门、不进 `Executor`、不写 audit**,改为渲染成人读 **runbook**(含 precheck/forward/verify/rollback 四段可复制命令)交给人自己在目标机执行。
- **BREAKING**(对外行为):`hostlens fix <medium-or-high-plan>` 不再执行,改为打印 runbook 并以提示性退出码结束。P2 「high-risk 交互双确认后执行」的行为被「high-risk 不代执行」取代。
- **新增 runbook 渲染**:Jinja2 模板把 medium/high plan 渲染为结构化人读 runbook,命令经 `core/redact.py` best-effort 脱敏,**纯本地 stdout / 文件输出,不推任何通道**。
- **正式放弃飞书/任何远程审批**:`design.md` 写 ADR 记录评估与否决,结论——本地 `ApprovalGate` 即受控修复的审批终态;原登记的 P3 follow-up `add-remediation-lark-approval` 标记为「评估后不实施」。

## 功能 (Capabilities)

### 新增功能
- `remediation-runbook`: 把含 medium/high step 的 `RemediationPlan` 渲染成人读 Markdown runbook(四段命令 + 风险标注 + 脱敏 + 「请人工执行」语义),纯本地输出、不执行、不写 audit、不推通道。

### 修改功能
- `remediation-execution-workflow`: `Executor` / `hostlens fix` 新增风险分流——含 medium/high step 的 plan 被分流到 runbook(不执行);仅全 low plan 进 `ApprovalGate` → `Executor`。`ApprovalGate` 的 high-risk 双确认分支因 high 永不进执行路径而被「不代执行」语义取代。

## 影响

- **新增代码**:`src/hostlens/remediation/runbook.py`(渲染)+ `remediation/templates/runbook.md.j2`(Jinja2)+ 对应测试;`remediation/executor.py` 或 `cli/fix.py` 增分流门;`design.md` 的远程审批 ADR。
- **对外契约影响**:
  - **CLI**:`hostlens fix` 行为变更——medium/high plan 从「审批后执行」变「输出 runbook 不执行」(BREAKING,但 M9 未发版,无下游)。不新增子命令。
  - **不改** Agent tool schema / MCP tool schema(执行永不暴露给 Agent / MCP,只读红线不变)、Inspector schema、Notifier Protocol、Schedule manifest。
  - **不改** `remediation-plan-schema`(`risk_level` 三值阶梯不变)、`remediation-planner-agent`(Planner 仍产出含各风险级 step 的完整 plan;分级是执行侧策略,不是规划侧)。
- **依赖**:无新增运行时依赖(Jinja2 已在栈内)。

## Non-Goals(非目标)

- ❌ **不做飞书 / 钉钉 / 任何远程审批**——本地 `ApprovalGate` 即审批终态(理由见 `design.md` ADR)。
- ❌ **不引入任何长连接(出站/入站)或入站 HTTP server**——Hostlens 一贯 stdio-only、无 daemon 持有外部会话。
- ❌ **low 风险不退化成「全 runbook」**——low 保留自动执行闭环;本变更不削弱已交付的 low 执行能力。
- ❌ **不改 Planner**——仍产出含各风险级 step 的完整 plan;若 plan 含 medium/high step 即整 plan 走 runbook,不在规划侧做风险过滤。
- ❌ **不改执行不暴露给 Agent/MCP 的红线**——runbook 也只是本地 CLI 输出,不上 MCP、不进 Agent loop。
- ❌ **runbook 不推送任何通道**——纯本地 stdout / 文件,不经 Notifier(避免命令明文进飞书群等不可控历史)。
- ❌ **不做 medium/high 的「降级为 low 后执行」**——不提供把高风险 step 拆/降成可执行的机制;风险级由 Planner 判定,执行侧只读不改。

## 对外契约影响

唯一对外契约变化:**1 个 CLI 命令 `hostlens fix` 的行为分级**(medium/high 不再执行)。M9 受控修复尚未发版,无外部下游依赖此行为,故 BREAKING 影响限于内部 demo / 测试。无 schema / Protocol / manifest 破坏。

## Failure Modes(故障模式与降级)

1. **plan 风险分类边界错判**:plan 含一个 medium step + 多个 low step → 必须整 plan 走 runbook(取 max risk)。降级:分类用 `any(step.risk_level in {"medium","high"})`,保守偏向 runbook(宁可不执行也不误执行高风险)。
2. **runbook 模板渲染失败**(Jinja2 异常 / plan 字段缺失):medium/high plan 无法渲染。降级:渲染异常 fail-closed——打印结构化错误 + 非零退出,**绝不 fallback 到执行**。
3. **runbook 脱敏漏掉 flag 形密钥**:与 P2 audit 同源残留面(`redact_text` 不覆盖 `mysql -p<pw>`)。降级:runbook 仍走 `redact_text`,文档诚实声明残留;且 runbook 仅本地输出(不进飞书),泄漏面小于远程审批。
4. **low plan 被误判为含高风险**:因 schema 校验保证 risk_level ∈ {low,medium,high},不存在未知值;若 Planner 产出全 low 则正常进执行路径。降级:无未知风险值分支(schema 已 fail-closed)。
5. **medium/high plan 的 runbook 被人误当成「已执行」**:人读 runbook 后忘了自己执行。降级:runbook 顶部显式标注「本工具未执行任何命令,请人工在目标机执行」+ 退出码区分(非 0 的提示码)。

## Operational Limits(运行约束)

- **并发**:无新增并发;runbook 渲染是纯 CPU 同步操作(Jinja2),走 `asyncio.to_thread` 或直接同步渲染,无 IO。
- **内存**:runbook 字符串与 plan 同量级(KB 级),无新增预算。
- **超时**:runbook 路径无远程 IO、无 LLM 调用,无超时设置需求;low 执行路径的 `per_step_timeout` 不变。

## Security & Secrets(安全与密钥)

- **不引入新密钥**:无远程审批 = 无飞书 Event verification token / encrypt key,无新凭据面。
- **攻击面收缩**:P2 的真实写攻击面(任意 shell 经 `exec`)被砍到只剩 low 风险;medium/high 不再由本工具执行,blast radius 大幅缩小。
- **脱敏**:runbook 命令经 `core/redact.py` best-effort 脱敏;残留 flag 形密钥面与 P2 同,诚实声明;runbook 纯本地不外发,泄漏面严格小于远程审批方案。
- **非 root**:runbook 路径不执行任何命令,EUID==0 拒绝门对 low 执行路径不变(继承全局写操作拒 root)。

## Cost / Quota Impact(成本/配额影响)

- **零 LLM 调用**:runbook 渲染是确定性模板渲染,不调 Anthropic API,不消耗 token,不影响配额。Planner 产 plan 的成本不变(本变更不碰规划侧)。

## Demo Path(5 分钟本地复现,无 SSH / 无付费 API)

```bash
# 1. 低风险 plan:走现有审批+执行闭环(dry-run 即可演示零执行)
hostlens fix tests/fixtures/remediation/low-plan.json            # 默认 dry-run:展示三元组、零执行

# 2. 中/高风险 plan:输出人读 runbook、零执行、不写 audit
hostlens fix tests/fixtures/remediation/medium-plan.json         # 打印 runbook + 「请人工执行」提示,非零提示退出码
hostlens fix tests/fixtures/remediation/high-plan.json           # 同上;high 不再走双确认执行

# 3. 测试全绿
pytest tests/remediation/test_runbook.py tests/cli/test_fix.py -v
```
