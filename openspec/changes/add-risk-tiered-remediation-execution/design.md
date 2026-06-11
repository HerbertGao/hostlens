## Context

M9 受控修复 P1/P1b/P2 已交付:`RemediationPlan` schema、Planner、`Executor`(顺序执行 + 倒序 rollback + 三态结果)、本地 `ApprovalGate`(y/N + `--yes` + high-risk 手输 finding_id 双确认)、append-only 两段式 audit、`hostlens fix` CLI(EUID 拒 → load → preview → 审批 → execute,默认 dry-run)。

P2 把执行权放给「过审批门的任意风险级 plan」,high-risk 仅多一道双确认。原计划 P3 = 飞书卡片远程审批,在此 `ApprovalGate` 上扩远程触发。

本设计推翻 P3 的远程审批方向,改为引入**风险分级执行策略**:`Executor` 只代执行 low 风险;medium/high 渲染成人读 runbook 由人自己执行。约束:沿用全部 P2 不变量(执行不暴露 Agent/MCP、Remediation 自成子系统不进 Tool Registry、`ToolContext.ApprovalService` 永久 Noop)。

## Goals / Non-Goals

**Goals**
- 让 `hostlens fix` 按 plan 风险分流:全 low → 审批后执行;含 medium/high → 渲染 runbook、零执行。
- 新增确定性 runbook 渲染(Jinja2),纯本地输出,命令脱敏。
- 用 ADR 正式记录「放弃飞书/任何远程审批」的评估与结论,使形态翻转留痕。

**Non-Goals**(详见 proposal Non-Goals)
- 不做任何远程审批 / 长连接 / 入站 server。
- 不削弱 low 执行闭环;不改 Planner / plan schema;不在规划侧做风险过滤。

## Decisions

### D1:风险分流发生在审批门之前,以 plan 级 max risk 判定

`hostlens fix` 编排序列改为:

```
EUID==0 拒(最早门,不变)
   → load_json(plan)
   → 分类:has_elevated = any(step.risk_level in {"medium","high"})
   → has_elevated?
        是 → 渲染 runbook 到 stdout/文件 → 提示「请人工执行」→ 退出(提示码,非 0)
              [不进 ApprovalGate / 不进 Executor / 不写 audit / 不触发任何 exec]
        否(全 low)→ preview → ApprovalGate → Executor.execute → 两段 audit(P2 闭环不变)
```

- **plan 级风险 = steps 的 max**:任一 step 为 medium/high,整 plan 走 runbook。理由:rollback 是按 plan 倒序联动的,无法只执行 plan 里的 low step 而跳过 medium step——step 之间有顺序依赖,部分执行会破坏 plan 语义。保守取 max 是唯一安全选择。
- **分流先于审批门**:medium/high 根本不进审批门。理由:审批门的存在意义是「授权执行」,而 medium/high 的结论是「不执行」,让它进审批门再拒绝是语义噪音;且分流早 = runbook 路径零执行风险面最小。

**替代方案(否决)**:在审批门内对 medium/high 返回「拒绝执行」。否决——把「不代执行」表达成「审批拒绝」会污染 `ApprovalRejected` 的语义(它是安全门refusal,不是策略分流),且仍让 medium/high 进了执行编排的内圈。分流应在编排外层。

### D2:`ApprovalGate` 的 high-risk 双确认分支移除,语义由「不代执行」取代

D1 后,审批门只会见到全 low plan(medium/high 已在上游分流走)。`approval.py` 现有的 `high_risk = any(... == "high")` 双确认分支变**不可达死代码**。

**决策:移除 high-risk 双确认分支**,`ApprovalGate` 简化为「全 low plan 的 y/N + `--yes`」。high-risk 的安全保证从「双确认后执行」升级为「根本不代执行」(更强)。spec 显式声明这一语义迁移。

**替代方案(否决)**:留双确认作 defense-in-depth。否决——它在 D1 下永不触发,留着是死代码(违反项目「不写不可能分支的兜底」§6),且会让读者误以为 high-risk 仍可执行。移除 + spec 说明比留死代码诚实。

> 注:`ApprovalGate` 仍保留 non-TTY 缺 `--yes` → reject、`--yes` 跳过 y/N 等 low 路径行为。仅移除 high-risk 那一段。

### D3:runbook 是独立 capability `remediation-runbook`,纯本地确定性渲染

新增 `remediation/runbook.py` + `remediation/templates/runbook.md.j2`:
- 输入:含 medium/high step 的 `RemediationPlan`;输出:Markdown 字符串(本地 stdout 或 `--out <file>`)。
- 内容:plan 元信息(finding_id / target / rationale / 风险标注)+ 逐 step 四段(precheck / forward / verify / rollback)**可复制命令块**;顶部显式横幅「⚠ 本工具未执行任何命令——中高风险修复请人工在目标机执行,执行后自行验证,出事用 rollback 段回退」。
- **脱敏**:命令经 `core/redact.py` `redact_text`(复用 P2 同源 best-effort);残留 flag 形密钥面诚实声明(同 audit)。
- **确定性**:无 LLM、无随机、无远程 IO——纯模板渲染。
- **不推任何通道**:不经 Notifier,只本地输出。理由:命令明文若进飞书群会留在不可控消息历史(这正是远程审批被否决的泄漏面之一)。

**为何独立 capability 而非并入 execution-workflow**:渲染契约(模板结构、脱敏、横幅语义)与执行契约(顺序、rollback、三态)关注点正交,且 runbook 零执行、零 audit、零审批——把它和「写操作执行」混在一个 spec 会让「runbook 不执行」这条最重要的不变量淹没在执行需求里。独立 spec 让「runbook 永不执行」成为可单独测试的一等需求。

### D4:退出码语义——runbook 路径用专属提示码区分「未执行」

`hostlens fix <medium/high plan>` 渲染 runbook 后**以非 0 提示码退出**(区别于 low 执行成功的 0 / 执行失败的 1 / 参数错误的 3)。理由:脚本 / CI / Agent ping 能机械区分「这个 plan 没被执行,需要人接手」与「执行成功」。具体码值在 spec 场景钉死,保持与现有 `cli/__init__.py` 退出码体系一致(0 成功 / 1 业务失败 / 2 或 3 参数错)。

## ADR:放弃飞书 / 任何远程审批(P3 形态翻转记录)

> 这是本变更存在的核心决策,单列为 ADR 供后续读者理解为何 `add-remediation-lark-approval` 不实施。

**状态**:Accepted(取代原 P3 follow-up `add-remediation-lark-approval`,后者标记为「评估后不实施」)。

**背景**:原 P3 拟在本地 `ApprovalGate` 上扩飞书卡片远程审批。经两轮架构评估:

**评估到的三条形态及其否决理由**:

| 形态 | 机制 | 否决理由 |
|---|---|---|
| **A 入站 webhook** | 飞书点击 POST 到公网回调 URL | 需常驻入站 HTTP server + 公网可达 / 内网穿透——破 Hostlens 一贯 stdio-only、无入站基调 |
| **D 出站长连接** | `fix` 进程持有 lark-oapi WebSocket,阻塞等卡片回调 | 技术最优(零入站 + 实时),但**负责人对 fix 进程持有公网长连接有洁癖**;否决 |
| **P 轮询飞书审批 OpenAPI** | 创建审批实例 + 轮询状态 | 飞书官方明确建议「订阅事件而非轮询」(逆设计);需先在飞书后台配审批定义(破坏「加通道=加文件」基调);fix 进程被迫长命挂着等人,与「fix 是秒级本地命令」对冲 |

**压垮性论点**:本变更把 **medium 也划入「只给方案、AI 不代做」**后,远程审批合法域只剩 **low 风险**(high 早已永久禁止远程,见下)。为本就安全的 low 风险单独建一套飞书审批仪式 = 纯过度设计。远程审批失去存在理由。

**high-risk 永久禁止远程的独立理由**(即使将来重启远程审批也成立):本地 high-risk 双确认(手输 finding_id)的安全性来自 **proof-of-presence**——证明操作者正坐在执行机终端前、有 shell 访问权。远程点按钮只证明「某个有飞书的人点了」,无法复现该语义,等于把删库级操作授权降级为「点一下」,是净安全损失(攻击者只需钓到一个飞书账号,而非同时拿到执行机 shell)。

**结论**:本地 `ApprovalGate` 即受控修复的审批终态。远程审批不进 M9。若将来确有远程审批诉求,须另起独立提案并解决 proof-of-presence、审批者身份白名单 + 回调验签、命令明文不外发等问题,且永不覆盖 high-risk。

## Risks / Trade-offs

- [runbook 脱敏漏 flag 形密钥(`mysql -p<pw>`)] → 与 P2 audit 同源残留;runbook 仅本地输出不外发,泄漏面严格小于远程审批;文档诚实声明,建议 plan 作者走 `exec` 的 `env` 注入而非命令明文。
- [人读完 runbook 忘了自己执行,误以为已修] → runbook 顶部横幅显式「本工具未执行」+ 非 0 提示退出码;不做自动追踪(那会变成 B 状态机,已在 ADR 否决)。
- [medium/high plan 永远无法经本工具执行,运维觉得不便] → 这是刻意的安全取舍(AI 担不了责);low 自动修 + 中高出 runbook 的产品叙事清晰;接受。
- [移除 high-risk 双确认后,若将来想恢复 high 执行,需重写审批门] → 接受;恢复 high 执行本就应是独立提案 + 更强审批模型,不应靠保留死代码预留。

## Migration Plan

- M9 受控修复未发版,无外部下游依赖 `hostlens fix` 对 medium/high 的执行行为,故无破坏性迁移。
- 现有 P2 测试中「high-risk 交互双确认后执行」的用例需改写为「high-risk 走 runbook 不执行」;「ApprovalGate high-risk 双确认」相关用例随 D2 移除/改写。
- 回滚策略:本变更是策略收紧(更安全),无需运行时回滚开关;若要恢复 P2 行为即 revert 本 change。

## Open Questions

- runbook 是否需要 `--out <file>` 落盘选项,还是只 stdout?(倾向:MVP 先 stdout,落盘作薄 follow-up;由 tasks 决定是否纳入本次。)
- runbook 提示退出码取值(例如 4)是否与未来其他「非执行性结果」码冲突?(由 spec 场景钉死,需扫一遍 `cli/__init__.py` 现有码位。)
