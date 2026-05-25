## 上下文

M0 已交付项目骨架，M2 前置 `tool-registry-capability-layer` 已落地 + 归档。Tool Registry 把 `ToolContext.target_registry` 暂时声明为 stub Protocol，标注"M1 落地前可用 stub"。本提案是 M1 的第一块拼图：把 `ExecutionTarget` 抽象、`Capability` 系统、`LocalTarget` / `SSHTarget` 两个实现、`TargetRegistry`、`hostlens target` CLI 一次性落地，让"Inspector dispatch → 拿 ExecutionTarget → exec 命令"端到端可跑。

技术约束已由 CLAUDE.md §4.3 + docs/ARCHITECTURE.md §5 钉死：
- Protocol 形状（`name` / `type` / `exec(cmd, *, timeout, env)` / `read_file(path)` / `capabilities`）不可议
- `exec` 接受 shell-evaluated string（不是 argv list）—— Inspector manifest 的 `collect.command` 含 pipe / redirect / 变量引用，必须 shell 解析；安全边界由后续 Inspector 提案的 manifest 渲染层负责
- `env` 通过 subprocess `env=` 参数注入（不在 cmd string 里拼），secrets 走这条路径
- AsyncSSH / asyncio 异步链路，不允许同步 IO

利益相关者：
- M2 Agent loop（已用 stub target_registry）—— 本提案落地后切真实类型，PR 同周期
- M1 下一个提案 `add-inspector-plugin-system` —— 消费 ExecutionTarget Protocol
- M8 Docker / K8s Target —— 复用 Capability / Registry / CLI 框架，本提案不超前实现
- M9 Remediation —— 未来扩展 `exec` 写语义边界（本提案禁止）

## 目标 / 非目标

**目标：**

1. 把 `ExecutionTarget` Protocol 与 `Capability` 枚举从 ARCHITECTURE 文档搬进 `src/hostlens/targets/base.py`，作为后续所有 Target 实现的 SOT
2. 交付 `LocalTarget`（M1 演示路径必需 + 单测基础设施）与 `SSHTarget`（M1 第一个跨主机 Target，验证抽象边界）
3. 提供 `TargetRegistry` + `targets.yaml` loader + `hostlens target {add,list,remove,test}` CLI，让用户能从零开始 5 分钟内把"配 SSH 主机 → 跑命令拿结果"端到端走通
4. 把 Tool Registry 落地时的 `target_registry` stub 替换成真实类型；M2 `list_targets` ToolSpec 拿真数据
5. 给 doctor 子命令加 `targets` 健康检查 section（连通性 / 凭据来源 / 明文密码警告）

**非目标：**

1. DockerTarget / KubernetesTarget —— M8 单独提案
2. **跨进程 SSH 连接共享**（OpenSSH `ControlMaster` 用 unix socket 共享给其它进程）—— 进程内 per-target connection pool 是 M1 必做（见决策 6 + OPERABILITY §2），但跨进程共享留到 M6+ 基于真实部署形态再决定
3. macOS Keychain / Linux Secret Service / SOPS 加密密钥来源 —— M5+ 路线
4. SSH bastion / jump host / agent forwarding —— 用户需求出现后再做
5. `exec` 的写操作语义（"返回的 exit_code 之外允许修改远端状态"）—— M9 Remediation 才扩展；M1 `exec` 用于读类命令，与 Tool Registry `side_effects ∈ {none, read}` 一致
6. Target 自动 health 监控 / 自动 disable 失败 target —— 失败由调用方处理，不做后台守护
7. Capability 自动发现的完备性 —— M1 只做基础探测（SSH 默认 + 运行时 `which systemctl` / `which docker` probe；POSIX 标准且轻量，与 execution-target spec 一致），false negative 可接受
8. **Windows 宿主支持** —— M1 LocalTarget 用 `os.killpg` + `start_new_session`（POSIX 专有 API），明确**只支持 POSIX 宿主（Linux / macOS）**；Windows 落地推到有用户需求时（届时实现路径走 `CREATE_NEW_PROCESS_GROUP` + `TerminateProcess`，是独立工作量）；M1 在 import 时跑 `if sys.platform == "win32": raise ImportError("LocalTarget requires POSIX host")` 给清晰错误

## 决策

### 决策 1：`ExecutionTarget` 是 Protocol 而不是 ABC

**选择**：`typing.Protocol`（structural typing）

**理由**：

- M8 Docker / K8s Target 的实现可能来自第三方库的包装（不方便强制继承自 Hostlens 基类）
- Protocol 配合 mypy `--strict` 已经能在 import 时静态检查 Target 是否符合接口
- 与 Tool Registry 的 `ToolContext` 字段类型注解风格一致（也是 Protocol）

**替代**：

- ❌ `abc.ABC` + `@abstractmethod`：强制继承，对第三方 wrapper 不友好；运行时 instantiation 才报错，反馈晚
- ❌ duck typing 不加 Protocol：丢失 mypy 检查

### 决策 2：`exec(cmd: str, *, timeout, env)` 收 string 而不是 argv list

**选择**：`cmd: str`，shell-evaluated

**理由**：

- Inspector manifest `collect.command` 是 YAML 字符串，自然含 pipe / redirect / 变量引用：`ps -eo ... | head -20`、`echo ${PGPASSWORD} | mysql -p`
- 强制 argv list 等于要求 Inspector manifest 自己拆 token + 自己处理 pipe，反而把 shell 注入风险从一处集中防御（manifest 渲染层）变成 N 处分散防御
- 安全边界归属：**manifest 渲染层**（下一个提案 `add-inspector-plugin-system`）必须强制 string parameter 走 `| sh` Jinja filter；secrets 必须走 `env=` 路径而不是模板插值；本提案的 `exec` 假定调用方已经做完渲染层校验

**替代**：

- ❌ `cmd: list[str]`（argv）：见上
- ❌ 两个 API（`exec_shell(str)` + `exec_argv(list)`）：增加复杂度无收益；M1 Inspector 全部走 shell 模式
- ❌ 自己实现 mini shell parser：重复造轮子，且容易和真实 shell 行为分歧

### 决策 3：`ExecResult.timed_out` 与 `exit_code: int | None` 字段分离

**选择**：`timed_out: bool` 与 `exit_code: int | None` 是独立字段；超时时 `timed_out=True, exit_code=None`；signal-killed 时 `exit_code=128+signum`（POSIX 约定）；模型层 `model_validator` 强制不变式 `timed_out is True → exit_code is None`。

**理由**：

- 调用方区分"命令跑完但返回非零"与"命令被 hostlens 主动取消"的语义需求强（前者 = 业务诊断信号，后者 = hostlens 自身配额信号）
- **`exit_code: int | None`**（不是 `int`+魔数 `-1`）：Linux subprocess 在 signal-killed 时返回 `128+signum`（SIGSEGV → 139、SIGKILL → 137）—— 用 `-1` 表达 "hostlens 主动超时" 会与某些信号场景的真实负值或 256-补码 wrap 冲突，语义不清；`None` 表达 "无 OS-level exit code"（hostlens 主动取消 / 远端断开未拿到 status）则语义干净
- 显式 `timed_out` 配合 M4 Scheduler 的 RunStatus 与 M3 Diagnostician 的 finding `inspector_status: timeout` 自然对齐
- 调用方判断超时**必须**用 `timed_out` 字段，**不**用 `exit_code` 值

**替代**：

- ❌ `exit_code: int` 用 `-1` 表达超时：与 Python subprocess 在某些平台/信号场景的真实负值或 wrap 后的 unsigned 255 冲突；调用方写 `if exit_code != 0` 也会把超时混进业务失败
- ❌ 单字段 `exit_code: int | None` 不要 `timed_out`：调用方无法区分"超时取消"与"远端断开未拿到状态"
- ❌ raise 异常表达超时：和 `exit_code != 0` 一致性差；调用方必须 try/except；并行调用时统计聚合变难

### 决策 4：`Capability` 是 Enum 不是 set[str] 自由字面量

**选择**：`enum.Enum`，初始集 `SSH / SYSTEMD / DOCKER_CLI / FILE_READ / SHELL`

**理由**：

- mypy / IDE 自动补全；防 typo（`FILE_READ` vs `FILE_RAED` 调用方写错 string 不报错）
- Inspector manifest 的 `requires_capabilities` 字段在加载时按 Enum 校验，未知 capability 立刻报错（避免 silent skip）
- Tool Registry `TargetSummary.capabilities` 投影时已声明 allowlist（spec 已锁）—— Enum 与该 allowlist 必须严格一致，避免运行时漂移

**替代**：

- ❌ `set[str]`：失去类型安全
- ❌ `Literal["ssh", ...]`：扩展时要改所有引用处；Enum 更自然

### 决策 5：SSH 凭据只接受 `${ENV_VAR}` 占位，明文 password 触发 doctor warn 但不 raise

**选择**：

- yaml 中允许 `password: ${HOSTLENS_DEMO_SSH_PASSWORD}` 占位（loader 展开）
- 也允许 `password: literal-text`（loader 接受 + doctor warn）
- private_key path 永远是字符串（路径本身不是 secret，文件内容才是）

**理由**：

- M1 不需要把"明文 password 直接报错"做成红线 —— 早期 demo / 本地测试用户会因此寸步难行
- 但 doctor 必须 warn，让用户感知；M2+ 可逐步升级为加载时 error
- `${...}` 占位是 Hostlens 全栈约定（M0 Settings 也用），不引入新机制
- macOS Keychain / SOPS 是更彻底的方案，留到 M5+

**替代**：

- ❌ 明文密码加载时 raise：阻塞早期 demo
- ❌ 强制 keychain：M1 范围爆炸
- ❌ 仅支持 key 认证：用 docker `linuxserver/openssh-server` 起测试容器配 key 比配 password 复杂得多，CI 集成测试体验差

### 决策 6：SSHTarget 维护 per-target control connection pool（M1 必须复用，1 次重连）

**选择**：每个 `SSHTarget` 实例持有**一个** asyncssh control connection（首次 `exec` 时建立）；每次 `exec` 在该连接上**新建 channel**（`conn.run`）；空闲超过 `ssh.idle_timeout_seconds`（默认 300s）才 close；连接断开 / EOF / 心跳失败时按指数退避 **1s → 4s → 16s 自动重连 1 次**（严格对齐 docs/OPERABILITY.md §2.2 措辞「1 次自动重连（指数退避 1s→4s→16s）」），仍失败 raise `TargetError(kind="ssh_connection_lost", target=self.name)`。

**理由**：

- **OPERABILITY 已硬性规定**：§2.1 明确"每个 target 维护一个 per-process SSH connection pool（类似 OpenSSH ControlMaster auto）"+ §2.2 "连接中断 → 1 次自动重连（指数退避 1s→4s→16s），再失败 → 该 target 本次巡检全部 Inspector 标记 `target_unreachable`" + "不允许『每个 Inspector 重新 SSH 一次』—— 这是 M1 实施 SSH target 时必须 enforced 的硬约束"。本提案严格复制 OPERABILITY 措辞与参数，不引入新解读
- SSH connection 建立成本（key exchange + 认证）100-500ms；M6+ 一次巡检会跑 10+ Inspector，per-exec 建连会把单次巡检拖长几秒
- asyncssh `connect()` 返回的 `SSHClientConnection` 原生支持并行多 channel；并行 4 个 Inspector 在同一连接上互不阻塞
- 实现上其实并不复杂：一个 `asyncio.Lock` 保护"是否已建连 + 是否需重连"状态机；channel 创建本身无需加锁
- **退避总时长 21s**（1+4+16）；超出后转为业务级 `target_unreachable`，由 Inspector Runner / Scheduler 决定是否在下次巡检重试

**替代**：

- ❌ 每次 exec 新建连接：违反 OPERABILITY §2 硬约束
- ❌ 重连 3 次 / 退避 1s/3s/9s（前一轮 spec 误写）：与 OPERABILITY §2.2 参数不符；spec 之间不一致会让实现者无从依据
- ❌ 长连接 + 无 idle timeout：长跑 daemon 下连接数随 target 数线性增长；300s idle 是合理上限

### 决策 7：SSH `env` 注入只走 asyncssh `env=` 参数（**禁止** export 拼接）

**选择**：

- `SSHTarget.exec(env=...)` 内部把 dict 传给 asyncssh control connection 上的 `conn.run(env=env)`（**不**是 module-level `asyncssh.run(...)`，asyncssh 没有这个 API）
- 远端 sshd 默认只 `AcceptEnv LANG LC_*` —— 大多数 env var 会被静默丢弃
- docs 与 doctor 必须明确说明这一限制；M1 不在 runtime 验证（验证成本太高 + false alarm 率高）
- 真正传 secret 的 Inspector 必须走**以下三种方式之一**，**不允许**其它：
  1. 用户在远端 sshd 配 `AcceptEnv HOSTLENS_*`（推荐） + Inspector 用 `HOSTLENS_` 前缀变量名
  2. Inspector 通过 stdin 传 secret（asyncssh `stdin=...`）
  3. M5+ 引入 secret transport 通道（未来）

**理由**：

- `export VAR=val; cmd` 拼接到 shell 命令字符串会让 secret 进入 `ps auxw` / shell history / 远端 audit log，与 docs/ARCHITECTURE.md §4 命令渲染安全规则的"secrets 必须走 env 路径而不是模板插值"明确冲突
- 假装"env 注入永远生效"会让用户配了 `PGPASSWORD` 跑 `mysql -p` 后失败但不知道为什么 —— 明文限制比沉默失败好
- 验证 sshd `AcceptEnv` 需要远端配置读取或试错探测，成本与价值不匹配

**替代**：

- ❌ 把 env 转换为 `export VAR=val; cmd` 拼到 cmd string：违反 ARCHITECTURE §4 secret 边界（安全倒退）
- ❌ 强制远端 sshd 加 `AcceptEnv *`：通配符等于没限制，运维抗拒
- ❌ 把 secret 写到远端临时文件再 `source`：临时文件残留 + 跨平台 cleanup 复杂

### 决策 8：`TargetsConfig` 是 Pydantic 模型，targets.yaml 由 loader 显式校验

**选择**：

- `hostlens.targets.config.TargetsConfig` Pydantic v2 模型，含 `version: Literal["1"]` + `targets: list[TargetEntry]`
- 加载错误（schema 错误 / env_var 未设置 / `${...}` 占位出现在非 secret 字段 / unknown type / name 不匹配正则）有清晰文件路径 + 字段级 error
- **明文 password 字段不在加载错误清单内**：与决策 5 一致——明文 password 加载成功，仅 doctor warn（M2+ 才升级为加载时 error）
- 与 M0 `Settings` 风格一致（pydantic-settings）

**理由**：

- Pydantic 的 ValidationError 自动给出字段路径（`targets[1].password`），用户友好
- 与 Inspector manifest（下一提案）的加载方式对齐，降低维护成本

**替代**：

- ❌ 直接 `yaml.safe_load()` 后手工 dict 访问：报错不友好
- ❌ JSON Schema 校验：与项目其他模块不一致

### 决策 9：写操作 CLI（`add` / `remove`）EUID==0 拒绝

**选择**：

- `hostlens target add` / `hostlens target remove` 入口检查 `os.geteuid() == 0`，是则立即 exit 1，stderr 输出修复建议
- `hostlens target list` / `test` / `hostlens doctor` 是只读，**不**拒绝 root
- M1 落地时**实现一次** inline `os.geteuid()` 检查到 `hostlens.cli.target` 的 `add` / `remove` 入口；**不**新增 `hostlens.core.privilege.require_unprivileged()` helper —— M1 范围内仅 2 个写命令，inline 比抽 helper 更直接；helper 可在 M9 Remediation（届时写命令数量增加）时再提取

**理由**：

- 全局 `~/.claude/CLAUDE.md`「写操作必须拒绝 root（EUID==0）」+ 项目 CLAUDE.md §4.5「写操作的硬约束」明确要求
- 写 `~/.config/hostlens/targets.yaml` 含凭据引用，以 root 跑会创建 root-owned 配置文件，后续普通用户跑命令时无法读 → 调试地狱
- 只读命令以 root 跑无副作用，强制拒绝反而妨碍合法运维场景（如 root daemon 调 `doctor --check-targets`）

**替代**：

- ❌ 全部 CLI 都拒绝 root：阻塞 daemon 运维场景
- ❌ 只 warn 不 exit：用户会忽视，留下 root-owned 文件雷区
- ❌ 抽 `require_unprivileged()` helper：M1 只有 2 个调用点，抽 helper 反而增加间接层；M9 多个写命令再抽即可

## 风险 / 权衡

### 风险

1. **SSH 集成测试的 CI 稳定性**：CI 起 docker sshd 容器有 cold start 时间（~10s）+ 偶发网络抖动 → 缓解：用 pytest-docker session-scoped fixture 复用容器；测试用 `pytest.mark.timeout(60)`；`pytest-rerunfailures` 仅对**显式标记** `@pytest.mark.flaky_ssh_integration` 的测试 retry 1 次（**禁止**全局 retry，避免掩盖真实 race 条件 bug）；retry 时必须保留首次失败日志 + container logs（pytest --capture=no 抓 stderr）
2. **AsyncSSH 依赖体积**：~2 MB，含 cryptography 子依赖 → 缓解：在 `pyproject.toml` 的 `[project.optional-dependencies]` 里分组（`core` 必装 / `ssh` 可选）—— 但 M1 SSHTarget 是核心场景，最终决定还是放 `core`，体积可接受
3. **明文密码 doctor warn 被忽略**：用户长期忽视 warning 把 prod 凭据明文写进 yaml → 缓解：M2+ 升级为加载时 error；M1 doctor warn 文本必须含修复步骤示例
4. **SSH env 注入限制踩坑**：用户配了 env 但 Inspector 跑不通 → 缓解：docs 顶部突出说明 + Inspector 提案（下一个）的 `secrets` 字段加载时强制走 env 路径并给出 sshd `AcceptEnv` 配置示例
5. **`exec` 收 string 引入 shell 注入风险**：本提案不做防御 → 缓解：本提案 spec 明确"shell 注入防御边界在 manifest 渲染层"；Inspector 提案必须实现 Jinja `| sh` filter 强制 + secret env-only 约束；本提案 docs 引用该边界
6. **SSH connection pool 引入新风险面**：长连接断开 / 服务端 idle disconnect / FD 泄漏 → 缓解：必须有 1 次自动重连（指数退避 1s → 4s → 16s，对齐 OPERABILITY §2.2）；必须有 `ssh.idle_timeout_seconds` 主动 close；测试覆盖"server 主动断开"场景；asyncssh 自带 `keepalive_interval` 默认 60s 帮助早发现
7. **`Capability` Enum 与既有 `CAPABILITY_ALLOWLIST` 同 PR 切换**：M2 已上线代码用 `{shell, file_read, file_write, docker, k8s_exec}`，新 Enum 会改这套；任何已有测试 fixture 含老值会同时挂 → 缓解：tasks.md 显式列"更新 CAPABILITY_ALLOWLIST + 配套测试"；本提案 PR 不允许只改一头

### 权衡

1. **`exec` shell-evaluated 换来 Inspector 表达力**：损失少量"argv 模式的安全感"，换来 manifest 可以写自然 shell 命令；M1 价值天平倾向后者
2. **SSH connection pool 复杂度 vs OPERABILITY 合规**：要写状态机 + 重连 + idle close，但 OPERABILITY 已硬性规定不可不做；M6 多 Inspector 场景下也会自然受益
3. **明文密码接受 + warn**：损失安全严格度，换来 demo 路径流畅；M1 不为安全洁癖牺牲上手体验
4. **SSH read_file 仅 SFTP（不 fallback cat）**：损失对禁用 sftp-server 的远端的支持，换来零 shell 注入 + 字节完整性；M1 用户面对的远端基本都启用 sftp-server，可接受
5. **不实现 capability 自动发现的完备性**：损失"零配置识别远端能力"，换来实现简单；用户偶尔遇到 false negative 时可手动在 yaml 标 capabilities

## Migration Plan

1. **Tool Registry `target_registry` 字段类型切换**：本提案 PR 同时修改 `src/hostlens/tools/base.py` 把 stub Protocol import 改为真实 `TargetRegistry`；M2 注释中的"M1 落地前可用 stub"删除
2. **`list_targets` handler 升级**：本提案 PR 同时让 `list_targets` handler 从真实 registry 取数据（之前返回空 list / 假数据）；M2 已通过的相关测试需要更新 fixture 提供真实 TargetRegistry
3. **配置文件向后兼容**：`targets.yaml` 是新文件，M0 用户没有 → 不需要迁移；`load_targets_config` 找不到文件时返回空 `TargetsConfig(version="1", targets=[])`（不报错），由后续 `build_registry_from_config` 决定如何构造空 registry；doctor 提示用户运行 `hostlens target add`
4. **回滚策略**：本提案 = 新增模块 + Tool Registry 一个字段类型切换；回滚 = `git revert <PR commit>` 即可（带走 stub Protocol 删除 + handler 迁移 + 字段类型切换三处的所有改动，原子还原到 M2 stub 状态）。**不**需要在 main 代码或测试 fixture 里保留 stub fallback —— task 8.4 明确禁止保留 stub fallback，因为 M1 落地后 stub 即死代码，留着会和真实类型在 mypy / 测试上互相干扰。回滚靠 git，不靠 fallback

## Open Questions

无 —— Protocol 与边界由 CLAUDE.md / ARCHITECTURE.md 钉死，决策都在上面列了替代方案。
