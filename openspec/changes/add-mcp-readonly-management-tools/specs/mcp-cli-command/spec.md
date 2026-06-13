## 修改需求

### 需求:`hostlens mcp serve` 必须以 stdio 启动 MCP Server 且对缺失依赖优雅退出

CLI 必须新增 `mcp` 子命令组，含 `hostlens mcp serve`，以 stdio transport 前台启动 MCP Server（装配真实 `ToolRegistry` + `ToolContext` 工厂）。registry 装配必须先调 `register_default_tools`（只读三件套），再调 `register_mcp_management_tools`（7 个只读管控工具，见 `mcp-management-tools`）；后者所需的 scheduler / report / notifier 依赖必须由 serve 从 `Settings` 构造后经闭包注入（`ManagementToolDeps`），**禁止**为此向 `ToolContext` 增加字段。

`run_schedule_now` 的 runner 工厂构造的 `backend_factory` 必须强制 daemon-safety：MCP server 是长驻、接受远程 LLM 指令的进程，等价于 CLAUDE.md §4.11 rule 3 所禁的「长驻 daemon 上下文」。但 `create_backend(settings)` 仅在 `settings.daemon_mode is True` 时才调 `ensure_safe_for_daemon`，而 serve **默认不置** `daemon_mode`。故 serve 必须使管控工具的 `backend_factory` 走 daemon-safe 设置（以 `model_copy(daemon_mode=True)` 后的 settings 构造 backend，复用 `cli/schedule.py` 的 daemon 翻转模式）。

**eager 探针语义**：serve 在启动期 eager 构造**一个**管控工具 backend（复用 `cli/schedule.py` `_serve` 的「boot 时 eager `create_backend` 触发 daemon 门」模式），该实例**仅为校验、随即丢弃**；后续每次 `run_schedule_now` dispatch 时 `build_runner` 经**同一 daemon-safe `backend_factory`** 按需重建 backend。不变量：eager 探针与 `backend_factory` 闭包**必须绑定同一份 `daemon_mode=True` 的 settings**——否则会出现「探针用 daemon settings 校验通过、而 factory 闭包误绑原始 settings 致 dispatch 期绕过 daemon 门」的洞（当前 placeholder backend 阶段无害，但订阅 backend 实装后会成真洞）。

**实现状态边界（诚实声明，避免场景前提与代码错配）**：daemon-safe 门 `ensure_safe_for_daemon` → `BackendDaemonUnsafe` 是**前瞻保护**——它在未来 daemon-unsafe backend（如 `ClaudeSubscriptionBackend`）实装后才会以该具体异常触发；**当前** `bedrock` / `vertex` / `claude_subscription` 在 `create_backend` 均为 placeholder、直接抛 `NotImplementedError`（早于 daemon 门）。两条路径都达成「远程 LLM 不能驱动这些 backend」的净效果，但异常类型不同。因此 serve 的 eager 探针构造**必须同时捕获 `BackendDaemonUnsafe` / `NotImplementedError` / `ConfigError`**，**禁止**漏掉 `NotImplementedError` 导致裸 traceback（注意：`cli/schedule.py` 的既有 `_serve` 只 catch `BackendDaemonUnsafe`+`ConfigError`，MCP serve 不可原样照搬、须补 `NotImplementedError`）。**退出码映射**与既有 daemon `_serve` 一致：`BackendDaemonUnsafe` / `NotImplementedError`（backend 不可用的启动期拒绝）→ **退出码 1**（`_serve` 的 `BackendDaemonUnsafe` 经 `_fail` 即 exit 1）；`ConfigError`（配置/参数错）→ **退出码 2**。

`mcp` 为 optional-dependency：当官方 `mcp` SDK 未安装时，`hostlens mcp serve` 必须捕获 `ImportError`，向 stderr 打印清晰提示（含 `pip install "hostlens[mcp]"`）并以**退出码 1** 退出，**禁止**抛裸 traceback、**禁止**以退出码 0 静默成功。

管控工具的依赖构造若失败（如 `notifiers.yaml` 不可读 → `ConfigError`），serve 必须在进入运行态**前** fail-loud 以**退出码 2** 退出（经脱敏的清晰错误），**禁止**进入半装配运行态、**禁止**以退出码 0 静默成功；**禁止**用退出码 1 表示配置文件不可读（退出码 1 专用于 SDK 缺失 / policy / backend 不可用类启动拒绝，见下）。

退出码语义与全局约定及 serve 既有路径一致——退出码 1 = 业务失败 / optional-dep 缺失 / 启动期拒绝（backend 不可用），退出码 2 = 参数·配置错。具体生产者集：
- **退出码 1（共 4 个生产者）**：① mcp SDK 未安装（`ImportError`，既有）；② `build_server` eager fail-closed 自检抛 `ToolPolicyViolation`（既有）；③④ 本提案在 daemon-safe 探针**新增**的 `BackendDaemonUnsafe` 与 `NotImplementedError`（backend 不可用的启动期拒绝，与 ② 同语义类）。①② 既有保留；③④ 是本提案新增的 exit-1 生产者（serve 当前根本未捕获这两个异常，须新增 catch）。
- **退出码 2（生产者：`ConfigError`）**：本提案新增「管控依赖构造失败（`ConfigError`，如 `notifiers.yaml` 不可读）→ 退出码 2」一类，对齐 serve 现有 `_load_settings_or_exit` 对 `ConfigError` 即用退出码 2、`cli/schedule.py` 的 `_build_channels` 亦退出码 2。

**禁止**用退出码 1 表示配置文件不可读（配置错恒走退出码 2，与上述 4 个 exit-1 生产者区分）。

#### 场景:mcp SDK 已安装时 serve 启动 stdio server 并暴露全部工具

- **当** 官方 mcp SDK 已安装，运行 `hostlens mcp serve`
- **那么** 进程以 stdio transport 启动 MCP Server（前台），可被 MCP host 拉起并响应 list_tools / call_tool
- **且** list_tools 必须同时含只读三件套（`list_inspectors` / `list_targets` / `run_inspector`）与 7 个管控工具（`list_schedules` / `get_schedule_status` / `run_schedule_now` / `list_channels` / `list_reports` / `show_report` / `diff_reports`）

#### 场景:mcp SDK 未安装时 serve 退出码 1 且提示安装

- **当** 官方 mcp SDK 未安装，运行 `hostlens mcp serve`
- **那么** 进程以退出码 1 退出，stderr 含安装提示 `pip install "hostlens[mcp]"`
- **且** **不**打印裸 Python traceback、**不**以退出码 0 退出

#### 场景:管控依赖构造失败时 serve fail-loud 退出 2

- **当** 官方 mcp SDK 已安装但 `notifiers.yaml` 不可读导致 `ManagementToolDeps` 构造失败（`ConfigError`），运行 `hostlens mcp serve`
- **那么** 进程必须以**退出码 2** 退出、stderr 含经脱敏的清晰错误，**不**进入运行态、**不**打印裸 traceback、**不**以退出码 0 退出、**不**以退出码 1 退出（退出码 1 已被既有用途占用：mcp SDK 未装 / `ToolPolicyViolation` fail-closed；配置错须用退出码 2 与之区分）

#### 场景:run_schedule_now backend 工厂强制 daemon-safe

- **当** `settings` 选定一个 daemon-unsafe 或未实装的 backend（当前 `claude_subscription` / `bedrock` / `vertex` → `NotImplementedError`；未来订阅 backend 实装后 → `BackendDaemonUnsafe`），运行 `hostlens mcp serve`
- **那么** serve 必须在启动期 eager 构造一个探针 backend（在 `daemon_mode=True` 设置下、与 `backend_factory` 同源 settings）、捕获 `BackendDaemonUnsafe` / `NotImplementedError`（→ **退出码 1**）与 `ConfigError`（→ **退出码 2**），以**经脱敏的非 0 退出** fail-loud，**禁止**漏掉 `NotImplementedError` 而打印裸 traceback、**禁止**进入运行态后让远程 LLM 经 `run_schedule_now` 驱动该 backend 消费
