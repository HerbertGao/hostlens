## 新增需求

### 需求:`Settings` 从 env 与 .env 文件加载配置并强类型校验

`hostlens.core.config.Settings` 必须继承自 `pydantic_settings.BaseSettings`；**M0 阶段必须支持且仅支持**：(a) 环境变量（前缀 `HOSTLENS_`）(b) 项目根 `.env` 文件；YAML 加载（`~/.config/hostlens/*.yaml`）**推到 M1+**（M0 doctor 仅探测 `config_dir` 目录可读性，**不**读 yaml 文件内容）；任何字段类型校验失败必须 raise `hostlens.core.exceptions.ConfigError`，错误消息必须含字段名 + 期望类型 + 实际值；**实际值若来自标记为 sensitive 的字段（名称匹配 `(?i)(key|token|secret|password|credential)`）必须脱敏为 `"***"`**，不允许在 ConfigError 消息中泄露密钥原值。

**实施约定**：`Settings` 直接构造（`Settings()`）走 Pydantic 原生路径，会 raise `pydantic.ValidationError`（非 ConfigError），仅用于库内部 / 测试 / 高级用户场景；**应用入口必须用 `hostlens.core.config.load_settings()` 工厂函数**，该工厂捕获 `ValidationError` 并按下面要求转换为 `ConfigError` + 脱敏。本规范的下列场景全部基于 `load_settings()`，不基于裸 `Settings()`。

#### 场景:从环境变量加载

- **当** `HOSTLENS_LOG_LEVEL=INFO` 已设置，调用 `load_settings()`
- **那么** 返回的 `settings.log_level == "INFO"`

#### 场景:类型校验失败

- **当** `HOSTLENS_LOG_LEVEL=NotAValidLevel` 已设置，调用 `load_settings()`
- **那么** 必须 raise `ConfigError`，错误消息必须含字段名 `log_level` 与期望的有效值集合 + 实际值 `"NotAValidLevel"`（非 sensitive 字段保留实际值便于调试）

#### 场景:敏感字段值在 ConfigError 中脱敏

- **当** 假设未来某 sensitive 字段（如 `anthropic_api_key`）由 env 传入非法值 `"sk-ant-leakvalue"`，调用 `load_settings()`
- **那么** 必须 raise `ConfigError`，错误消息中**禁止**包含 `"sk-ant-leakvalue"` 子串，必须替换为 `"***"`

#### 场景:默认值生效

- **当** 无任何环境变量与 .env 文件，调用 `load_settings()`
- **那么** 必须 exit 0；`settings.log_level` 必须为默认值（M0 暂定 `"INFO"`）

### 需求:`Logging` 支持 dev 与 prod 两种渲染模式

`hostlens.core.logging.configure_logging(mode: Literal["dev", "prod"])` 必须根据 mode 配置 structlog：dev 模式输出人类可读（含颜色、缩进、时间戳）；prod 模式输出 JSON Lines；模式选择由 `Settings.log_mode` 决定，默认 prod。

#### 场景:dev 模式人类可读

- **当** 调用 `configure_logging("dev")` 后用 `structlog.get_logger().info("hello", k="v")`
- **那么** 输出必须含颜色码（在 TTY 下）+ key-value 对（`k=v` 形式）

#### 场景:prod 模式 JSON Lines

- **当** 调用 `configure_logging("prod")` 后用 `structlog.get_logger().info("hello", k="v")`
- **那么** 输出必须是单行 valid JSON，且 `jq -r .event` 必须返回 `"hello"`，`jq -r .k` 必须返回 `"v"`

### 需求:Logging 必须不打印环境变量值（含嵌套结构兜底）

`configure_logging` 配置的 structlog processor 链中**禁止**包含任何会自动 dump `os.environ` 的 processor；即使开发者错误地用 `logger.info("env", env=os.environ)` 或在嵌套 dict / list 中传入密钥（如 `logger.info("cfg", cfg={"auth": {"api_key": "sk-..."}})`），processor 链必须有**递归遍历**的兜底 redactor：(a) 对任何匹配 `(?i)(key|token|secret|password|credential)` 的**字段名**（包括任意层级嵌套的 mapping key）替换值为 `"***"`；(b) 遍历必须支持 `collections.abc.Mapping`（涵盖 dict 与 `os._Environ` 等非 dict mapping）/ list / tuple / set，最大递归深度 8 层（防恶意递归）；(c) 不修改原始数据，仅修改日志输出。

#### 场景:顶层 env 字段自动脱敏

- **当** `os.environ["ANTHROPIC_API_KEY"] = "sk-ant-abcdef"` 且执行 `logger.info("test", anthropic_api_key="sk-ant-abcdef")`
- **那么** 日志输出中 `anthropic_api_key` 字段值必须为 `"***"`，**禁止**含 `sk-ant-abcdef` 子串

#### 场景:os.environ 整体传入也脱敏（非 dict mapping）

- **当** `os.environ["ANTHROPIC_API_KEY"] = "sk-ant-leakkey"` 且执行 `logger.info("env_dump", env=os.environ)`（注意 `os.environ` 是 `os._Environ` 不是 dict）
- **那么** 日志输出中 `env.ANTHROPIC_API_KEY` 必须为 `"***"`，**禁止**含 `sk-ant-leakkey` 子串；`env.HOME` 等非 sensitive key 必须保留原值

#### 场景:嵌套 dict 中的密钥递归脱敏

- **当** 执行 `logger.info("env", env={"ANTHROPIC_API_KEY": "sk-ant-realkey", "HOME": "/Users/alice"})`
- **那么** 日志输出中 `env.ANTHROPIC_API_KEY` 必须为 `"***"`；`env.HOME` 必须保留原值 `"/Users/alice"`；输出 string 中**禁止**含 `sk-ant-realkey`

#### 场景:嵌套 list 中的 dict 也递归

- **当** 执行 `logger.info("targets", targets=[{"name": "prod-01", "ssh_key": "BEGIN PRIVATE KEY..."}])`
- **那么** 日志输出中 `targets[0].ssh_key` 必须为 `"***"`；`targets[0].name` 必须保留原值

#### 场景:正常字段不被脱敏

- **当** `logger.info("user", username="alice")`
- **那么** 日志输出中 `username` 字段必须保留原值 `"alice"`（不被误脱敏）

#### 场景:不修改原始数据

- **当** 执行 `data = {"api_key": "sk-x"}; logger.info("d", d=data); print(data)`
- **那么** print 输出必须为 `{'api_key': 'sk-x'}`（脱敏只发生在日志渲染阶段，不修改 caller 的 dict）

### 需求:异常基类层次明确

模块 `hostlens.core.exceptions` 必须导出以下类：

- `HostlensError`：所有 Hostlens 自定义异常的基类，继承自 `Exception`
- `ConfigError(HostlensError)`：配置加载 / 校验失败
- `TargetError(HostlensError)`：ExecutionTarget 相关错误（M1+ 真实使用，M0 仅占位）
- `InspectorError(HostlensError)`：Inspector 相关错误（M1+ 真实使用，M0 仅占位）

未来 milestone（M2+）可在此模块继续扩展（如 `BackendError` / `NotifierError`），但 **M0 禁止预先添加未在本 spec 列出的子类**（避免过度设计）。

#### 场景:异常继承链正确

- **当** 执行 `from hostlens.core.exceptions import HostlensError, ConfigError; isinstance(ConfigError("x"), HostlensError)`
- **那么** 必须返回 `True`

#### 场景:异常基类可被通用 catch

- **当** 业务代码 `try: ... except HostlensError as e: log(e)`
- **那么** 必须能捕获 `ConfigError` / `TargetError` / `InspectorError` 任意子类实例

#### 场景:M0 子类列表完整且最小

- **当** 检查 `hostlens.core.exceptions` 模块的导出
- **那么** 必须**恰好**包含 `HostlensError` / `ConfigError` / `TargetError` / `InspectorError` 四个类，**不**含未列出的其他异常类
