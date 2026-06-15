## 为什么

deterministic 调度（`add-deterministic-inspection-mode`，已上线 ts.mac-mini 每日全队巡检）目前**无法给固定 inspector 集传参**：`run_deterministic_inspection` 对每个 inspector 写死 `runner.run(..., parameters=None)`（`src/hostlens/orchestration/deterministic.py:214`），整队只能跑各 inspector 的默认参数。

这在真机暴露为**误报噪声**：`net.listening_ports`（`src/hostlens/inspectors/builtin/net/listening_ports.yaml`）把任何绑定到公网通配地址、且端口不在 `allowed_ports` 白名单里的监听口标 `warning`。但 fleet 里大量服务器**正常运行**代理 / 组网 / 中继业务——`easytier-core`、`derper`、`hbbr`/`hbbs`、`snell-server`、`sing-box`、`hysteria-server`、`bark-server`、`rustdesk`——它们**多用动态 / 随机端口**，端口白名单根本兜不住，于是这些「业务本来就该公网监听」的端口被逐个标成 warning（2026-06-15 ts.mac-mini 实测一轮巡检产生 89 条此类 warning），把真正值得看的异常淹没。

端口白名单是错的抓手——这些服务的稳定身份是**进程名**不是端口。需要一个「按进程名豁免」的能力，且要能在**调度层**按队声明（不同 fleet 跑不同业务，豁免名单是部署相关的，不该硬编码进 inspector 默认值）。这需要补上 deterministic 调度「给 inspector 传参」这条缺失的通路。

## 变更内容

三处协同（一条参数从 manifest 流到 inspector finding 逻辑）：

1. **schedule-manifest**：`ScheduleManifest` 新增 `inspector_parameters: dict[str, dict[str, Any]]`（inspector canonical name → 该 inspector 的参数对象），默认 `{}`。可与内置默认健康集或显式 `inspectors:` 并用。`load_schedules` 新增注入 `InspectorRegistry`（与既有 `TargetRegistry` 对称），在**加载期** fail-loud 五道校验（全部翻译成 `ConfigError`）：agent 模式非空即拒；deterministic 下 key 须 ∈ 解析出的 inspector 集；key 须是已注册 inspector（`registry.get` 的 `InspectorError` 翻译成 `ConfigError`，不裸抛）；key 的 inspector 须声明 `parameters`（给无参 inspector 传参拦掉——8 个默认健康 inspector 里 7 个无参）；参数值须过该 inspector 的 `parameters` schema（经与 runner 复用的同一 helper，typo / 非法值 / 畸形 schema 在加载期暴露，不拖到次日触发）。

2. **deterministic-inspection-mode**：`run_deterministic_inspection` / `run_deterministic_pipeline` 接受 `inspector_parameters`，逐 inspector 查 `(inspector_parameters or {}).get(<inspector name>)` 透传给 `InspectorRunner.run(parameters=...)`（替换写死的 `None`）。加载期校验是主门；`InspectorRunner.run` 的 jsonschema 校验是**运行期 defense-in-depth 二道门**，兜住绕过 loader 直接构造 manifest 的路径（非法参数 → `status="exception"`，在报告里可见、不静默）。

3. **net.listening_ports inspector**：新增 `allowed_processes: array<string>`（`items` 带 `pattern: ^[A-Za-z0-9._@-]+$`，默认 `[]`）参数；finding 的 `when` 由 `p.wildcard == True and p.port not in allowed_ports` 改为 `p.wildcard == True and p.port not in allowed_ports and p.process not in allowed_processes`——通配监听口**且**端口未豁免**且**进程未豁免才报 warning。已知业务进程放行、陌生进程仍报（不是把整条检查关掉）。`pattern` 既是 authoring-contract 对 string-array 参数的硬约束（缺则 registry build 崩），又顺带拒掉 `allowed_processes:[""]` 会静默放过所有未归因监听口的洞。version `1.0.0 → 1.1.0`。

**Demo Path**（一个 deterministic fleet manifest——其 `targets` 须先 `target add` 注册，故作为 docs 示例 / `*.yaml.example` 交付，不在仓内 `schedules/` 提交 live manifest，避免 fresh checkout 上 `schedule list`/`doctor` 因 target 未注册而失败）：

```yaml
mode: deterministic
inspector_parameters:
  net.listening_ports:
    allowed_processes: [easytier-core, derper, hbbr, hbbs, snell-server, sing-box, hysteria-server, bark-server, rustdesk]
```

→ `hostlens schedule trigger <name>` → 报告不再为这些进程的监听口报 warning，陌生进程的通配监听口**仍**报 warning。

## 非目标（Non-Goals）

- **不改 agent 模式行为**。`inspector_parameters` **仅** deterministic 生效；agent 模式下 Planner 按 Agent-loop 设计自主选 inspector 并决定参数，不读 schedule 静态参数。agent 模式 manifest 写了非空 `inspector_parameters` → loader fail-loud 拒绝（不静默忽略）。
- **不引入全局 inspector 参数默认配置文件**。参数只在 schedule manifest 内声明；不做 `~/.config/hostlens/inspector-defaults.yaml` 这类跨 schedule 的全局默认。
- **不做端口范围 / 正则 / glob 匹配**。`allowed_processes` 与 `allowed_ports` 同为**精确成员**（membership）比较；进程名要逐字匹配，端口是整数集合。模糊匹配是后续提案。
- **不改 `net.listening_ports` 的采集 shell**。`allowed_processes` 只进 Finding DSL 的 `not in` membership 比较，不插值进 `ss`/awk 命令（无需 `| sh`），与 `allowed_ports` 同处理路径——不扩大 shell 注入面。
- **不新增 `InspectorStatus` / 报告 severity 语义**。透传参数只改变某 inspector「报不报某条 finding」，不改它的 status 派生、不改 fleet 聚合规则。
- **不补 inspector-authoring-contract**。`allowed_processes` 完全沿用 `allowed_ports` 已有的「array 参数 + Finding DSL membership 比较」约定（该约定已是 contract 的现有形态），无新作者约定要立。

## 影响

- **契约**：`schedule-manifest`（新增 `inspector_parameters` 字段 + loader 注入 `InspectorRegistry` 的五道校验需求）、`deterministic-inspection-mode`（新增参数透传 + 运行期兜底需求）。net.listening_ports 的 `allowed_processes` 行为以 manifest + 测试为契约，不建 per-inspector spec（与所有既有 builtin inspector 一致），见 design 决策 5。
- **代码**：`scheduler/schema.py`、`scheduler/loader.py`（新增 `inspector_registry` 形参，3 处调用点补传：`cli/schedule.py` / `cli/doctor.py` / `cli/mcp.py`）、`orchestration/deterministic.py`、`scheduler/runner.py`、`inspectors/builtin/net/listening_ports.yaml`；`resolve_inspector_set` 从 `orchestration/deterministic.py` 迁到轻量的 `inspectors/health.py`（避免 loader 经它拖入 agent/backend 重依赖；`deterministic.py` 改从新址 import、保 SOT 单点）。
- **向后兼容**：`inspector_parameters` 默认 `{}` → 既有 deterministic manifest 行为不变；`net.listening_ports` `allowed_processes` 默认 `[]` → 既有调用方（含 `hostlens inspect`）行为不变（空豁免集 = 原逻辑）。无迁移。
