# ssh-execution-target 规范

## 目的

定义 Hostlens 基于 AsyncSSH 的 `SSHTarget` 实现（M1 落地范围）：per-target control connection 复用、连接状态机与重连策略、认证方式、命令执行（含 env 注入与 stdin 交互的禁止边界）、文件读取大小上限、连接级 keepalive 与超时、错误分类（认证失败 / DNS / 网络 / timeout / 文件过大）。本规范不含 `ExecutionTarget` Protocol 本体（见 `execution-target` 规范）。
## 需求
### 需求:`SSHTarget` 必须基于 AsyncSSH 实现且复用 per-target control connection

`hostlens.targets.ssh.SSHTarget` 必须：

- `type == "ssh"`
- 实现 `ExecutionTarget` Protocol（见 `execution-target` spec）
- **持有一个 per-target asyncssh control connection**：首次 `exec` 时按需建立 `asyncssh.connect(...)`；之后每次 `exec` 在该连接上**新建 channel**（通过 `conn.run(cmd, env=env)`，asyncssh 内部为每次 run 开 channel）—— **禁止**每次 exec 都重新 `asyncssh.connect`（对齐 docs/OPERABILITY.md §2.1 / §2.2 硬约束「不允许『每个 Inspector 重新 SSH 一次』—— 这是 M1 实施 SSH target 时必须 enforced 的硬约束」）
- 连接管理用 `asyncio.Lock` 保护"是否已建连 + 是否需重连 + 冷连接负缓存"状态机；channel 创建本身**无需**加锁（asyncssh 原生支持并行 channel）
- `connect_timeout`（单次 `asyncssh.connect` 尝试的上限）默认 10s，可在 `TargetEntry` 配置中按 target override
- **`cold_connect_retry_budget_seconds`（首次 connect 的总重试预算）是 `SSHTarget` 的构造参数，默认 `None`**（`None` = 不重试 = 单次尝试，完全保持既有行为）。**它是构造参数、不是 `TargetEntry` 字段**——谁需要重试由**调用路径意图**决定、不是 per-host 配置：`build_registry_from_config(..., cold_connect_retry_budget_seconds=None)` 透传（同文件 `build_one_target` 也加同名参并转发其内部 `build_registry_from_config`），**仅**两条路径传非 None（硬默认 `_DEFAULT_COLD_CONNECT_RETRY_BUDGET_SECONDS = 90.0`）：① 定时 fleet 巡检（`cli/schedule.py` 的调度 registry 构造）；② 纳管探活（`targets/probe.py` 调 `registry.build_one_target` 处，含 `target import` / `propose_target_import`）。**doctor（`cli/doctor.py`）/ `target test`（`cli/target.py`）/ 临时 `inspect`（`cli/inspect.py`）/ `mcp serve`（`cli/mcp.py`）/ `fix`（`cli/fix.py`）** 的 registry 构造一律**不**传（默认 `None` → 保持各自 5–12s 快速失败的响应契约，零行为变更）。**不**在 `Settings.ssh` 加全局字段、**不**在 `SSHEntry` 加 per-target 字段（YAGNI；upgrade path 是真有 per-host 调优需求时再加）。
- `ssh.idle_timeout_seconds` 默认 300s：control connection 空闲超过此值时自动 close；下次 exec 按需重连。**该配置由 M0 `Settings.ssh.idle_timeout_seconds` 提供**（M1 通过 task 4.3a 扩展 Settings 加入 `ssh` 子 namespace；env var `HOSTLENS_SSH__IDLE_TIMEOUT_SECONDS` 可 override）；**不**放进 `TargetEntry` 字段集（M1 范围内 per-process 单值即可，per-target override 推到有用户需求时）
- **首次 connect、冷连接重试、重连三条路径必须严格分开**：
  - **首次 connect**（lazy 建立 `self._conn`）按异常类型分类 raise（**不变**）：
    - `asyncio.TimeoutError` / `OSError` / `socket.gaierror` / `ConnectionRefusedError`（网络层 / DNS / 防火墙） → `TargetError(kind="ssh_connect_timeout", target=self.name)`
    - `asyncssh.PermissionDenied` / `asyncssh.HostKeyNotVerifiable` / `asyncssh.misc.KeyExchangeFailed`（认证 / host key / KEX） → `TargetError(kind="ssh_auth_failed", target=self.name)` + **三层 password scrub**
    - 其它 asyncssh 异常 → `TargetError(kind="ssh_connect_failed", target=self.name, original=exc)`（兜底，**不**走重连循环）
  - **冷连接预算重试**（**本提案新增，仅当 `cold_connect_retry_budget_seconds` 非 None 且 `self._conn is None`**）：在该预算内反复尝试首次 connect，**仅重试 `ssh_connect_timeout`** kind。理由：生产 fleet 走 Tailscale，空闲节点冷路径首次建连是逐步收敛过程，实测可达机器需 >10s（如 ~70s）才建好；单次 `connect_timeout` 无法覆盖。算法：
    - `start = time.monotonic()`；循环：`remaining = budget - (monotonic() - start)`；`remaining <= 0` → 退出去 stamp+raise；
    - 以 **`connect_timeout = min(self._connect_timeout(), remaining)`** 调首次 connect（**每次尝试 cap 到剩余预算**——否则贴着截止线开始的最后一次尝试会超出预算整整一个 `connect_timeout`。`_open_connection` / `_connect_kwargs` 必须支持本次 `connect_timeout` override，否则 cap 静默失效）；
    - 成功 → 见下「锁体顺序」步④；
    - 捕获 `TargetError` 且 **`kind == "ssh_connect_timeout"`** → **重算 `remaining`，`remaining <= 0` 则不再退避、直接去 stamp+raise；否则 `await asyncio.sleep(min(1.0, remaining))`**（1s 小固定退避激进重探推进握手而非忙等，但退避前必须界住截止线——否则耗尽会迟一个退避才判、冲出预算，使总耗时 ≤ budget + 末次尝试而非 budget + 退避）后续循环；
    - **`kind ∈ {ssh_auth_failed, ssh_connect_failed, ssh_no_entry}` 立即 re-raise，不重试**。认证/host-key 是永久错（重试放大延迟 + 触发远端 sshd「too many auth failures」）；`ssh_connect_failed` 是 asyncssh 未分类兜底，**可能含 host-key 不匹配等永久错误**，盲目重试 = 把改过的 host key 当瞬时态反复探测（安全隐患）。故重试集**严格只含 `ssh_connect_timeout`**（明确网络瞬时态）。
    - 预算耗尽（`remaining <= 0`）→ stamp 负缓存后 raise `TargetError(kind="ssh_connect_timeout", target=self.name)`（**kind 不变**，对调用方契约稳定，只是来得更晚）
  - **冷连接失败的批内负缓存短路**：定时巡检里一台机的 N 个 inspector 共享同一个跨 run 长生命周期 `SSHTarget`（`context_factory` 闭包共享 `TargetRegistry`）、并发争连接锁。若不短路，首个 inspector 耗尽预算失败后每个兄弟 inspector 会各起一轮预算（N×预算）。故耗尽预算时 stamp `self._cold_connect_failed_at = time.monotonic()`，锁体内在 `self._conn is None` 分支检查该 stamp 是否在 TTL（`_COLD_CONNECT_NEG_TTL = 120.0`）内 → 命中则立即 raise `ssh_connect_timeout`（fast-fail）。stamp **只在预算耗尽时写一次**（非每次尝试失败）、**只在锁入口读一次**（非循环中途）。**清除点唯一**：首次 connect 成功（步④）清 `self._cold_connect_failed_at = None`。**`_reconnect` 不清 stamp**——常态下 stamp 仅在耗尽时写（此时 `self._conn is None`），而 `_reconnect` 仅在 `self._conn` 曾成功建立（必经步④已清 stamp）后断开才触发，二者实际不可达地共存；且负缓存读受 `self._conn is None` 门控，`_conn` 非 None 时任何残留 stamp 被屏蔽，即便构造出极罕见交错（活跃连接被兄弟误判 idle——inspector 超时 ≪ idle_timeout 300s 已阻止），最坏后果也仅是 TTL（≤120s）内一次假 fast-fail，非正确性破坏。故**不**为此路径加防御性清除（对齐「不写不可能分支兜底」）。该负缓存覆盖**所有获取连接的入口**（`exec` / `read_file` / preflight `command -v`·`[ -r ]` 探测都经 `_ensure_connection`），不止 inspector exec——同一冷 host 一次 stamp 后其余入口一并 fast-fail，正确且符合预期。TTL 远短于日巡间隔故不污染下一次 run；连续 trigger（间隔 < TTL）间真机恢复会假阴性一次（有意权衡）。纳管探活每台**新建** `SSHTarget` 后 `aclose`，stamp 不跨 host，故 probe 只吃预算、不享负缓存（每台一次 exec，无 N× 问题）。
  - **重连（仅当 `self._conn is not None` 且已经成功建立过连接，随后检测到 `asyncssh.ConnectionLost` / `asyncssh.ChannelOpenError`）**：按下方精确算法（**不变**）
- **`_ensure_connection` 锁体精确顺序**（pin 死，防 stale-stamp 误判）：① idle sweep（`_conn` 非 None 且超 idle → close 置 None，不变）→ ② **若 `_conn is None`**：负缓存 fast-fail 检查（命中 raise）→ ③ 进首次 connect（budget None：单次 `_open_connection`；budget 非 None：预算重试循环）→ ④ 成功：赋 `self._conn` + `self._last_used_at` + 清 stamp → ⑤ 耗尽：stamp + raise。
- **重连精确算法**（不变）：
  ```python
  # Pre-condition: self._conn 之前已成功建立，现在 exec(cmd) 时检测到 ConnectionLost
  conn_timeout = entry.connect_timeout or 10
  for delay in [1.0, 4.0, 16.0]:        # 严格按 OPERABILITY §2.2 的退避序列
      await asyncio.sleep(delay)
      try:
          self._conn = await asyncssh.connect(..., connect_timeout=conn_timeout)
          return await self._run_on_channel(cmd, timeout=timeout, env=env)
      except (asyncssh.ConnectionLost, asyncssh.ChannelOpenError):
          continue                       # 已建过连接 + 这一类错误才重试
  raise TargetError(kind="ssh_connection_lost", target=self.name)
  ```
  其中 `self._run_on_channel(cmd, timeout, env)` 是 exec 的实际 channel 调用，与首次正常路径同一 helper。这定义为 **"1 次自动重连尝试块（一组 3 段退避 + 3 次 connect 尝试）"**——总共最多 3 次 `asyncssh.connect` + 3 次 sleep（共 21s 上限）。**重连循环的 catch 范围仍严格限 `ConnectionLost` / `ChannelOpenError`，禁止扩到 `OSError`**。**冷连接预算重试是与重连循环完全独立的路径**——前者仅用于 `self._conn is None` 的首连、仅退 1s、仅重试 `ssh_connect_timeout`、报 `ssh_connect_timeout`；后者仅用于已建连断开、退 `[1,4,16]s`、报 `ssh_connection_lost`。两者互不混入。
- asyncssh `keepalive_interval` 设为 60s；`agent_forwarding=False` + `x11_forwarding=False` 显式禁用
- `capabilities` 初始值 `{Capability.SSH, Capability.SHELL, Capability.FILE_READ}`；运行时按需探测 `SYSTEMD` / `DOCKER_CLI`（首次 `exec` 后探测一次并缓存）
- 析构（`__del__` / `aclose`）必须 close control connection；测试套不允许 `ResourceWarning: unclosed transport`

#### 场景:SSHTarget 首次 exec 建立连接后复用

- **当** 同一 `SSHTarget` 实例连续调用 `await ssh_target.exec(...)` 3 次，每次间隔 < 5s
- **那么** `asyncssh.connect(...)` 必须**只被调用 1 次**（后续 2 次复用）；3 次都成功返回 ExecResult

#### 场景:默认无预算时首次 connect 单次尝试（回归锚，本提案新增）

- **当** `SSHTarget` 以 `cold_connect_retry_budget_seconds=None`（默认，即 doctor / `target test` / `inspect` 路径）构造，目标 host 不响应
- **那么** 首次 connect **只尝试 1 次**即 raise `ssh_connect_timeout`（**不**进重试循环、**不** stamp 负缓存）——既有快速失败行为零变更

#### 场景:SSHTarget 并行 exec 在同一 connection 上开多 channel

- **当** 用 `asyncio.gather(target.exec(...), target.exec(...), target.exec(...))` 并行触发 3 次 exec
- **那么** `asyncssh.connect` 仍只被调用 1 次；3 个 exec 必须独立完成

#### 场景:SSHTarget idle timeout 自动关闭连接

- **当** 配置 `ssh.idle_timeout_seconds=2`；exec、sleep(3)、再 exec
- **那么** `asyncssh.connect` 必须被调用**2 次**（首次 + idle close 后第二次）

#### 场景:SSHTarget control connection 断开自动重连

- **当** control connection 因服务端 idle disconnect 抛出 `ConnectionLost`；调用 exec
- **那么** 自动重连**1 次**（退避 1s → 4s → 16s）；成功后 exec 正常返回
- **且** 该重连块穷尽仍失败 → raise `ssh_connection_lost`（**不**raise asyncssh 原生异常）
- **且** 重连路径**不**触碰 `self._cold_connect_failed_at`（stamp-set 态与重连前置态互斥，无需在此清除——见上「清除点唯一」）

#### 场景:SSHTarget 冷连接在预算内重试后成功（本提案新增）

- **当** 以 `cold_connect_retry_budget_seconds=90`（巡检 / 纳管路径）构造，**可达但冷路径慢**的 host：`asyncssh.connect` 前 K 次抛 `asyncio.TimeoutError`、第 K+1 次成功；预算足以容纳 K+1 次尝试 + K 次 1s 退避
- **那么** `exec` 必须**最终成功**返回 ExecResult；`asyncssh.connect` 被调用 K+1 次；建连后 `self._conn` 缓存，后续 exec 复用（不再触发预算重试）

#### 场景:SSHTarget 冷连接耗尽预算后 raise ssh_connect_timeout 且每次尝试 cap 到剩余预算（本提案新增）

- **当** 以小预算构造（测试），目标 host 持续不响应 —— `asyncssh.connect` 恒抛 `TimeoutError`
- **那么** 必须在预算耗尽后 raise `ssh_connect_timeout`（含 target name 与 host:port、不含凭据 / key path），且 stamp 负缓存；**且** 每次尝试的 `connect_timeout` 取 `min(connect_timeout, 剩余预算)`、退避取 `min(1.0, 剩余预算)` 且耗尽不再退避——总耗时 ≤ budget + 末次连接尝试（**不**额外加一整个退避），最后一次尝试不冲出截止线

#### 场景:SSHTarget 冷连接失败后同批兄弟 exec 立即 fast-fail（本提案新增）

- **当** 同一 `SSHTarget` 实例：首个 exec 耗尽预算失败（stamp 负缓存），随后 TTL 内对同实例发起第二个 exec
- **那么** 第二个 exec 必须**立即** raise `ssh_connect_timeout`，**不再**进预算重试（`asyncssh.connect` 调用计数相对第一个 exec 之后**不增加**）

#### 场景:SSHTarget 冷连接负缓存 TTL 过期后重新重试（本提案新增）

- **当** 首个 exec 耗尽预算失败 stamp 后，等待超过负缓存 TTL（测试 monkeypatch 为小值），再对同实例 exec
- **那么** 该 exec 必须**重新进入**预算重试（`asyncssh.connect` 再次被调用）——负缓存是批级短路、非持久健康判定

#### 场景:SSHTarget 首次 connect 认证失败立即 raise 不重试（本提案新增）

- **当** 以非 None 预算构造，首次 connect 时 `asyncssh.connect` 抛 `asyncssh.PermissionDenied`（或 host-key / KEX 失败，归 `ssh_auth_failed`）
- **那么** 必须**立即** raise `ssh_auth_failed`（含三层 scrub），`asyncssh.connect` **只被调用 1 次**（**不**进预算重试、**不** stamp 负缓存）。同理 `ssh_connect_failed`（asyncssh 兜底，可能含 host-key 漂移）**不重试**——重试集严格只含 `ssh_connect_timeout`

#### 场景:SSHTarget exec 超时返回 timed_out 且 channel close

- **当** 调用 `await ssh_target.exec("sleep 60", timeout=2)` 且 control connection 已建立
- **那么** 必须在 ~2s 后返回 `ExecResult(timed_out=True, exit_code=None)`；超时仅 close 该 channel，**不**影响 control connection（下次 exec 仍可复用）

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

#### 场景:password 不出现在 SSH 连接失败的错误日志（三层脱敏）

- **当** SSH 认证失败（错误密码 `literal-pwd-do-not-leak-12345`），asyncssh 抛出 `PermissionDenied("auth failed for admin@10.0.0.5 with password literal-pwd-do-not-leak-12345")`
- **那么** Hostlens 必须把异常包装成 `TargetError(kind="ssh_auth_failed", target=name)` 前按以下**三层顺序**清洗原始异常字符串：
  1. **已知 secret 精确替换**（SSHTarget 本地实现，必须）：用 `self._entry.password` / `self._entry.passphrase` 等**已知**的 secret 值在异常字符串上做 `str.replace(secret, "***")`；这层能保证 caller 配置过的 secret 一定被脱敏，**与正则规则覆盖范围无关**
  2. **正则脱敏 `scrub_exception_message`**（来自 agent-tool-adapter spec §需求:handler 异常必须包装 定义）：覆盖 path / IPv4 / IPv6 / 凭据特征（`*_KEY=` / `Bearer` / `sk-...`）/ 身份键值对（`user=x` / `login=y` 等带 `=` 形式）/ email-at-host —— 处理 caller 不知道的"未知敏感子串"
  3. **bare credential keyword scrub**（必须，覆盖 layer 2 漏的"key value"格式）：在 scrub_exception_message 之后再跑一次正则 `(?i)(password|passwd|pwd|passphrase|secret|token|api[_-]?key|auth)\s+\S+` → `"\1 ***"`，覆盖 `with password X` / `auth token Y` / `passphrase Z` 这种 caller-unknown 但形态明显的裸 secret
- **且** 最终 `TargetError.__str__` / `structlog log` 中**禁止**含原始 password `literal-pwd-do-not-leak-12345`（被 layer 1 精确替换）、原始 IP `10.0.0.5`（被 layer 2 IPv4 规则脱敏）、原始 username `admin`（被 layer 2 email-at-host `admin@10.0.0.5` 整段脱敏）任意子串；保留 target name + kind `"ssh_auth_failed"` + sanitize 后的 asyncssh error 类型名
- **注意**：**禁止**单独依赖 `hostlens.core.logging.redact_sensitive`（它只按 key 名脱敏 mapping）；**禁止**省略 layer 1（"已知 secret 精确替换"）—— 否则若用户的 password 字符不触发任何正则模式（如纯字母 + 数字 + 连字符），会泄露

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
- SFTP 不可用（远端禁用 sftp-server subsystem）时 raise `TargetError(kind="sftp_unavailable", target=name)`；**禁止** fallback 到 `cat <path>` shell 命令（理由：`cat` fallback 含 (a) shell 命令注入风险——`path="x; curl evil"` 直接 RCE；(b) 二进制内容经 shell stdout 可能被截断或编码变换，破坏字节完整性；(c) 大文件无法在读到 10MB 时主动中断）
- 文件 ≥10 MB 时 raise `TargetError(kind="file_too_large", target=self.name, path=path, size=size)`，**不**返回部分内容
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
- 测试覆盖：成功 exec / 非零 exit / signal-killed exit (128+signum) / **超时取消 + channel close**（**SSH 远端进程不在 hostlens 进程下，hostlens 只能 close channel；远端进程清理由 sshd 负责，hostlens 不做进程组回收 —— 那是 LocalTarget 的事**）/ 连接失败 + 1 次自动重连（对齐 OPERABILITY §2.2）/ SFTP read_file / read_file 10MB raise / SFTP 不可用 raise / env 透传仅限 `HOSTLENS_TEST_*` 前缀 / control connection 复用 / idle timeout 关闭 + 重连 / control connection 断线 + 自动重连成功 / **三层 password scrub**（已知 secret 精确替换 + scrub_exception_message + bare credential keyword scrub）
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
- **那么** 检查 hostlens 进程到 sshd 的 TCP 连接数（通过 fixture 在 sshd 容器内跑 `ss -tn 'sport = :2222'`），整个过程必须**只看到 1 个**新增 ESTABLISHED 连接（验证 control connection 复用）

#### 场景:不允许 mock asyncssh

- **当** 检查 `tests/targets/test_ssh_integration.py` 文件内容
- **那么** 必须**不含** `mock.patch("asyncssh.connect")` / `mock.patch("hostlens.targets.ssh.asyncssh")` 等 mock asyncssh 的代码（M1 SSH 必须走真实协议）
