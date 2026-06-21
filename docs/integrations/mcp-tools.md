# Hostlens MCP 工具清单

Hostlens 以 **stdio MCP Server** 形态把一组工具暴露给远程 LLM（Claude
Code / Cursor 等）。MCP host 把 `hostlens mcp serve` 作为子进程拉起、持有
stdio 管道（不要在 shell 里用 `&` 后台跑——stdio 需 client 接管
stdin/stdout）；client `list_tools` 应见 **11 个工具**。

> 安装：`pip install "hostlens[mcp]"`（`mcp` 为 optional-dep）。MCP 当前
> **stdio-only**，不接 HTTP/SSE。

工具分三组：

- **只读三件套**（M7）——巡检能力：`list_inspectors` / `list_targets` /
  `run_inspector`。
- **读期管控工具集**（M7-ext 读期）——调度 / 报告 / 通道的只读视图：
  `list_schedules` / `get_schedule_status` / `run_schedule_now` /
  `list_channels` / `list_reports` / `show_report` / `diff_reports`。
- **纳管 propose 工具**（`add-mcp-target-import-propose`）——
  `propose_target_import`：产 `ImportPlan` 不落盘的 propose-only 写计划。

全部 11 个工具 `surfaces ∋ "mcp"`、`side_effects ∈ {none, read}`、
`requires_approval=False`，过现有 `dispatch` 门；真正落盘的写操作（落地 target
/ 发通知 / 删 target）属独立写期提案，不在此清单——`propose_target_import` 是
propose-only（`side_effects="read"`），**永不**写 `targets.yaml`。每条工具的
描述与其代码内的 `mcp_description` 语义对齐（权威来源：
`src/hostlens/tools/default_tools.py`、`src/hostlens/tools/management_tools.py`
与 `src/hostlens/tools/import_propose_tool.py`）。

---

## 只读三件套（巡检）

### `list_inspectors`

列出可用 inspector（项目元数据）。每条含 name / version / description /
tags / 兼容的 target kind。不含任何 secret。

- `side_effects`: `none`
- `sensitive_output`: `False`

### `list_targets`

列出已配置 target，输出经脱敏（不含凭据 / 主机名 / 端口）。即便脱敏后的
形态也会暴露环境结构——MCP 暴露需据此把关。

- `side_effects`: `none`
- `sensitive_output`: `True`

### `run_inspector`

对一个 target 跑一个只读 inspector。输出可能含进程 / 端口 / 连接元数据。

- `side_effects`: `read`
- `sensitive_output`: `True`

---

## 读期管控工具集（调度 / 报告 / 通道）

> **跨工具 ID 契约（必读）**：scheduler ledger 的 `run_id` 与 report-store
> 键 `report_id` 是**两个不同的标识符**。`show_report` / `diff_reports` 的
> 查询键是 **`report_id`**，由 `list_reports` / `get_schedule_status` /
> `run_schedule_now` 输出。用 ledger `run_id` 调 `show_report` 会命中
> not-found。

### `list_schedules`

列出从 `schedules/*.yaml` 读取的已配置 schedule。每条含 name / schedule
表达式 / `next_fire_time` / targets / intent / notify 绑定（每条绑定的
channel + `only_if` 路由）。notify 的 `only_if` 是 manifest 文本、非
secret。不返回任何凭据。

> M4 无 schedule 级 enabled 概念——所有加载的 manifest 均为活动态，输出
> **不含** `enabled` 字段。路由可见性由本工具的 `notify[].only_if` 暴露。

- `side_effects`: `none`
- `sensitive_output`: `True`

### `get_schedule_status`

返回最近若干次 scheduler run 留痕（可按 schedule name 过滤；`limit` 默认
10、上限 100）。每条含 ledger `run_id`、status、targets、inspectors，以及
一个可能为 null（无 Report 的 run）的 `report_id`。用 `report_id`（而非
`run_id`）调 `show_report`。notify 结果中的错误已脱敏。

- `side_effects`: `none`
- `sensitive_output`: `True`

### `run_schedule_now`

立即触发一个已配置 schedule 绑定的诊断 pipeline，持久化 Report 但**抑制
所有 notify 派发**（不向任何通道发送）。返回 ledger `run_id`、run status
（`ok` / `partial` / `failed_api_unavailable` / `failed`）、以及 report-store
`report_id`——用 `report_id`（而非 `run_id`）调 `show_report` 读结果；无
Report 产出时 `report_id` 为 null。

> 本工具会跑 **LLM 诊断 pipeline（消耗 token、非免费）**，且只能触发**已配置**
> 的 schedule；未知 name 返回结构化 not-found 错误。建议先用
> `list_schedules` 确认 name。

- `side_effects`: `read`（跑只读 inspector + 本地持久化，无 host / 外部状态变更）
- `sensitive_output`: `True`

### `list_channels`

列出 `notifiers.yaml` 中已配置的通知通道，**仅暴露**每个通道的实例 name 与
type。bot token / webhook URL / 签名 secret / `${ENV_VAR}` 占位符**绝不返回**
——输出是严格的 name/type 白名单。通道无 `enabled` 字段；`only_if` 不属通道
（它是 per-schedule 的 notify 绑定，由 `list_schedules` 暴露）。

- `side_effects`: `none`
- `sensitive_output`: `True`

### `list_reports`

列出**单个 target** 的历史报告索引（`target` 参数**必填**——没有 all-targets
列表；先用 `list_targets` 枚举 target）。每行含 `report_id`（`show_report` /
`diff_reports` 的键）、timestamp、status、finding_count。

- `side_effects`: `none`
- `sensitive_output`: `True`

### `show_report`

按 `report_id` 取回单份已存储 Report（即 `list_reports` /
`get_schedule_status` / `run_schedule_now` 输出的 report-store 键，**不是**
scheduler ledger `run_id`）。返回完整报告，含 findings 与 hypotheses；未知
`report_id` 返回结构化 not-found 错误。

- `side_effects`: `none`
- `sensitive_output`: `True`

### `diff_reports`

按 `report_id` 对两份已存储 Report 跑 regression diff（`report_id_a` = 基线，
`report_id_b` = 当前）。两份必须同 target——跨 target 的组合返回结构化错误，
未知 `report_id` 返回结构化 not-found。输出列出新增 / 已解决 / severity 变化
的 finding，以及 hypothesis 变化。

- `side_effects`: `read`
- `sensitive_output`: `True`

---

## 纳管 propose 工具

### `propose_target_import`

从一个 inventory `ref`（ssh_config / yaml）**提议**一份纳管计划：解析
（promote）→ 探活（probe）→ 分类，返回四桶 `ImportPlan`（`to_add` /
`skipped` / `failed_probe` / `invalid_candidate`）。本工具是 **propose-only**
——**永不**写 `targets.yaml`、永不触发任何落盘（`side_effects="read"`：探活是
对远端的只读 exec，读既有 target 名也是读），故天然不碰 adapter 对
write/destructive 的拒绝门、零 dispatch gate 改动即可暴露（对齐 roadmap §5
「MCP 最多 propose-only」/ M9「给方案不代执行」红线）。

- 可选输入 `source`（限 `ssh_config` / `yaml`，不传则内容嗅探）、`concurrency`
  （`1..100`，对齐 `TargetProbe` 内部并发上界——超限直接 reject 而非静默
  clamp）。
- 输出是完整可 round-trip 的 `ImportPlan`（`model_dump()` dict），用户 / MCP
  client 可把它序列化成文件交给本地 CLI 落地——见下文 **propose→land 闭环**。

- `side_effects`: `read`
- `sensitive_output`: `True`

#### propose→land 闭环（落盘永远在本地完成）

MCP 永不写 `targets.yaml`；落盘半永远是本地的 `hostlens target import
--from-plan <path> --yes`：

```
1. 远程 LLM（经 MCP）调 propose_target_import(ref=...) → 拿到 ImportPlan（dict）
2. client / 用户把该结果序列化成文件（YAML 或 JSON 皆可）：plan.yaml
3. 在本地（落盘机器）跑：
     hostlens target import --from-plan plan.yaml --dry-run   # 先审计（可选）
     hostlens target import --from-plan plan.yaml --yes        # 落盘
```

`--from-plan` 跳过来源解析与探活，**逐字、确定性**落地 plan 快照里的探活结果
（不重跑探测，避免 propose 与 land 之间 probe 漂移）；`ImportPlan.load` 会
重新校验 promotion 不变量（裸 env 名、SSH entry 的 host/user/key_path 无控制
字符、`to_add` enabled、无内联明文凭据），篡改 / 畸形 plan 被拒 exit 2。
CLI 侧用法见 [`docs/operations/targets.md` →「`--from-plan`」](../operations/targets.md#--from-plan--land-a-pre-built-importplan)。

#### `sensitive_output=True` 是披露标签，不是脱敏

输出携带 `to_add` 主机的连接地址（横向移动图），故声明
`sensitive_output=True`。这是**披露标签**（向 MCP client / 审计声明「该工具
输出敏感」），**不是脱敏机制**——adapter 成功路径零 redaction，主机地址逐字
进 `TextContent`。真实信任边界是**用户的 MCP client**：是否把这份含主机地址的
计划交给远程 LLM，由 client / 运营者把关。

#### 探测凭据视角 = MCP 宿主机视角（cred-ful 主机建议走本地 CLI）

探活的凭据从**运行 `hostlens mcp serve` 进程的 `os.environ`** 解析
（`build_import_plan` → `TargetProbe`）。若 serve 宿主机**缺**某候选所需的
`password_env` / `passphrase_env`，该 cred-ful 候选会探活失败、诚实归入
`failed_probe`（非崩溃、非误判 reachable）——远程 propose 出的 plan 会让本可
达的主机看似不可达，落地一个降级 plan。

因此：**对 cred-ful 主机的纳管，优先在能解析凭据的本地直接跑
`hostlens target import <ref>`，而不是远程 MCP propose。** 首批 tizi 节点是
cred-less Tailscale SSH，不触发本场景。

---

## 运行前提

`hostlens mcp serve` 启动时会 **eager 解析 `notifiers.yaml` 中所有已配置通道** 的
`${ENV_VAR}` secret（与 scheduler daemon 一致的 fail-loud 行为）：任一被引用的
环境变量未设，serve 在启动期即以**退出码 2** 退出、**不进入运行态**。

这一行为不因 surface 只读而豁免——即便 `run_schedule_now` 抑制所有通知、
`list_channels` 走的是**不展开 `${ENV_VAR}` 的 raw reader**，serve 仍会在 boot 期
逐一展开每个通道的 secret（`list_channels` 的独立性是 reader 级，不是 boot 级）。

因此运营者在 `serve` 之前必须：

- `export` 每个已配置通道引用的 secret 环境变量，**或**
- 从 `notifiers.yaml` 移除未使用的通道。

---

## Demo Path（无 SSH / 无付费 API 的 cassette replay）

```bash
pip install -e ".[dev,mcp]"
# MCP host 把 `hostlens mcp serve` 作为子进程拉起（client 持 stdio 管道）
# 经该 client list_tools → 应见 11 个工具（原 3 + 7 管控 + 1 propose）
# list_schedules → schedules/*.yaml 列表 + next_fire_time + notify 绑定（无 enabled）
# list_targets 枚举 target → list_reports(target) → 该 target 历史报告
# show_report(report_id) / diff_reports(report_id_a, report_id_b) → 报告 / diff
# list_channels → 仅含 name/type，不含 token / enabled / only_if
# propose_target_import(ref=...) → 产 ImportPlan 不落盘；序列化后本地
#   `hostlens target import --from-plan <file> --yes` 落地（propose→land 闭环）
```
