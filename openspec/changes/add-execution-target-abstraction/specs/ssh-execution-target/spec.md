## 新增需求

### 需求:`SSHTarget` 必须基于 AsyncSSH 实现且每次 exec 新建连接

`hostlens.targets.ssh.SSHTarget` 必须：

- `type == "ssh"`
- 实现 `ExecutionTarget` Protocol（见 `execution-target` spec）
- 内部使用 `asyncssh.connect` + `conn.run(cmd, env=env)`
- **每次 `exec` 调用必须新建 SSH connection**（M1 不实现连接复用 / ControlMaster；性能优化推到 M6+ 基于 benchmark）
- `connect_timeout` 默认 10s，可在 `TargetEntry` 配置中按 target override
- `capabilities` 初始值 `{Capability.SSH, Capability.SHELL, Capability.FILE_READ}`；运行时按需探测 `SYSTEMD` / `DOCKER_CLI`（首次 `exec` 后探测一次并缓存到 target 实例）

#### 场景:SSHTarget exec 通过 asyncssh.connect 建立连接

- **当** 调用 `await ssh_target.exec("uptime", timeout=10)` 且目标可达
- **那么** 实现内部必须**每次**调用 `asyncssh.connect(...)`（不复用前次连接）；exec 完成后必须 close connection（asyncio 完成时无 ResourceWarning）

#### 场景:SSHTarget 连接超时 raise TargetError

- **当** 配置 `connect_timeout=2`，目标 host 不响应（防火墙 drop）
- **那么** 必须在 ~2s 后 raise `TargetError`，kind 为 `"ssh_connect_timeout"`，含 target name 与 host:port（**不**含凭据 / 不含 stack 中的 key path）

#### 场景:SSHTarget exec 超时返回 timed_out

- **当** 调用 `await ssh_target.exec("sleep 60", timeout=2)` 且连接已建立
- **那么** 必须在 ~2s 后返回 `ExecResult(timed_out=True, exit_code=-1)`；asyncssh channel 必须被 close

### 需求:SSH 凭据**仅**接受环境变量占位 + 明文密码 warn

`SSHTarget` 凭据配置必须遵守：

- **password** 字段：值是 `${ENV_VAR}` 占位时 → loader 展开（见 `execution-target` spec §`TargetsConfig`）；值是字面明文时 → loader 接受 + doctor 必须 warn；**禁止**写入 SQLite / 日志 / Notifier payload（脱敏走 M0 已落地的 `core/logging.redact_sensitive`）
- **private_key path** 字段：必须是文件系统路径字符串（路径本身不是 secret）；文件内容由 asyncssh 加载，**禁止**把 key 文件内容拷贝到任何 Hostlens 日志 / 报告 / payload
- **passphrase** 字段（key 加密时）：与 password 同规则
- **禁止**支持 `agent_forwarding` 与 SSH agent 集成（M1 范围，避免 agent 安全审计复杂度）

#### 场景:password 走 env 占位

- **当** yaml 含 `password: ${HOSTLENS_SSH_PWD}`，环境 `HOSTLENS_SSH_PWD=secret123`
- **那么** `TargetEntry.password == "secret123"`；`doctor --json` 输出 `credential_source == "env_var"`，无 warning

#### 场景:password 明文 doctor warn 但允许

- **当** yaml 含 `password: literal-pwd`（非 `${...}` 占位）
- **那么** loader 必须**成功**加载；`doctor --json` 输出 `credential_source == "inline_plaintext"` + warning（含 target name 与"建议改用 `${ENV_VAR}` 占位"的修复提示）；doctor 整体**不** exit 1

#### 场景:password 不出现在 SSH 连接失败的错误日志

- **当** SSH 认证失败（错误密码）
- **那么** 抛出的 `TargetError` `__str__` / structlog log 必须**不含**原始 password 子串；含 target name + kind `"ssh_auth_failed"` + sanitize 后的 asyncssh error 类型名

#### 场景:禁止 agent forwarding

- **当** yaml 含 `agent_forwarding: true`
- **那么** 加载必须 raise `pydantic.ValidationError`（M1 不支持的字段）

### 需求:SSH `env` 注入受远端 sshd `AcceptEnv` 限制

`SSHTarget.exec(env=...)` 必须：

- 把 `env` dict 通过 asyncssh `conn.run(env=env)` 透传到远端
- **不**在客户端把 env 转换为 `export VAR=val; cmd` 拼到 cmd string（secret 不能进 shell history / process list）
- docstring 与 docs/operations/targets.md 必须**显式**说明：远端 sshd 默认 `AcceptEnv LANG LC_*`，大多数 env var 会被静默丢弃；推荐 Inspector 通过 stdin 传 secret 或要求用户在远端 sshd 配 `AcceptEnv HOSTLENS_*`
- M1 **不**在 runtime 验证远端 sshd 配置（验证成本高且 false alarm 多）

#### 场景:env 通过 asyncssh.run 透传

- **当** 调用 `await ssh_target.exec("echo $MY_VAR", timeout=5, env={"MY_VAR": "x"})`
- **那么** 实现必须调用 `conn.run(..., env={"MY_VAR": "x"})`，不在 cmd string 中拼 `export MY_VAR=x`

#### 场景:env 未在 cmd string 中泄露

- **当** 调用 `await ssh_target.exec("ps auxw", timeout=5, env={"SECRET_TOKEN": "abc"})`
- **那么** 实现传给 asyncssh 的 cmd 字符串必须严格等于 `"ps auxw"`（**不**含 `"SECRET_TOKEN"` 或 `"abc"` 子串）

### 需求:SSH read_file 必须用 SFTP 或 cat 兜底且尊重 10MB 上限

`SSHTarget.read_file(path)` 必须：

- 优先用 asyncssh SFTP（`async with conn.start_sftp_client() as sftp: ...`）
- SFTP 不可用（如远端无 sftp-server）时 fallback 到 `cat <path>` 通过 stdout 读取
- 文件 ≥10 MB 时 raise `TargetError("file_too_large", path=path, size=size)`，**不**返回部分内容
- 文件不存在 raise `FileNotFoundError`（标准库异常，不包装）

#### 场景:read_file 超过 10MB raise

- **当** 远端 `/tmp/big.bin` 大小为 11 MB；调用 `await ssh_target.read_file("/tmp/big.bin")`
- **那么** 必须 raise `TargetError`，kind 为 `"file_too_large"`；不返回任何字节

#### 场景:read_file 不存在 raise FileNotFoundError

- **当** 调用 `await ssh_target.read_file("/nonexistent")` 远端无此文件
- **那么** 必须 raise `FileNotFoundError`（不是 `TargetError`）

### 需求:SSH 集成测试必须用真实 sshd 容器（不 mock asyncssh）

`tests/targets/test_ssh_integration.py` 必须：

- 通过 `pytest-docker` 起 `linuxserver/openssh-server` 容器
- 测试覆盖：成功 exec / 非零 exit / 超时取消 / 连接失败 / SFTP read_file / read_file 10MB 上限 / env 透传
- **禁止** mock `asyncssh.connect` / `conn.run` —— 必须打真实 SSH 协议（CLAUDE.md §6 测试规则）
- 容器 cold start ~10s，测试用 session-scoped fixture 复用容器
- CI 上偶发失败允许 retry 1 次（pytest-rerunfailures），多于 1 次必须当 bug 处理

#### 场景:集成测试通过真实 sshd 跑 echo

- **当** 跑 `pytest tests/targets/test_ssh_integration.py::test_exec_echo`
- **那么** 必须连到 docker 起的真实 sshd 容器，跑 `echo hostlens-probe`，断言 `ExecResult.stdout` 含 `"hostlens-probe"`

#### 场景:不允许 mock asyncssh

- **当** 检查 `tests/targets/test_ssh_integration.py` 文件内容
- **那么** 必须**不含** `mock.patch("asyncssh.connect")` / `mock.patch("hostlens.targets.ssh.asyncssh")` 等 mock asyncssh 的代码（M1 SSH 必须走真实协议）
