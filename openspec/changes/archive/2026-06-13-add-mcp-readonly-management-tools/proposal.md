## 为什么

M7（`add-mcp-server-surface`，已归档）把 Hostlens 暴露成 MCP Server，但只接了**只读三件套** `list_inspectors` / `list_targets` / `run_inspector`。AI 助手（Claude Code / Cursor 等远程 LLM）想看「这台机器配了哪些定时巡检」「上次 schedule 跑出了什么」「历史报告里这次相比上次新增了哪些问题」「通知发到哪些渠道」，目前只能让用户切回终端敲 `hostlens schedule/notify/reports` CLI。诊断回路被人为割裂。

本提案是 **M7-ext 读期**：把 7 个**只读**管控能力接进 MCP surface，让远程 LLM 在一个对话里完成「看调度 → 跑整包巡检 → 读报告 → 跑 regression diff」的闭环。写操作（加 target / 发通知 / 删 target）属于独立的写期提案 `add-mcp-write-approval-flow`，**不在本提案**。

关键前提：M7 落地的双层 capability + fail-closed 投影架构**对 registry 内容是泛型的**——`McpToolsAdapter` / `build_server` 不关心有几个工具，只要每个 `ToolSpec` 显式 `surfaces ∋ "mcp"` + 声明 `sensitive_output`。因此本提案**零闸门改动**：7 个新工具全部 `side_effects ∈ {none, read}` 且 `requires_approval=False`，天然过现有 `dispatch` 九步门，无需触碰 `mcp-server` / `mcp-tool-adapter`。

## 变更内容

- 新增 7 个只读 `ToolSpec`，全部 opt-in `surfaces={"agent","mcp"}`，显式声明 `sensitive_output`，各撰独立 `mcp_description` / `agent_description`：
  1. `list_schedules()` — schedule manifest 列表：name / schedule 表达式 / `next_fire_time`（由 manifest trigger 计算）/ targets / intent / notify 绑定（channel + only_if 路由，兑现「路由可见性由 list_schedules 暴露」）（**M4 无 schedule 级 enabled 概念，不输出 enabled**）
  2. `get_schedule_status(name?, limit?)` — 最近 N 次 Run 留痕（ledger `run_id` / 触发时间 / 目标 / inspector 集合 / `report_id` / report_hash / notify 结果）
  3. `run_schedule_now(name)` — 跑 schedule 绑定的诊断 pipeline（含 intent）+ 持久化 Report，**固定不发通知**；输出 `report_id`（供 `show_report` 复用）+ ledger `run_id` + status
  4. `list_channels()` — `notifiers.yaml` 通道 name（实例 key）/ type（**绝不返回 token/secret；通道无 enabled 字段；only_if 不属通道——它是 per-schedule 的 notify 绑定，由 `list_schedules` 暴露**）
  5. `list_reports(target, limit?)` — 单 target 历史报告列表（`target` **必填**，匹配既有 `ReportStore.list_runs(target_id)`；远程 LLM 可先 `list_targets` 枚举 target 再逐一查）
  6. `show_report(report_id)` — 取回单份 Report（含 findings / hypotheses）；`report_id` = `ReportStore.get_run` 的键（即 `run_schedule_now` / `get_schedule_status` 输出的 `report_id`）
  7. `diff_reports(report_id_a, report_id_b)` — regression diff（两份须同 target，跨 target 返结构化错误）
- 新增显式装配函数 `register_mcp_management_tools(registry, *, deps: ManagementToolDeps)`，按本仓既有 `register_default_tools(clock=…, collector=…)` 的**闭包注入**模式，把 scheduler / report / notifier 依赖在装配期绑进 spec factory，**`ToolContext` 6 字段冻结集不动**（ADR-008 纪律）。`deps` 携带 schedule manifest 加载器、`RunStore`、`ReportStore`、**只读 raw 通道摘要加载器**（仅取 name/type，不经 `load_channels` 的 `${ENV_VAR}` 展开）、产出 `SchedulerRunner` 的工厂；**不引用不存在的 `NotifierConfig` 类型**。
- `hostlens mcp serve` 在 `register_default_tools` 之后**追加调用** `register_mcp_management_tools`，从 `Settings` 构造所需 store/loader 依赖；构造管控工具 backend 工厂时强制走 daemon-safe 校验（防远程 LLM 经 `run_schedule_now` 驱动订阅 backend）。
- scheduler runner 新增 `dispatch_notify: bool = True`（keyword-only）抑制路径：默认 `True` 完全保留既有行为（daemon / `schedule trigger` CLI）；仅 `run_schedule_now` 传 `False`，跑完 pipeline + 持久化 Report 但跳过 notify 派发。**注意**：真实派发点在 `_map_outcome`（`trigger` 下游 4 层），且 timer 注册的是 `_run_job` 而非 `trigger`，故该参数须**逐层穿透** `trigger`→`_run_job`→`_finalize_outcome`→`_map_outcome` 直到 `_dispatch_notify` 调用点、**每层默认 `True`**——这是 timer 零变更的前提（见 `scheduler-engine` spec 的「参数穿透契约」）。`side_effects` 在 ToolSpec 上保持**静态** `read`，不靠参数在 read/write 间横跳。

## 功能 (Capabilities)

### 新增功能
- `mcp-management-tools`: MCP surface 上的管控工具集（本提案以 7 个只读工具填充）——含每个工具的 input/output Pydantic schema、`sensitive_output` 声明、双 description、以及 `register_mcp_management_tools` 闭包注入装配契约。

### 修改功能
- `scheduler-engine`: runner 的「Report 持久化后派发 notify」需求增加显式抑制路径（`dispatch_notify=False`），供 `run_schedule_now` 复用 pipeline 而不发外部通知；默认值保证既有 daemon / CLI 行为零变更。
- `mcp-cli-command`: `hostlens mcp serve` 装配的 registry 从「仅 `register_default_tools`」扩为「`register_default_tools` + `register_mcp_management_tools`」，并从 `Settings` 构造 scheduler / report / notifier 依赖注入后者。

## 影响

- **代码**：新增 `tools/schemas/`（7 个工具的 in/out schema）+ 7 个薄 handler（适配既有 `scheduler/store`、`scheduler/loader`、`scheduler/runner` 的 `SchedulerRunner`、`reporting/store`、`reporting/diff`，**不重写既有业务逻辑**）+ 一个**新增的只读 raw 通道摘要读取**（解析 `notifiers.yaml` 原文取 name/type，因既有 `load_channels` 会展开 secret 不适合 `list_channels`）+ `register_mcp_management_tools` 装配函数；改 `cli/mcp.py serve` 装配段（含 daemon-safe backend 工厂）；改 `scheduler/runner.py` 加 `dispatch_notify` 参数（穿透 `trigger`/`_run_job`/`_finalize_outcome`/`_map_outcome` 4 层，每层默认 `True`）。`list_reports` 的 `target` 必填以严格 1:1 复用 `ReportStore.list_runs(target_id)`，不新增 store 方法。
- **零改动**：`mcp_server/tools_adapter.py`、`mcp_server/server.py`、`agent/tools_adapter.py`、`tools/base.py`（`ToolContext` 不扩字段）、`dispatch` 九步门。
- **依赖**：不新增第三方依赖。
- **MCP 工具数**：`list_tools` 从 3 → 10。

## 非目标 (Non-Goals)

- **不含任何写工具**：`import_targets` / `remove_target` / `test_channel` / `notify_report` 全部属写期提案 `add-mcp-write-approval-flow`（②a/②b）。
- **不建 approval 机制**：不碰两段式 token / pending-action store / `hostlens mcp approve` —— 那是写期②a。
- **不放开 dispatch gate**：rule③（拒 `write`/`destructive`）、rule④（拒 `requires_approval`）原样不动。
- **不扩 `ToolContext` 字段集**：依赖走闭包注入，不动 ADR-008 锁死的 6 字段。
- **不暴露 MCP Resources**（`hostlens://reports/<id>` 等留待有需求另提案）。
- **不动 transport**：stdio-only 不变，不接 HTTP/SSE。
- **不改 `mcp-server` / `mcp-tool-adapter` 契约**。

## 对外契约影响

- **MCP tool schema**：新增 7 个 MCP 工具定义（`mcp-management-tools` 新 spec 固化每个的 input schema / `sensitive_output` / `mcp_description`）。
- **Agent tool schema**：同 7 个工具同时 opt-in `"agent"` surface，本地 Planner/Diagnostician loop 亦可见（`agent_description` 独立撰写）。
- **Schedule manifest schema**：不变。**runner 行为契约**：`trigger` 增 `dispatch_notify` keyword-only 参数（向后兼容，默认 `True`），并逐层穿透 `_run_job`/`_finalize_outcome`/`_map_outcome`（每层默认 `True`，保 timer 零变更）——`scheduler-engine` spec delta 固化。
- **CLI 命令**：`hostlens mcp serve` 装配内容变更（`mcp-cli-command` spec delta 固化）；不新增/不删除任何 CLI 子命令。
- **Inspector schema / Notifier Protocol**：不变。

## Failure Modes

1. **`run_schedule_now` 触发 LLM 失败/超时**：schedule 含 intent，pipeline 会调 Hostlens 自身 backend（非 MCP 远程 LLM）。backend rate-limited / unavailable → Run 落 `failed_api_unavailable`（无 Report）；pipeline 内 token 预算退化 → Report `degraded_*` → runner 映射为 `partial`（**runner 从不构造 `budget_exhausted`，见 `scheduler/runner.py` 文档**）。handler 返回结构化错误信封（经 `scrub_exception_message` 脱敏），不抛裸异常。
2. **schedule/report store 不存在或损坏**：首次运行无 `runs.db` / `reports.db` → list 查询类（`list_schedules` / `list_reports` / `get_schedule_status`）返回**空列表**（`get_schedule_status` 是 list 查询，空 store 返回 `[]` 而非 not-found）；单项查询类（`show_report` / `diff_reports`）对不存在 `report_id` 返回结构化 **not-found**，均不崩。
3. **`show_report` / `diff_reports` 传入不存在的 `report_id`**：返回结构化 `not_found` 错误，不泄露内部路径。`diff_reports` 两份报告跨 target（`compute_diff` 抛 `ValueError`）→ 返结构化错误信封，不让裸 `ValueError` 透传。
4. **`run_schedule_now` 传入未知 schedule name**：handler 在 fresh-load manifests 后**前置检查**命中、返回结构化 not-found，**不**复用 runner `trigger` 抛出的裸 `KeyError`（`trigger` 对未知 name 直接 `raise KeyError`，经 `dispatch` 原样透传——绕过 `is_error` 结构化信封；server 层仍脱敏文本但信封形状非结构化 not-found），**不**触发任何 pipeline。
5. **新工具漏声明 `sensitive_output`**：`build_server` eager 自检（`list_for_mcp`）在服务进入运行态**前** raise `ToolPolicyViolation` fail-closed，serve 退出非 0——防漏配的既有防线天然覆盖新工具。
6. **闭包注入依赖构造失败**（如 `notifiers.yaml` 不可读 → `ConfigError`）：在 `serve` 装配期 fail-loud **退出 2**（与全局退出码约定及 serve 既有 `ConfigError`→exit 2 路径一致），不进入半装配运行态。

## Operational Limits

- **并发**：MCP `dispatch` 沿用 per-call `ToolContext` + 各工具 `timeout`。`run_schedule_now` 最重（跑整包 inspector + LLM pipeline），设较长 `timeout`（建议 ≥120s，复用 schedule pipeline 既有 per-run token budget 与 inspector 并发预算，不另开新并发面）。
- **内存**：`show_report` / `diff_reports` 加载完整 Report JSON 进内存；沿用 `reporting/store` 既有单报告体量（M3 已验证）。
- **超时**：纯查询类（`list_schedules` / `list_channels` / `list_reports` / `get_schedule_status` / `show_report` / `diff_reports`）设短 timeout（5–10s）；`run_schedule_now` 设长 timeout。

## Security & Secrets

- **不引入新密钥**。
- **脱敏**：`list_channels` **绝不返回** token / webhook secret（仅 name/type；通道无 enabled/only_if 字段）；为此 handler **不复用** `load_channels`（它在加载期把 `${ENV_VAR}` 展开成明文 secret 写进 Notifier config），改走只读 raw 通道摘要读取（仅解析原文取 name/type），从源头杜绝 secret 进入输出。`get_schedule_status` 的 notify_results 复用既有 `redact_secret_text`；7 个工具均 `sensitive_output=True`（脱敏后仍暴露环境结构——调度/渠道/报告内容）。`dispatch` 错误信封一律过 `scrub_exception_message`。
- **攻击面 1（远程触发 backend 花费）**：`run_schedule_now` 让远程 LLM 能触发 Hostlens 自身 backend 的 token 消费（见 Cost），属新增的「远程触发本地 LLM 花费」面；由 schedule 必须预先在 `schedules/*.yaml` 配置（远程 LLM 只能触发**已存在**的 schedule，不能构造任意巡检）+ pipeline 既有 token budget 双重约束。只读，无 host/外部状态变更。
- **攻击面 2（订阅 backend 经 MCP 被远程驱动）**：`run_schedule_now` 经 `create_backend(settings)` 构造 backend，而 daemon-safety 门（`ensure_safe_for_daemon`）仅在 `settings.daemon_mode is True` 时触发；`mcp serve` 默认不置 `daemon_mode`，故若不加固，未来订阅 backend 实装后远程 LLM 可经长驻 MCP server 驱动 `ClaudeSubscriptionBackend`（CLAUDE.md §4.11 rule 3 明禁长驻上下文用订阅 backend）。**缓解**：serve 以 `daemon_mode=True` 的 settings 构造管控工具 backend 工厂、并在启动期 eager 构造一次。**当前** `claude_subscription`/`bedrock`/`vertex` 在 `create_backend` 是 placeholder（抛 `NotImplementedError`，先于 daemon 门），订阅 backend 实装后才以 `BackendDaemonUnsafe` 触发 daemon 门——两路径净效果一致（远程不可驱动），但 serve 须**同时 catch `NotImplementedError` 与 `BackendDaemonUnsafe`（及 `ConfigError`）** 映射为脱敏非 0 退出，不漏裸 traceback（`mcp-cli-command` spec delta 固化）。

## Cost / Quota Impact

- 6 个纯查询工具：**零 LLM 调用**（直接读 store/config）。
- `run_schedule_now`：触发一次完整诊断 pipeline = 1 次 Planner + Diagnostician 多轮 tool-use，token 消耗等同一次 `hostlens schedule trigger`，受 schedule 的 `agent.token_budget_*` 既有预算硬上限约束。远程 LLM 频繁调用会放大 Hostlens backend 配额消耗——Demo/文档须提示该工具非免费。
- 本提案自身**不**改变 prompt caching 策略（复用 pipeline 既有两层 cache）。

## Demo Path

无 SSH / 无付费 API 的 cassette replay 优先：

```bash
pip install -e ".[dev,mcp]"
# 1. MCP server 走 stdio：不在 shell 里 `&` 后台跑（stdio 需 client 接管 stdin/stdout），
#    而是由 MCP host / inspector 把 `hostlens mcp serve` 作为子进程拉起（client 持有 stdio 管道）
# 2. 经该 client list_tools → 应见 10 个工具（原 3 + 新 7）
# 3. 调 list_schedules → 返回 schedules/*.yaml 列表 + next_fire_time + notify 绑定（无 enabled 字段）
# 4. 调 list_targets 枚举 target → 调 list_reports(target) → 返回该 target 历史报告（先跑过 demo 持久化的）
# 5. 调 show_report(report_id) / diff_reports(report_id_a, report_id_b) → 返回报告 / diff
# 6. 调 list_channels → 验证仅含 name/type、不含 token / enabled / only_if
```

- 单元/集成测试：每个工具的 handler 直测（薄适配层）+ `dispatch` 投影测试 + fail-closed 自检测试（漏 `sensitive_output` → raise）。
- `run_schedule_now` 走 PlaybackBackend cassette 回放，离线确定性验证「跑 pipeline + 持久化 + 不发 notify」。
