# remediation-runbook 规范

## 目的
待定 - 由归档变更 add-risk-tiered-remediation-execution 创建。归档后请更新目的。
## 需求
### 需求:runbook 必须把含 medium/high step 的 plan 确定性渲染为人读 Markdown，纯本地、零执行、零 audit、不推任何通道

系统必须提供 runbook 渲染能力：输入一个含 `risk_level ∈ {"medium","high"}` step 的 `RemediationPlan`，输出一份**人读 Markdown runbook**，交给人自己在目标机执行。渲染必须**确定性**（Jinja2 模板，无 LLM、无随机、无远程 IO），且：

- **顶部必须有显式横幅**，声明「本工具未执行任何命令——中高风险修复请人工在目标机执行，执行后自行验证，出事用 rollback 段回退」。
- **必须逐 step 渲染四段命令**（`precheck` / `forward` / `verify` / `rollback`）为可复制命令块，并标注每 step 的 `risk_level`；`precheck_cmd` / `rollback_cmd` 为 `None` 时显式标注「无」。
- **命令必须经 `core/redact.py` `redact_text` best-effort 脱敏**后再渲染（与 audit 同源）；**已知残留**：`redact_text` 不覆盖 flag 形密钥（`mysql -p<pw>` 等），文档须诚实声明，建议 plan 作者走 `exec` 的 `env` 注入。
- **禁止执行任何命令**：runbook 渲染路径绝不调用 `ExecutionTarget.exec` / `Executor` / 任何 `CommandRunner`。
- **禁止写 audit.log**：runbook 不是真实执行事件，不得污染取证日志。
- **禁止经任何 Notifier / 通道外发**：纯本地 stdout（或显式 `--out` 落盘），命令明文绝不进飞书等不可控消息历史。

#### 场景:medium/high plan 渲染含四段命令与风险标注
- **当** 渲染一个含至少一个 `risk_level ∈ {"medium","high"}` step 的 plan
- **那么** 输出 Markdown 含 plan 元信息（finding_id / target_name / rationale）与逐 step 的 precheck/forward/verify/rollback 可复制命令块及该 step 的 risk_level 标注

#### 场景:顶部横幅声明本工具未执行
- **当** 任意 runbook 被渲染
- **那么** 输出顶部必须含显式横幅，声明本工具未执行任何命令、须人工在目标机执行、出事用 rollback 段回退

#### 场景:命令脱敏后渲染
- **当** plan 的 `forward_cmd` 含 `redact_text` 可识别形态的敏感串（`key=value` / `Bearer` / JWT / `sk-`）
- **那么** runbook 中该串已脱敏；flag 形密钥（`-p<pw>` 等）属已知残留，不被覆盖

#### 场景:渲染路径零执行零 audit 不推通道
- **当** 一个 medium/high plan 被渲染成 runbook
- **那么** 全程不调用任何 `ExecutionTarget.exec` / `Executor` / `CommandRunner`、不写 audit.log、不经任何 Notifier；副作用仅为本地输出文本

#### 场景:渲染确定性无 LLM
- **当** 对同一 plan 重复渲染
- **那么** 输出逐字一致（纯模板渲染，不调 LLM、无随机、无远程 IO）

#### 场景:渲染失败 fail-closed 不回退到执行
- **当** runbook 模板渲染抛异常（如 plan 字段异常）
- **那么** 打印单行结构化错误并以非零退出，**绝不** fallback 到执行该 plan
