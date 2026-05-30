# Spec: Replay ExecutionTarget（执行层回放）

## ADDED Requirements

### Requirement: ReplayTarget 实现 ExecutionTarget 协议

The system SHALL 提供 `ReplayTarget`，一个实现完整 `ExecutionTarget` Protocol（`exec` / `read_file` / `capabilities`）的 shippable target（位于 `src/hostlens/targets/`），按预录 fixture 返回确定性结果，使 Inspector 无需真实主机即可走完整 `target → collect → parse → findings` 路径。

#### Scenario: exec 命中返回预录结果

- **WHEN** 对 `ReplayTarget` 调 `exec(cmd, timeout=..., env=...)` 且 `cmd` 命中 fixture
- **THEN** 返回 fixture 中该命令预录的 `ExecResult`（stdout / stderr / exit_code / duration_seconds）

#### Scenario: capabilities 由 fixture 声明

- **WHEN** Inspector preflight 读取 `ReplayTarget.capabilities`
- **THEN** 返回值等于 fixture 顶层 `capabilities` 字段投影出的 `set[Capability]`，使 `requires_capabilities`（如 `systemd`）的场景按声明通过或 skip

#### Scenario: 运行时 type 冒充既有 target 类型

- **WHEN** 读取 `ReplayTarget.type`
- **THEN** 返回 fixture 顶层 `impersonate` 声明的既有类型（`"local"` 或 `"ssh"`，默认 `"local"`），使 runner preflight 的 `target.type in manifest.targets`（`Literal["local","ssh"]`）透明通过，且 `ExecutionTarget.type` 与 `InspectorManifest.targets` 的 Literal 枚举均无需改动

### Requirement: 回放命令匹配与未命中语义

The system SHALL 以「渲染后命令逐行 rstrip 后的 SHA256」作为匹配键精确匹配 fixture；未命中时 MUST 抛 `ReplayMiss`，绝不回落到真实 shell 执行。`ReplayMiss` MUST 继承 `HostlensError` 而**非** `TargetError`，以保证 runner 的 `except TargetError`（将传输失败映射为 `status=target_unreachable`）**不**捕获它 —— 否则命令漂移会被静默吞成「目标不可达」的正常 result，破坏响亮失败语义。

#### Scenario: 未命中命令抛 ReplayMiss

- **WHEN** `exec` 的 `cmd` 在 fixture 中无匹配条目
- **THEN** 抛 `ReplayMiss` 并包含未命中命令信息，不执行任何真实子进程

#### Scenario: ReplayMiss 不被 runner 当作 target_unreachable 吞掉

- **WHEN** 某 Inspector 经 runner 在 `ReplayTarget` 上运行，且其某条命令在 fixture 中未命中
- **THEN** `ReplayMiss`（继承 `HostlensError`）不被 runner 的 `except TargetError` 映射成 `status=target_unreachable`（注：更上层 `ToolsAdapter.dispatch` 的 blanket `except Exception` 仍会把它 catch 成 `is_error` tool_result，故管线级响亮失败靠 strict-consumption，见下）

### Requirement: miss 记录支持 strict-consumption 断言

The system SHALL 让 `ReplayTarget` 记录每一次未命中（`exec` / `read_file`）到可读的 `misses` 集合，即使该次调用同时抛出 `ReplayMiss`。这是漂移响亮失败的主保障：因为 `ToolsAdapter.dispatch` 的 blanket `except Exception` 会把 tool handler 异常吸收成 `is_error` tool_result，管线级漂移检测 MUST 依赖测试对 `target.misses == []` 的断言，而非异常冒泡。

#### Scenario: miss 被记录供测试断言

- **WHEN** 对 `ReplayTarget` 发起一次未命中的 `exec` 后读取 `target.misses`
- **THEN** `target.misses` 含该次未命中的命令记录（即便 `exec` 已抛 `ReplayMiss`）

#### Scenario: 全命中时 misses 为空

- **WHEN** 一次回放管线运行中 `ReplayTarget` 的所有 `exec` / `read_file` 均命中
- **THEN** `target.misses` 为空集合，strict-consumption 断言通过

#### Scenario: preflight 探测命令也需命中

- **WHEN** 某 Inspector 声明 `requires_binaries: [openssl]`，runner preflight 先发 `command -v openssl`
- **THEN** 该探测命令必须在 fixture `commands[]` 中预录，否则 preflight 阶段即抛 `ReplayMiss`

#### Scenario: read_file 未命中抛 ReplayMiss

- **WHEN** `read_file(path)` 的 `path` 不在 fixture `files` 映射中
- **THEN** 抛 `ReplayMiss`，不访问真实文件系统

### Requirement: 配置驱动接线

The system SHALL 允许在 `targets.yaml` 以 `type: replay` + `fixture: <path>` 声明 ReplayTarget，并由 `build_registry_from_config` 识别注册；ReplayTarget 为只读，不受写操作 EUID==0 限制。

#### Scenario: 从配置构建 ReplayTarget

- **WHEN** `targets.yaml` 含 `type: replay` 且 `fixture` 指向有效文件
- **THEN** `build_registry_from_config` 注册一个可用的 `ReplayTarget` 实例，`exec`/`env` 注入路径可用（`env` 被接受但不参与命令匹配）
