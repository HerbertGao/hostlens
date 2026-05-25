## 新增需求

### 需求:`ExecutionTarget` Protocol 必须定义完整接口

`hostlens.targets.base.ExecutionTarget` 必须是 `typing.Protocol`，定义以下成员：

- `name: str`：target 实例的唯一标识；必须匹配正则 `^[a-z][a-z0-9_\-]{0,63}$`（用于 yaml key 与 CLI 引用）
- `type: Literal["local", "ssh", "docker", "k8s"]`：与 docs/ARCHITECTURE.md §5 锁定的 4 种 target 类型一致；**禁止**自定义 type 名（如 `kubernetes` 必须用 `k8s`）
- `async def exec(self, cmd: str, *, timeout: int, env: dict[str, str] | None = None) -> ExecResult`：异步执行 shell-evaluated 命令；`timeout` 单位秒，必填；`env` dict 通过实现侧的 subprocess `env=` 参数注入，**禁止**实现侧把 env 拼到 cmd string
- `async def read_file(self, path: str) -> bytes`：异步读远端文件；最大 10 MB（超出 raise `TargetError("file_too_large")`）
- `capabilities: set[Capability]` 属性：返回该 target 当前支持的 Capability 集合（运行时探测结果）

Protocol 必须支持 mypy `--strict` 静态校验。

#### 场景:Protocol 形状完整

- **当** 检查 `ExecutionTarget` 的 `__annotations__` 与方法签名
- **那么** 必须**恰好**含 `name` / `type` / `capabilities` 属性 + `exec` / `read_file` 异步方法（不多不少）；`exec` 必须有 `cmd` 位置参数 + `timeout` 与 `env` keyword-only 参数

#### 场景:exec 是 async 方法

- **当** 检查 `inspect.iscoroutinefunction(SomeTarget.exec)`（任意实现类）
- **那么** 必须返回 `True`

#### 场景:type 字段值域受限

- **当** 实例化 `LocalTarget(name="x", type="local")` 与 `SSHTarget(name="y", type="ssh")`
- **那么** 必须成功；任何试图把 `type` 设为 `"kubernetes"` / `"vm"` / 其他字符串的实现必须在 mypy 阶段报错

#### 场景:read_file 文件超过 10MB raise

- **当** 调用 `await target.read_file("/var/log/huge.log")` 且文件 ≥10 MB
- **那么** 必须 raise `TargetError`，错误 kind 为 `"file_too_large"`，含 target name 与 path（**不含**文件内容）

### 需求:`Capability` Enum 必须含 M1 最小集与扩展守则

`hostlens.targets.base.Capability` 必须是 `enum.Enum`，M1 阶段**至少**含以下成员：

- `SHELL = "shell"`：能跑 shell 命令（所有 target 都有）
- `FILE_READ = "file_read"`：能读文件（所有 M1 target 都有）
- `SSH = "ssh"`：通过 SSH 协议访问（仅 SSHTarget）
- `SYSTEMD = "systemd"`：远端有 systemd（运行时探测）
- `DOCKER_CLI = "docker_cli"`：远端能跑 `docker` CLI（运行时探测）

Enum 成员名必须**全大写**，值必须**全小写**（与 docs/ARCHITECTURE.md §5 一致）。**禁止**在加载 Inspector manifest 时接受 Enum 之外的 capability token —— 未知 capability 必须在 manifest 加载时 raise（防止 silent skip）。

#### 场景:Capability 含 M1 最小集

- **当** 检查 `set(Capability.__members__.keys())`
- **那么** 必须**至少**含 `{"SHELL", "FILE_READ", "SSH", "SYSTEMD", "DOCKER_CLI"}`

#### 场景:Capability 值是小写 string

- **当** 检查每个 `Capability` 成员的 `.value`
- **那么** 必须是该成员名的 lower case（如 `Capability.SSH.value == "ssh"`）

#### 场景:capabilities 集合与 tool-registry TargetSummary allowlist 一致

- **当** 同时检查 `Capability` 所有 `.value` 集合与 `tool-registry-capability-layer` spec 中 `TargetSummary.capabilities` allowlist
- **那么** 两者**必须严格相等**（防止 Tool Registry 投影漂移：本 Enum 是 SOT）

### 需求:`ExecResult` 必须把 `timed_out` 与 `exit_code` 字段分离

`hostlens.targets.base.ExecResult` 必须是 Pydantic v2 模型，含以下字段：

- `exit_code: int`：命令返回码；**超时时必须为 `-1`**（约定值），调用方应**优先**通过 `timed_out` 字段判断超时
- `stdout: str`：UTF-8 解码后的标准输出（非 UTF-8 字节用 `errors="replace"` 容错）
- `stderr: str`：同上
- `duration_seconds: float`：实际执行时长（含连接 + 等待）
- `timed_out: bool`：是否因 `timeout` 参数到期被取消；超时与非零退出**互斥但同字段不能合并**

`model_config = ConfigDict(frozen=True, extra="forbid")` 必须设置。

#### 场景:超时时 timed_out=True 且 exit_code=-1

- **当** 调用 `await target.exec("sleep 100", timeout=1)`
- **那么** 返回 `ExecResult.timed_out is True`、`exit_code == -1`、`duration_seconds >= 1.0`

#### 场景:正常返回非零 exit_code

- **当** 调用 `await target.exec("exit 42", timeout=10)`
- **那么** 返回 `ExecResult.timed_out is False`、`exit_code == 42`

#### 场景:stdout/stderr 非 UTF-8 字节不 raise

- **当** 命令输出含 `\xff\xfe` 等非 UTF-8 字节
- **那么** 必须不 raise；`stdout` 中对应位置必须是 Unicode replacement character `�`

#### 场景:ExecResult 实例不可变

- **当** 已构造的 `result` 试图赋值 `result.exit_code = 0`
- **那么** 必须 raise `pydantic.ValidationError`（frozen=True 生效）

### 需求:`LocalTarget` 必须基于 `asyncio.create_subprocess_shell` 实现

`hostlens.targets.local.LocalTarget` 必须：

- `type == "local"`
- `capabilities` 至少含 `{SHELL, FILE_READ}`；运行时探测：如 `which docker` 成功则加 `DOCKER_CLI`；如 `which systemctl` 成功则加 `SYSTEMD`（探测结果在 target 构造时缓存，**不**每次 exec 都重新探测）
- `exec` 实现走 `asyncio.create_subprocess_shell(cmd, env=...)`，**禁止**走 `create_subprocess_exec`（M1 Inspector 命令含 pipe / redirect 必须 shell 解析）
- 超时实现走 `asyncio.wait_for` 包裹 `proc.communicate()`；超时时必须发 `SIGKILL` 终止 subprocess（不只是 SIGTERM）
- `env` 参数传入时**合并**到 `os.environ.copy()` 之上（不是替换），保留 PATH 等关键 env var

#### 场景:LocalTarget exec 走 shell 解析

- **当** 调用 `await local.exec("echo a | wc -c", timeout=5)`
- **那么** 必须返回 `exit_code=0`、`stdout` 含 `"2\n"`（pipe 被 shell 解析，不是被当作字面字符串）

#### 场景:LocalTarget 超时发 SIGKILL

- **当** 调用 `await local.exec("sleep 60", timeout=1)`
- **那么** 必须在 ~1s 后返回 `ExecResult(timed_out=True)`；subprocess 必须已被回收（无 zombie 进程）；后续 `ps` 看不到该 `sleep` 进程

#### 场景:LocalTarget env 合并而非替换

- **当** 调用 `await local.exec("echo $PATH:$MY_VAR", timeout=5, env={"MY_VAR": "x"})`
- **那么** stdout 必须**同时**含原 `PATH` 内容与 `:x`（合并到 os.environ.copy 之上）

#### 场景:LocalTarget capabilities 运行时探测

- **当** 在装有 docker 的机器上构造 `LocalTarget("my-local")`
- **那么** `local.capabilities ⊇ {Capability.SHELL, Capability.FILE_READ, Capability.DOCKER_CLI}`
- **且** 在无 docker 的机器上构造 → `Capability.DOCKER_CLI ∉ local.capabilities`

### 需求:`TargetRegistry` 必须按 name 索引且禁止重复注册

`hostlens.targets.registry.TargetRegistry` 必须提供：

- `register(target: ExecutionTarget) -> None`：name 冲突 raise `TargetError("duplicate_target", name=name)`
- `get(name: str) -> ExecutionTarget`：未找到 raise `KeyError`
- `names() -> set[str]`：返回所有已注册 target 的 name 集合
- `list() -> list[ExecutionTarget]`：按 name 字典序返回（保证测试 / Tool Registry 投影可复现）

Registry **不**持有连接状态 —— 它只是 name → target 实例的索引；连接生命周期由各 target 实现内部管理。

#### 场景:register 冲突 raise

- **当** registry 已含 `name="prod-web"` target，再次 `registry.register(another_target_named_prod_web)`
- **那么** 必须 raise `TargetError`，错误 kind 为 `"duplicate_target"`，含 name；**不**覆盖原 target

#### 场景:list 按 name 字典序

- **当** 注册顺序为 `["zeta", "alpha", "beta"]`
- **那么** `registry.list()` 必须返回 `[alpha, beta, zeta]`（按 name 排序）

#### 场景:get 未找到 raise KeyError

- **当** `registry.get("not-exist")`
- **那么** 必须 raise `KeyError`（**不是** `TargetError` —— 这是 lookup miss 不是业务错误）

### 需求:`TargetsConfig` 必须从 yaml 加载且环境变量占位展开

`hostlens.targets.config.TargetsConfig` 必须是 Pydantic v2 模型：

- 顶层结构：`version: Literal["1"]` + `targets: list[TargetEntry]`
- `TargetEntry`：`name: str` + `type: Literal["local", "ssh"]` + `enabled: bool = True` + type-specific 字段
- yaml 中 `${VAR_NAME}` 占位必须在加载时展开（从 `os.environ` 读取）；未设置时 raise `ConfigError("missing_env_var", var=VAR_NAME, target=target_name)`
- `${...}` 占位**仅**允许出现在 secret 字段（`password` / `passphrase`）—— 出现在 `host` / `user` / `port` 等字段时 raise `ConfigError("env_placeholder_not_allowed_here")`
- 加载文件不存在时**不**raise —— 返回空 `TargetsConfig(version="1", targets=[])`，让 doctor 引导用户跑 `hostlens target add`

加载入口：`hostlens.targets.config.load_targets_config(path: Path) -> TargetsConfig`

#### 场景:`${ENV}` 占位展开

- **当** yaml 含 `password: ${HOSTLENS_DEMO_PWD}`，环境变量 `HOSTLENS_DEMO_PWD=demo123`
- **那么** 加载后的 `TargetEntry.password == "demo123"`（占位被替换）

#### 场景:env 未设置 raise ConfigError

- **当** yaml 含 `password: ${UNSET_VAR}`，环境无 `UNSET_VAR`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"missing_env_var"`，含 var 名与 target name

#### 场景:占位出现在非 secret 字段 raise

- **当** yaml 含 `host: ${HOST_PLACEHOLDER}` 或 `user: ${USER_PLACEHOLDER}`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"env_placeholder_not_allowed_here"`（防止 host/user 通过 env 注入意外暴露）

#### 场景:配置文件不存在返回空 registry

- **当** `~/.config/hostlens/targets.yaml` 不存在
- **那么** `load_targets_config(path)` 必须返回 `TargetsConfig(version="1", targets=[])`，**不** raise

#### 场景:unknown type raise

- **当** yaml 含 `type: vm`
- **那么** 加载必须 raise `pydantic.ValidationError`（type 字段是 Literal）

### 需求:`hostlens target` CLI 命令集

`hostlens target` Typer 子命令组必须提供：

- `add <name> --type local|ssh [--host ... --user ... --port 22 --key-path ... --password-env VAR]`：写 `targets.yaml` + 校验；name 已存在时 raise + exit 2（参数错误）
- `list [--json]`：表格（默认）或 JSON 输出已配置 target + 每个 target 当前 capabilities + enabled 状态
- `remove <name>`：默认交互确认 y/N；`--yes` 跳过；非交互无 TTY 无 `--yes` 必须 exit 1（按 CLAUDE.md §4.5）
- `test <name>`：跑 `echo hostlens-probe-$$` 验证连通性；输出 ExecResult + 探测到的 capabilities；连通失败 exit 1

所有命令必须使用 M0 已落地的 structlog logger；错误输出走 stderr，数据走 stdout。

#### 场景:target add 名称冲突 exit 2

- **当** `targets.yaml` 已有 `name: prod-web`，跑 `hostlens target add prod-web --type local`
- **那么** 命令必须 exit 2（参数错误），stderr 含 `"target 'prod-web' already exists"`

#### 场景:target remove 无 TTY 无 --yes exit 1

- **当** 在非交互环境跑 `hostlens target remove prod-web`（无 stdin TTY，且未传 `--yes`）
- **那么** 必须 exit 1，stderr 提示 `"--yes required in non-interactive mode"`；**禁止**默默执行删除

#### 场景:target list --json 输出结构化

- **当** 跑 `hostlens target list --json`
- **那么** stdout 必须是合法 JSON，含 `targets: [{name, type, enabled, capabilities: [...]}]`

#### 场景:target test 连通失败 exit 1

- **当** 跑 `hostlens target test ssh-prod` 但远端不可达
- **那么** 必须 exit 1，stderr 含错误 kind（如 `"connection_refused"`）但**不含**凭据；stdout 为空

### 需求:`hostlens doctor` 必须新增 targets 健康检查

`hostlens doctor` 必须扩展输出新增 `targets` section，对每个已配置 target 报告：

- `connectivity`：`ok` / `failed` / `skipped`（disabled 的 target 标 `skipped`）
- `credential_source`：`env_var` / `inline_plaintext`（后者必须 warn）
- `capabilities`：探测到的 capability 集合

`--json` 输出必须含 `targets` key；任一 target `connectivity == "failed"` 必须使 doctor 整体 exit 1（与 M0 doctor 退出码语义一致）。

#### 场景:doctor 检测明文密码 warn

- **当** `targets.yaml` 含 `password: literal-pwd-not-env-placeholder`
- **那么** `hostlens doctor` 必须输出 warning（含 target name 与修复建议）；但 doctor 整体**不** exit 1（仅 warning 不阻塞）

#### 场景:doctor --json 含 targets section

- **当** 跑 `hostlens doctor --json`
- **那么** stdout 是合法 JSON，必须含 `"targets": [{...}]` key

#### 场景:某 target 连通失败 doctor exit 1

- **当** 已配置 SSH target 不可达；跑 `hostlens doctor`
- **那么** 整体 exit 1；输出含失败 target 名与错误 kind
