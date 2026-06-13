# mcp-management-tools 规范

## 目的
待定 - 由归档变更 add-mcp-readonly-management-tools 创建。归档后请更新目的。
## 需求
### 需求:7 个只读管控工具必须注册为 ToolSpec 并显式声明 surface / side_effects / sensitive_output

本能力必须新增 7 个只读 `ToolSpec`，每个 `surfaces` 必须恰为 `{"agent", "mcp"}`、`requires_approval` 必须为 `False`、`side_effects` 必须 ∈ `{none, read}`，且 `sensitive_output` 必须**显式声明**（禁止留 `None`）。每个工具必须分别撰写 `mcp_description`（面向远程 LLM）与 `agent_description`（面向本地 loop），**禁止**两者共用同一字符串。

七个工具与其策略元数据：

| 工具 | side_effects | sensitive_output | 说明 |
|---|---|---|---|
| `list_schedules` | none | True | 列出 `schedules/*.yaml` manifest：`name` / schedule 表达式 / `next_fire_time`（由 manifest 的 trigger + 当前时钟直接以 apscheduler `CronTrigger`/`IntervalTrigger` 计算，**禁止 import `cli/` 私有符号**如 `_next_fire_time`——`tools/` 不得反向依赖 `cli/` 层）/ `targets` / `intent` / `notify`（每条绑定的 `channel` + `only_if` 路由表达式——兑现「路由可见性由 list_schedules 暴露」承诺；`only_if` 是 manifest 文本、非 secret）。**M4 无 schedule 级 enabled 概念**——所有加载的 manifest 均为活动态，禁止输出不存在的 `enabled` 字段 |
| `get_schedule_status` | none | True | 最近 N 次 Run 留痕（ledger `run_id` / 触发时间 / 目标 / inspector 集合 / `report_id` / report_hash / notify 结果） |
| `run_schedule_now` | read | True | 跑 schedule 绑定诊断 pipeline + 持久化 Report，**不发 notify** |
| `list_channels` | none | True | `notifiers.yaml` 通道 `name`（实例 key）/ `type`（**禁返回 token/secret**；通道无 `enabled` 字段；`only_if` 不属通道——它是 per-schedule 的 notify 绑定，由 `list_schedules` 暴露） |
| `list_reports` | none | True | 历史报告列表（`target` **必填**，匹配 `ReportStore.list_runs(target_id)` + `limit`） |
| `show_report` | none | True | 取回单份 Report（含 findings / hypotheses） |
| `diff_reports` | read | True | 两份报告的 regression diff |

`run_schedule_now` 的 `side_effects` 必须**静态**标 `read`（跑只读 inspector + 本地持久化，无 host/外部状态变更），**禁止**通过任何「是否发通知」参数让其在 read/write 之间切换——发通知能力不属本能力（属写期 `notify_report`）。

#### 场景:7 个工具均显式声明 sensitive_output

- **当** 装配完成后遍历这 7 个工具的 `ToolSpec`
- **那么** 每个的 `sensitive_output` 必须为 `True`（非 `None`），`surfaces == {"agent","mcp"}`，`requires_approval is False`，`side_effects in {"none","read"}`

#### 场景:mcp_description 与 agent_description 不共用

- **当** 遍历 7 个工具
- **那么** 每个工具的 `mcp_description` 与 `agent_description` 必须均为非空且**互不相等**的字符串

#### 场景:经现有 fail-closed 投影自检

- **当** 以含本 7 个工具的 registry 调 `build_server` 的 eager `list_for_mcp` 自检
- **那么** 必须不 raise（7 个工具 `sensitive_output` 均已声明），且投影出的 MCP tool 定义含全部 7 个工具

### 需求:管控工具依赖必须经注册期闭包注入，禁止扩 ToolContext

本能力必须新增显式装配函数 `register_mcp_management_tools(registry, *, deps)`，`deps` 为一个 frozen 依赖容器（如 `ManagementToolDeps`），携带 scheduler / report / notifier 侧依赖（schedule manifest 加载器、Run store、Report store、通道摘要加载器、runner 工厂）。handler 必须从该闭包注入的 `deps` 取这些依赖，**禁止**从 module-level / global singleton 取，也**禁止**为此向 `ToolContext` 增加字段——`ToolContext` 必须保持 ADR-008 锁死的 6 字段集（`target_registry` / `inspector_registry` / `config` / `logger` / `approval_service` / `cancel`）不变。

依赖容器**禁止**引用不存在的类型：现有 notifier 加载器是 `load_channels(settings, registry) -> dict[str, Notifier]`（会把 `${ENV_VAR}` 展开成**明文 secret**注入每个 Notifier 实例的 config），它**不适合**直接喂给 `list_channels`。故 `list_channels` 的依赖必须是一个**只读 raw 通道摘要加载器**——直接解析 `notifiers.yaml` 原文（复用既有 `_parse_yaml` 等价逻辑），每次 fresh-load 仅取每个通道的 `name`（实例 key）与 `type`，**不经** `load_channels` 的 `${ENV_VAR}` 展开路径，从源头杜绝 secret 进入输出。

`run_schedule_now` 的 runner 工厂（类型为产出 `SchedulerRunner` 的工厂，**不是** `ScheduleRunner`——后者类名不存在）必须在 handler 调用时接受当次 `ToolContext`（以取 `target_registry` / `inspector_registry`）与已加载 manifests，把 ctx 侧依赖与闭包侧依赖在调用点汇合构造 runner。

#### 场景:ToolContext 字段集不变

- **当** 本能力落地后检查 `ToolContext` 的字段
- **那么** 必须恰为 6 个字段（`target_registry` / `inspector_registry` / `config` / `logger` / `approval_service` / `cancel`），**禁止**出现 `schedule_store` / `report_store` / `notifier_config` 等新字段

#### 场景:handler 依赖来自注入而非全局

- **当** 用注入了 fake `deps`（fake Run store / fake Report store）的装配构造 registry 并 dispatch `list_reports`
- **那么** handler 必须返回 fake Report store 的内容（证明依赖来自注入），过程中**禁止** import 任何 module-level store singleton

### 需求:`run_schedule_now` 必须复用 runner 抑制 notify 并返回可追溯 report_id

`run_schedule_now(name)` 必须复用 scheduler runner 的触发路径并显式抑制 notify 派发（runner 以 `dispatch_notify=False` 调用，见 `scheduler-engine`），即：跑 schedule 绑定的诊断 pipeline、持久化 Report、记录 Run，但**禁止**向任何通道发送。

**ID 命名契约（防跨工具误用，对全部发出该键的工具统一）**：scheduler ledger 的 `Run.run_id` 与 report-store 键是**两个不同的标识符**——`ReportStore.get_run(...)`（`show_report` / `diff_reports` 的查询键）消费的是 **report-store 键**，在 scheduler `Run` 上以 `report_id` 字段暴露（即 `Run.report_id`，等于 `ReportStore.save` 返回的键、等于 `Report.meta.run_id`）。因此本能力所有输出该键的工具必须把它命名为 **`report_id`**：
- `run_schedule_now` 输出必须含 `report_id`（report-store 键，**供 `show_report` / `diff_reports` 复用**）、ledger `run_id`、与 `status`（产出 Report 时 `report_id` 非空）。
- `get_schedule_status` 输出每条 Run 必须同时含 ledger `run_id`（scheduler 留痕标识，**非** `show_report` 的有效键）与 `report_id`（report-store 键，供 `show_report` 复用）。无-Report 状态的 Run（`failed_*` / `missed` / `skipped_due_to_running` / `daemon_stopped` / `budget_exhausted`）其 `report_id`（及 `report_hash`）为 `None`，output schema 必须容 nullable。
- `list_reports` 复用 `ReportStore.list_runs` 返回的 `RunIndexRow`，其字段名虽为 `run_id`，但该值**实为 report-store 键**（= `meta.run_id` = `show_report` 的有效键，**不是** scheduler ledger `run_id`）；故 `list_reports` 输出**必须把该 id 以 `report_id` 命名暴露**，避免与 `get_schedule_status` 的 ledger `run_id` 混淆而复活 ID 陷阱。
- 输出文案 / `mcp_description` 必须明确指示远程 LLM **据 `report_id`（而非 ledger `run_id`）继续调 `show_report`**，否则用 ledger `run_id` 调 `show_report` 会命中 not-found。

`name` 不存在于已加载 manifests 时，handler 必须在构造 runner / 触发 pipeline **之前**自行前置检查（fresh-load manifests 后判定），返回结构化 not-found 错误信封，**禁止**直接复用 runner `trigger` 抛出的裸 `KeyError`（`trigger` 对未知 name 在 `if name not in self._manifests` 处直接 `raise KeyError`，该 `KeyError` 经 `McpToolsAdapter.dispatch` 原样透传——绕过 dispatch 的 `is_error` 结构化信封；虽 server 层 `handle_call_tool` 仍会脱敏文本，但信封形状非结构化 not-found）、**禁止**触发任何 pipeline。

**fresh-load 失败与两段式校验**：`load_manifests`（= `load_schedules(_SCHEDULES_DIR, target_registry)`）在加载期校验的是 **target 存在性**（`target_name not in target_registry` → `ConfigError`）、manifest 形状、以及 `only_if` 的 **DSL 语法**——但**不校验 notify channel 存在性**（`load_schedules` 不读 `notifiers.yaml`，`schedule list` 故不依赖通道配置；**channel 存在性在 runner 装配期 `_validate_notify_channels` 校验**，见下）。因此：
- **target 校验**：「schedule 引用了已不在 registry 的 target」会在 fresh-load 即 `ConfigError`、**早于** `trigger` / `_run_job`——runner `_run_job` 内 `target_registry.get` 的 `KeyError` 路径在本工具下**不可达**（load 校验与 `_run_job` 查询用同一 per-serve 静态 registry）。
- **channel 校验时机**：**未知 notify channel 不在 fresh-load 失败**——`list_schedules`（只 `load_manifests`、从不 build runner）对引用未知 channel 的 manifest **正常加载、不抛错**；该错误仅在 `run_schedule_now` 的 `build_runner`（runner `__init__` → `_validate_notify_channels`）阶段以 `ConfigError` 触发。
- 凡 fresh-load 抛 `ConfigError`（manifest 畸形 / 未知 target / `only_if` 语法错），或 `build_runner` 抛 `ConfigError`（未知 channel），handler 都必须作为结构化错误信封返回（`dispatch` 的通用 `except Exception` 包装已保证不裸传，handler 无需特判，但不得在失败时静默成功或触发 pipeline）。

#### 场景:触发已存在 schedule 跑通且不发通知

- **当** dispatch `run_schedule_now` 指向一个配了 notify 通道、cassette 回放产出 ok Report 的 schedule
- **那么** 必须持久化 Report、返回非空 `report_id`（report-store 键）与 ledger `run_id`、`status` 为 ok/partial，且该 Run 的 `notify_results == []`（无任何通道发送）

#### 场景:run_schedule_now 的 report_id 可直接喂给 show_report

- **当** `run_schedule_now` 返回 `report_id` 后，以该 `report_id` dispatch `show_report`
- **那么** `show_report` 必须返回对应 Report（证明输出的 `report_id` 是 `ReportStore.get_run` 的有效键，而 ledger `run_id` 不是）

#### 场景:未知 schedule 名返回结构化 not-found

- **当** dispatch `run_schedule_now` 指向不在已加载 manifests 的 name
- **那么** handler 必须在触发前前置检查命中、返回结构化 not-found 错误信封（经脱敏），**禁止**抛裸异常、**禁止**让 runner `trigger` 的 `KeyError` 透传、**禁止**触发任何 pipeline

### 需求:`list_channels` 必须脱敏，禁止泄露凭据

`list_channels` 必须仅返回每个通道的 `name`（`notifiers.yaml` 实例 key）与 `type`，**禁止**返回 bot token / webhook secret / 签名密钥等任何凭据或 `${ENV_VAR}` 展开后的值。为此 handler **禁止**复用 `load_channels`（它在加载时已把 `${ENV_VAR}` 展开成明文 secret 写进 Notifier 实例 config），必须改走只读 raw 通道摘要加载器（仅解析原文取 `name` / `type`）。`get_schedule_status` 的 `notify_results` 必须复用既有 `redact_secret_text` 脱敏。

通道配置不含 `enabled` 字段、`only_if` 不属通道（它是 `schedules/*.yaml` 内 `manifest.notify[]` 的 per-schedule 绑定，由 `list_schedules` 暴露），故 `list_channels` 输出**禁止**声称返回 `enabled` 或 `only_if`。

#### 场景:list_channels 输出恰为 name/type 白名单

- **当** `notifiers.yaml` 配了一个含 `bot_token: ${TG_TOKEN}` / `webhook_url: ${HOOK}` / `secret: ${SIGN}` 等密钥键的 telegram/lark 通道，dispatch `list_channels`
- **那么** 输出的每条通道必须**恰含且仅含** `name`（实例 key）与 `type` 两字段（正向白名单，非「禁 token」黑名单——`ChannelSummary` 模型应以 `extra="forbid"` 封死形状）；测试必须断言**任何其它 raw entry 键**（`bot_token` / `webhook_url` / `secret` / `chat_id` 等）**及其 `${ENV_VAR}` 字面量本身**（如字符串 `"${TG_TOKEN}"`，它既非明文 token 也非展开值，黑名单断言会漏放）均**不出现**在输出，亦不含 `enabled` / `only_if` 字段

### 需求:查询类工具对空 store / 不存在 id 必须返回结构化结果而非崩溃

`list_schedules` / `list_reports` / `get_schedule_status` 在无对应 store（首次运行无 `runs.db` / `reports.db`）或空结果时必须返回空列表，**禁止**抛异常。`show_report` / `diff_reports` 对不存在的 `report_id` 必须返回结构化 not-found 错误信封（经脱敏，**禁止**泄露内部文件路径）；`diff_reports` 在两份报告 `target_id` 不一致（`reporting/diff.compute_diff` 抛 `ValueError`）时必须返回结构化错误信封（语义对齐 `cli/reports.py` 的 `_compute_diff_or_exit`，但**禁止 import 该 cli 私有符号**——它 `raise typer.Exit`，MCP 进程内不得用；handler 直接调 `reporting/diff.compute_diff` 并自捕 `ValueError`），**禁止**让裸 `ValueError` 透传。注：`compute_diff` 对 schema 变更 / baseline 非 ok 等是**返回**带 `diff_skipped_reason` 的 `RegressionDiff`（不 raise），仅跨 `target_id` raise `ValueError`——故只此一种 raise 路径需自捕。`get_schedule_status` 的 `limit` 默认必须为 10、上限 100（既有 `RunStore.list_recent` 默认 20、无上限钳制，故该默认值与上限钳制必须由 handler 层实现，不依赖 store）；`list_reports` 的 `limit` 默认必须为 20，**不设上限钳制**（沿用 `ReportStore.list_runs` 直透；单 target 列表查询体量小——与 `get_schedule_status` 设上限的差异是有意决定，非遗漏）。

**handler 表达 not-found 的异常类型约束**：handler 须以**结构化结果**或一个会被 `dispatch` 通用 `except Exception` 包成脱敏 `is_error` 信封的异常（如 plain `ToolError`，仿 `run_inspector` 既有先例）表达 not-found，**禁止**用 `ToolPolicyViolation`（`ToolError` 子类，被 `dispatch` 在通用 except 前 `raise` 透传）或 `KeyError`（同被原样透传）——否则绕过 `is_error` 结构化信封。

#### 场景:空 store 返回空列表

- **当** 无 `reports.db` 时以一个具体 `target` dispatch `list_reports`
- **那么** 必须返回空列表，**禁止**抛异常

#### 场景:show_report 不存在 id 返回结构化 not-found

- **当** dispatch `show_report` 指向不存在的 `report_id`
- **那么** 必须返回结构化 not-found 错误信封，消息经脱敏且**不含**内部文件路径

#### 场景:diff_reports 跨 target 返回结构化错误

- **当** dispatch `diff_reports` 指向两份 `target_id` 不一致的报告（`compute_diff` 抛 `ValueError`）
- **那么** 必须返回结构化错误信封（经脱敏），**禁止**让裸 `ValueError` 透传出 handler

#### 场景:diff_reports 任一 report_id 不存在返回结构化 not-found

- **当** dispatch `diff_reports` 的 `report_id_a` 或 `report_id_b` 任一不存在（对应 `ReportStore.get_run` 返回 `None`）
- **那么** 必须返回结构化 not-found 错误信封（经脱敏、不含内部文件路径），**禁止**抛裸异常、**禁止**调 `compute_diff`
