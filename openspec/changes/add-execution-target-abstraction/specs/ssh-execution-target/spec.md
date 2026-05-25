## 新增需求

### 需求:`SSHTarget` 必须基于 AsyncSSH 实现且复用 per-target control connection

`hostlens.targets.ssh.SSHTarget` 必须：

- `type == "ssh"`
- 实现 `ExecutionTarget` Protocol（见 `execution-target` spec）
- **持有一个 per-target asyncssh control connection**：首次 `exec` 时按需建立 `asyncssh.connect(...)`；之后每次 `exec` 在该连接上**新建 channel**（通过 `conn.run(cmd, env=env)`，asyncssh 内部为每次 run 开 channel）—— **禁止**每次 exec 都重新 `asyncssh.connect`（对齐 docs/OPERABILITY.md §2.1 / §2.2 硬约束「不允许『每个 Inspector 重新 SSH 一次』—— 这是 M1 实施 SSH target 时必须 enforced 的硬约束」）
- 连接管理用 `asyncio.Lock` 保护"是否已建连 + 是否需重连"状态机；channel 创建本身**无需**加锁（asyncssh 原生支持并行 channel）
- `connect_timeout` 默认 10s，可在 `TargetEntry` 配置中按 target override
- `ssh.idle_timeout_seconds` 默认 300s：control connection 空闲超过此值时自动 close；下次 exec 按需重连
- 断线重连：检测到 `asyncssh.misc.ConnectionLost` / `ChannelOpenError` 等后，自动重连**最多 3 次**，退避 1s / 3s / 9s（指数）；仍失败则 raise `TargetError("ssh_connection_lost", target=name)`
- asyncssh `keepalive_interval` 设为 60s（早发现死连接）；`agent_forwarding=False` + `x11_forwarding=False` 显式禁用（最小权限，对齐 OPERABILITY §2.2）
- `capabilities` 初始值 `{Capability.SSH, Capability.SHELL, Capability.FILE_READ}`；运行时按需探测 `SYSTEMD` / `DOCKER_CLI`（首次 `exec` 后探测一次并缓存到 target 实例）
- 析构（`__del__` / `aclose`）必须 close control connection；测试套不允许 `ResourceWarning: unclosed transport`

#### 场景:SSHTarget 首次 exec 建立连接后复用

- **当** 同一 `SSHTarget` 实例连续调用 `await ssh_target.exec(...)` 3 次，每次间隔 < 5s
- **那么** `asyncssh.connect(...)` 必须**只被调用 1 次**（后续 2 次复用首次建立的 control connection）；3 次调用都成功返回 ExecResult；测试可用 `unittest.mock.patch` wrap `asyncssh.connect` 计数验证

#### 场景:SSHTarget 并行 exec 在同一 connection 上开多 channel

- **当** 用 `asyncio.gather(target.exec(...), target.exec(...), target.exec(...))` 并行触发 3 次 exec
- **那么** `asyncssh.connect` 仍只被调用 1 次（首次建立后并行 exec 共享 connection）；3 个 exec 必须独立完成（一个的 stdout 不会污染另一个）

#### 场景:SSHTarget idle timeout 自动关闭连接

- **当** 配置 `ssh.idle_timeout_seconds=2`；调用 `await target.exec("echo hi", timeout=5)`，然后 `await asyncio.sleep(3)`，再调用 `await target.exec("echo hi", timeout=5)`
- **那么** `asyncssh.connect` 必须被调用**2 次**（首次 + idle 触发 close 后第二次按需建立）

#### 场景:SSHTarget control connection 断开自动重连

- **当** control connection 因服务端 idle disconnect 抛出 `ConnectionLost`；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 实现必须自动重连（最多 3 次，退避 1s/3s/9s）；首次重连成功后 exec 正常返回 ExecResult
- **且** 若 3 次重连全失败，必须 raise `TargetError("ssh_connection_lost", target=name)`（**不**raise asyncssh 原生异常）

#### 场景:SSHTarget 连接超时 raise TargetError

- **当** 配置 `connect_timeout=2`，目标 host 不响应（防火墙 drop）
- **那么** 必须在 ~2s 后 raise `TargetError`，kind 为 `"ssh_connect_timeout"`，含 target name 与 host:port（**不**含凭据 / 不含 stack 中的 key path）

#### 场景:SSHTarget exec 超时返回 timed_out 且 channel close

- **当** 调用 `await ssh_target.exec("sleep 60", timeout=2)` 且 control connection 已建立
- **那么** 必须在 ~2s 后返回 `ExecResult(timed_out=True, exit_code=None)`（与 execution-target spec §ExecResult 不变量一致）；超时仅 close 该 channel，**不**影响 control connection 本身（下次 exec 仍可复用）

### 需求:SSH 凭据**仅**接受环境变量占位 + 明文密码 warn，字段命名严格

`SSHTarget` 凭据字段（与 `TargetEntry` 字段名严格一致）必须遵守以下规则：

- **`password: str | None`** 字段：值是 `${ENV_VAR}` 占位时 → loader 展开（见 `execution-target` spec §`TargetsConfig`）；值是字面明文时 → loader 接受 + doctor 必须 warn；**禁止**写入 SQLite / 日志 / Notifier payload
- **`key_path: str | None`** 字段：必须是文件系统路径字符串（路径本身不是 secret）；文件内容由 asyncssh 加载，**禁止**把 key 文件内容拷贝到任何 Hostlens 日志 / 报告 / payload
- **`passphrase: str | None`** 字段（加密 key 时）：与 password 同规则
- **禁止**支持 `agent_forwarding` 与 SSH agent 集成（M1 范围，避免 agent 安全审计复杂度）

#### 场景:password 走 env 占位

- **当** yaml 含 `password: ${HOSTLENS_SSH_PWD}`，环境 `HOSTLENS_SSH_PWD=secret123`
- **那么** `TargetEntry.password == "secret123"`；`doctor --json` 输出 `credential_source == "env_var"`，无 warning

#### 场景:password 明文 doctor warn 但允许

- **当** yaml 含 `password: literal-pwd`（非 `${...}` 占位）
- **那么** loader 必须**成功**加载；`doctor --json` 输出 `credential_source == "inline_plaintext"` + warning（含 target name 与"建议改用 `${ENV_VAR}` 占位"的修复提示）；doctor 整体**不** exit 1

#### 场景:password 不出现在 SSH 连接失败的错误日志（双层脱敏）

- **当** SSH 认证失败（错误密码 `literal-pwd-do-not-leak-12345`），asyncssh 抛出 `PermissionDenied("auth failed for admin@10.0.0.5 with password literal-pwd-do-not-leak-12345")`
- **那么** Hostlens 必须把异常包装成 `TargetError("ssh_auth_failed", target=name)` 前，先用 `hostlens.agent.tools_adapter.scrub_exception_message`（按 agent-tool-adapter spec §需求:handler 异常必须包装 定义的 5 类正则脱敏函数）清洗原始异常字符串 —— 这套 scrubber 覆盖 path / IPv4 / IPv6 / 凭据特征 / 身份键值对 / email-at-host，能处理"值里有敏感子串、key 名却看似无害"的场景
- **且** 最终 `TargetError.__str__` / `structlog log` 中**禁止**含原始 password `literal-pwd-do-not-leak-12345`、原始 IP `10.0.0.5`、原始 username `admin` 子串；含 target name + kind `"ssh_auth_failed"` + sanitize 后的 asyncssh error 类型名
- **注意**：**禁止**单独依赖 `hostlens.core.logging.redact_sensitive` —— 它只按 key 名脱敏 mapping（如 `{"password": "xxx"}` → `{"password": "***"}`），无法清洗 string 值中的敏感子串

#### 场景:禁止 agent forwarding

- **当** yaml 含 `agent_forwarding: true`
- **那么** 加载必须 raise `pydantic.ValidationError`（M1 不支持的字段，extra=forbid 触发）

### 需求:SSH `env` 注入只走 asyncssh `env=` 参数，**禁止** export 拼接

`SSHTarget.exec(env=...)` 必须：

- 把 `env` dict 通过 asyncssh `conn.run(env=env)` 透传到远端
- **禁止**在客户端把 env 转换为 `export VAR=val; cmd` 拼到 cmd string（secret 会进 process list / shell history / 远端 audit log，与 docs/ARCHITECTURE.md §4 命令渲染安全规则「secrets 必须走 env 路径而不是模板插值」明确冲突）
- **禁止**通过远端临时文件 `source` 的方式传 secret（残留 + 跨平台 cleanup 复杂）
- docstring 与 docs/operations/targets.md 必须**显式**说明：远端 sshd 默认 `AcceptEnv LANG LC_*`，大多数 env var 会被静默丢弃；secret 传递路径限于以下 3 种方式：
  1. 用户在远端 sshd 配 `AcceptEnv HOSTLENS_*`（推荐）+ Inspector 用 `HOSTLENS_` 前缀变量名
  2. Inspector 通过 stdin 传 secret
  3. M5+ 引入 secret transport 通道（未来）
- M1 **不**在 runtime 验证远端 sshd 配置（验证成本高且 false alarm 多）

#### 场景:env 通过 asyncssh.run 透传

- **当** 调用 `await ssh_target.exec("echo $MY_VAR", timeout=5, env={"MY_VAR": "x"})`
- **那么** 实现必须调用 `conn.run(..., env={"MY_VAR": "x"})`，不在 cmd string 中拼 `export MY_VAR=x`

#### 场景:env 未在 cmd string 中泄露

- **当** 调用 `await ssh_target.exec("ps auxw", timeout=5, env={"SECRET_TOKEN": "abc"})`
- **那么** 实现传给 asyncssh 的 cmd 字符串必须严格等于 `"ps auxw"`（**不**含 `"SECRET_TOKEN"` 或 `"abc"` 子串）

### 需求:SSH read_file 必须用 SFTP（**禁止** cat fallback），尊重 10MB 上限

`SSHTarget.read_file(path)` 必须：

- 仅用 asyncssh SFTP（`async with conn.start_sftp_client() as sftp: ...`）
- SFTP 不可用（远端禁用 sftp-server subsystem）时 raise `TargetError("sftp_unavailable", target=name)`；**禁止** fallback 到 `cat <path>` shell 命令（理由：`cat` fallback 含 (a) shell 命令注入风险——`path="x; curl evil"` 直接 RCE；(b) 二进制内容经 shell stdout 可能被截断或编码变换，破坏字节完整性；(c) 大文件无法在读到 10MB 时主动中断）
- 文件 ≥10 MB 时 raise `TargetError("file_too_large", path=path, size=size)`，**不**返回部分内容
- 文件不存在 raise `FileNotFoundError`（标准库异常，不包装）
- path 参数即使是合法 SFTP 路径，也必须经过 `pathlib.PurePosixPath` 校验（拒绝含 NUL 字节 / 换行 / 含 `..` 的相对路径）

#### 场景:read_file 通过 SFTP 读小文件

- **当** 远端 `/tmp/hello.txt` 内容为 `b"hello"`；调用 `await ssh_target.read_file("/tmp/hello.txt")`
- **那么** 必须返回 `b"hello"`；实现内部走 SFTP 协议（**不**走 `conn.run("cat /tmp/hello.txt")`）

#### 场景:read_file SFTP 不可用 raise

- **当** 远端 sshd 禁用 `Subsystem sftp`；调用 `await ssh_target.read_file("/tmp/x")`
- **那么** 必须 raise `TargetError`，kind 为 `"sftp_unavailable"`；**禁止**回退到 cat 等 shell 命令

#### 场景:read_file 超过 10MB raise

- **当** 远端 `/tmp/big.bin` 大小为 11 MB；调用 `await ssh_target.read_file("/tmp/big.bin")`
- **那么** 必须 raise `TargetError`，kind 为 `"file_too_large"`；不返回任何字节（实现应在 SFTP `stat` 阶段判断 size 提前 raise，**不**先 download 11MB 再 raise）

#### 场景:read_file 不存在 raise FileNotFoundError

- **当** 调用 `await ssh_target.read_file("/nonexistent")` 远端无此文件
- **那么** 必须 raise `FileNotFoundError`（不是 `TargetError`）

#### 场景:read_file 路径含 NUL 字节 raise

- **当** 调用 `await ssh_target.read_file("/tmp/x\x00.txt")`
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_path"`；不发起 SFTP 请求

### 需求:SSH 集成测试必须用真实 sshd 容器，配 `AcceptEnv HOSTLENS_TEST_*`

`tests/targets/test_ssh_integration.py` 必须：

- 通过 `pytest-docker` 起 `linuxserver/openssh-server` 容器
- 容器启动时**必须**在 `/etc/ssh/sshd_config` 注入 `AcceptEnv HOSTLENS_TEST_*`（通过 docker mod / `DOCKER_MODS` 环境变量或 fixture 的 post-start command）—— **禁止**测试假装"任意 env var 都能跑"（默认 sshd 拒收，会让 env 测试永远 flaky 或永远 false-pass）
- 测试覆盖：成功 exec / 非零 exit / signal-killed exit (128+signum) / 超时取消 + 进程组回收 / 连接失败 + auto-retry / SFTP read_file / read_file 10MB raise / SFTP 不可用 raise / env 透传仅限 `HOSTLENS_TEST_*` 前缀 / control connection 复用 / idle timeout 关闭 + 重连 / control connection 断线 + 自动重连成功
- **禁止** mock `asyncssh.connect` / `conn.run` —— 必须打真实 SSH 协议（CLAUDE.md §6 测试规则）
- 容器 cold start ~10s，测试用 session-scoped fixture 复用容器；**测试用例之间必须独立**（每个测试用独立 username 或临时目录避免共享状态泄漏）
- **CI retry 策略收紧**：`pytest-rerunfailures` 仅对显式标记 `@pytest.mark.flaky_ssh_integration` 的测试 retry 1 次；**禁止**全局 retry；retry 时必须保留首次失败日志 + container logs（`pytest --capture=no` 或 fixture 在 retry 时 dump container stderr）；若同一测试 1 周内 retry 命中 ≥3 次必须当 race condition bug 处理（不允许长期挂 flaky marker）

#### 场景:集成测试通过真实 sshd 跑 echo

- **当** 跑 `pytest tests/targets/test_ssh_integration.py::test_exec_echo`
- **那么** 必须连到 docker 起的真实 sshd 容器，跑 `echo hostlens-probe`，断言 `ExecResult.stdout` 含 `"hostlens-probe"`

#### 场景:集成测试 env 透传仅限 HOSTLENS_TEST_* 前缀

- **当** 跑 `pytest tests/targets/test_ssh_integration.py::test_env_accepted`，env 含 `HOSTLENS_TEST_VAR=x`
- **那么** 必须断言 `await target.exec("echo $HOSTLENS_TEST_VAR")` stdout 含 `"x"`（容器 sshd 已配 `AcceptEnv HOSTLENS_TEST_*` 允许该前缀）
- **且** 同样 fixture 跑 `await target.exec("echo $SECRET_TOKEN", env={"SECRET_TOKEN": "abc"})` 必须断言 stdout **不**含 `"abc"`（容器 sshd 拒收 `SECRET_TOKEN`，验证了"非 allowlist env 会被远端静默丢弃"这一已记录的限制）

#### 场景:集成测试 control connection 复用

- **当** 在同一 `SSHTarget` 实例上连续跑 3 次 `await target.exec("echo hi", timeout=5)`
- **那么** 检查 hostlens 进程到 sshd 的 TCP 连接数（通过 fixture 在 sshd 容器内跑 `ss -tn '( sport = :22 )'`），整个过程必须**只看到 1 个**新增 ESTABLISHED 连接（验证 control connection 复用）

#### 场景:不允许 mock asyncssh

- **当** 检查 `tests/targets/test_ssh_integration.py` 文件内容
- **那么** 必须**不含** `mock.patch("asyncssh.connect")` / `mock.patch("hostlens.targets.ssh.asyncssh")` 等 mock asyncssh 的代码（M1 SSH 必须走真实协议）
