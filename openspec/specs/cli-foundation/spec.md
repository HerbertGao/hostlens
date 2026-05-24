# cli-foundation 规范

## 目的
待定 - 由归档变更 bootstrap-project-skeleton 创建。归档后请更新目的。
## 需求
### 需求:`hostlens` 命令必须作为全局 entrypoint 注册

`pip install -e ".[dev]"` 完成后，`hostlens` 必须出现在 venv 的 `bin/` 路径下；`hostlens --help` 必须列出所有顶级子命令；CLI 框架使用 Typer，所有命令必须用 Typer 装饰器注册（禁止 argparse / click 等其他框架混用）。

#### 场景:CLI entrypoint 注册到 venv

- **当** 执行 `pip install -e ".[dev]"` 后查 `which hostlens`
- **那么** 必须返回 venv 内 `bin/hostlens` 路径

#### 场景:`hostlens --help` 列出子命令

- **当** 执行 `hostlens --help`
- **那么** 必须 exit 0；stdout 必须包含 `doctor` 子命令（M0 唯一已注册的子命令）

#### 场景:未知子命令报错

- **当** 执行 `hostlens nonexistent-subcommand`
- **那么** 必须 exit 非 0，stderr 必须含错误信息指引可用子命令

### 需求:`hostlens doctor` 检查本地环境健康度

`hostlens doctor` 必须依次检查：(a) Python 版本 ≥3.11；(b) `ANTHROPIC_API_KEY` env var 是否存在（M0 不强制，只报告）；(c) `~/.config/hostlens/` 目录是否可读；默认输出人类可读（Rich 渲染），所有检查为 ok 时 exit 0。

#### 场景:全部检查通过

- **当** 在 Python 3.11+ 环境、`ANTHROPIC_API_KEY` 已设置、`~/.config/hostlens/` 可读时执行 `hostlens doctor`
- **那么** 必须 exit 0，输出 3 行检查结果都带 ok 标记

#### 场景:配置目录不可读时退出 1

- **当** `~/.config/hostlens/` 目录存在但当前用户无读权限
- **那么** `hostlens doctor` 必须 exit 1，stderr 必须含修复指引（例如 `chmod 755 ~/.config/hostlens`）

#### 场景:`ANTHROPIC_API_KEY` 缺失不阻塞

- **当** 仅缺失 `ANTHROPIC_API_KEY`（其他检查全 ok）时执行 `hostlens doctor`
- **那么** 必须 exit 0（M0 不强制 LLM 凭据），输出含 "anthropic_key: missing" 警告但不报错

### 需求:`hostlens doctor --json` 输出稳定 schema

`hostlens doctor --json` 必须输出严格符合以下 schema 的 JSON：`{"version": "0.1.0", "timestamp": ISO8601, "checks": {<check_id>: {"status": "ok" | "present" | "missing" | "unreadable" | "error", "detail": str | null, ...}}, "ready": bool}`；约定：「健康度」检查（python_version / config_dir）用 `"ok"`；「存在性」检查（anthropic_key）用 `"present"` / `"missing"`；输出必须可用 `jq` 解析；schema 顶层必须含 `version` 字段以支持未来向后兼容；schema 演进政策——required 字段（version / timestamp / checks / ready / 每个 check 的 status）任何变更都是 breaking，optional 字段（detail / path 等附加 metadata）允许 add-only 新增不 bump version。

#### 场景:JSON 输出合法

- **当** 执行 `hostlens doctor --json | jq -e .`
- **那么** 必须 exit 0（jq 解析成功）

#### 场景:schema 必含字段

- **当** 解析 `hostlens doctor --json` 输出
- **那么** 必须含顶层 `version` / `timestamp` / `checks` / `ready` 四字段；`checks` 必须含 `python_version` / `anthropic_key` / `config_dir` 三个 key

#### 场景:`--json` 模式不输出装饰字符

- **当** 执行 `hostlens doctor --json`
- **那么** stdout 必须是 valid JSON 单文档（无 Rich 颜色码 / 无表格框线 / 无 banner），可直接管道到 `jq` 或 `python -m json.tool`

### 需求:doctor 不泄露密钥原值

`hostlens doctor` 与 `hostlens doctor --json` 输出中，**禁止包含 `ANTHROPIC_API_KEY` 的真实值**；anthropic_key check 仅可输出 `{"status": "present", "detail": null}` 或 `{"status": "missing", "detail": null}`（`detail` 字段必须严格为 `null`，禁止任何前缀 / 后缀 / 掩码值）；即使在 verbose / debug 模式下也禁止打印密钥；structlog 配置必须确保任何环境变量值不进入日志输出。

#### 场景:密钥存在时不打印原值

- **当** `ANTHROPIC_API_KEY=sk-ant-abc123def456...` 已设置，执行 `hostlens doctor --json`
- **那么** 输出 JSON 中 `checks.anthropic_key.status` 必须为 `"present"`，且 `checks.anthropic_key.detail` 必须为 `null`（**禁止任何形式的脱敏前缀 / 后缀 / 掩码值**——存在性检查不需要任何值）；输出整体字符串中**禁止**出现 `sk-ant-abc123` 或类似密钥前缀

#### 场景:logs 中不泄露 env

- **当** 以 `HOSTLENS_LOG_LEVEL=DEBUG hostlens doctor` 运行
- **那么** stderr 日志中**禁止**出现 `ANTHROPIC_API_KEY=` 后跟非空值的字符串

### 需求:非交互环境的 CLI 行为可预测（M0 范围限定为只读命令）

M0 阶段 CLI 仅含 `doctor`（只读，无写操作 / 无交互确认需求）。本需求 M0 仅约束只读命令行为：在非交互环境（无 TTY）下，**禁止**显示 ANSI 颜色 / 进度条 / spinner（由 Rich `Console(force_terminal=False)` 自动检测处理）；**禁止**任何命令在非交互环境 hang 等待 stdin。

写操作的 `--yes` 强制确认 + 非 TTY 退出 1 + 拒绝 root（EUID==0）等 safety helper **不在 M0 范围**，统一推到 M1 第一个写命令（如 `target add`）落地时同步引入（参考 proposal.md 非目标声明）。

#### 场景:非 TTY 下无装饰

- **当** 执行 `hostlens doctor | cat`（管道剥离 TTY）
- **那么** 输出必须不含 ANSI 颜色码

#### 场景:doctor 默认 exit 0 在非交互环境

- **当** 在 CI 环境（无 TTY）执行 `hostlens doctor`
- **那么** 必须正常完成（exit 0 或 1 取决于检查结果），禁止 hang
