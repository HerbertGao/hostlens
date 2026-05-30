# Spec: Inspector 插件体系（sampling_window delta）

## ADDED Requirements

### Requirement: collect.sampling_window 时窗采集

The system SHALL 支持 manifest 可选字段 `collect.sampling_window.duration_seconds`；当声明时，runner MUST 基于可注入时钟计算 `window_end = now`、`window_start = now - duration_seconds`，并把 `window_start` / `window_end`（`YYYY-MM-DD HH:MM:SS` UTC 字符串，journalctl `--since/--until` 友好，非带 `T`/时区偏移的 ISO 形式）与 `window_seconds`（int）注入到 Jinja2 命令渲染上下文与 Finding DSL 求值上下文。省略该字段时行为与既有 Inspector 完全一致（向后兼容）。

#### Scenario: 注入窗口变量到命令渲染

- **WHEN** Inspector 声明 `collect.sampling_window.duration_seconds: 300` 且 `collect.command` 引用 `{{ window_start }}` / `{{ window_end }}`
- **THEN** runner 用 `[now-300s, now]` 的 `YYYY-MM-DD HH:MM:SS` UTC 字符串渲染命令，`window_start` 早于 `window_end` 恰好 300 秒

#### Scenario: 窗口变量可用于 Finding DSL

- **WHEN** 某 finding 的 `when` 表达式引用 `window_seconds`
- **THEN** DSL 求值上下文中 `window_seconds` 等于声明的 `duration_seconds`

#### Scenario: 省略 sampling_window 保持旧行为

- **WHEN** Inspector manifest 未声明 `collect.sampling_window`
- **THEN** 渲染与 DSL 上下文中不出现 `window_start` / `window_end` / `window_seconds`，加载与执行行为与本 delta 之前完全一致

### Requirement: 可注入时钟保证回放确定性

The system SHALL 允许向 runner 注入时钟（默认真实 UTC 时钟）；测试与回放场景 MUST 能注入固定时钟，使含窗口变量的渲染命令在重复运行间逐字节稳定（从而可被 `ReplayTarget` 精确匹配，并使 snapshot 稳定）。

#### Scenario: 冻结时钟产出稳定命令

- **WHEN** 注入固定时钟并对同一 `sampling_window` Inspector 渲染命令两次
- **THEN** 两次渲染出的命令字符串完全相同

### Requirement: 窗口注入变量名为保留名

The system SHALL 把 `window_start` / `window_end` / `window_seconds` 视为运行时注入的保留名；当 manifest `parameters` 声明了与之同名的字段时，loader MUST 拒绝加载并给出字段级错误，避免 parameter 覆盖注入变量造成求值歧义。

#### Scenario: parameter 撞保留名被拒

- **WHEN** 某 manifest 的 `parameters` 声明名为 `window_start` 的字段
- **THEN** loader 拒绝加载该 manifest 并指出该名为保留注入变量名
