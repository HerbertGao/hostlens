## 为什么

M1 第一块基石。Hostlens 所有巡检最终都要"在某个地方执行命令并读文件"——本地子进程 / 远程 SSH / Docker / K8s pod 都不一样。如果 Inspector 直接 import `asyncio.create_subprocess_shell` 或 `asyncssh`，未来 M8 加 Docker / K8s 时 Inspector 要全部改一遍；而且 Tool Registry 已落地的 `ToolContext.target_registry`（M2 首批 ToolSpec `list_targets` / `run_inspector` 都要它）目前是 stub Protocol，再不落地 M2 就无法 dispatch。

CLAUDE.md §4.3 与 docs/ARCHITECTURE.md §5 已经把 Protocol 形状钉死（`name` / `type` / `exec(cmd, *, timeout, env)` / `read_file(path)` / `capabilities`）。这次提案的任务是把契约从架构文档搬进 spec 与 `src/hostlens/targets/`，并交付 **LocalTarget + SSHTarget** 两个 M1 必需实现 + 一个 `TargetRegistry` + 一组 `hostlens target` CLI 命令，让"非 root 用户 → 写 yaml 配 SSH 主机 → 跑命令拿结果"端到端可跑。

不在本提案范围：DockerTarget / KubernetesTarget（M8 单独提案）、Inspector 调度逻辑（下一个提案 add-inspector-plugin-system）、target 凭据的高级管理（macOS Keychain / SOPS 留到 M5+ 路线）。

## 变更内容

**新增（execution-target 核心 Protocol 与基础类型）：**

- `hostlens.targets.base.ExecutionTarget` Protocol：`name` / `type` / `async exec(cmd, *, timeout, env)` / `async read_file(path)` / `capabilities` 属性
- `hostlens.targets.base.Capability` Enum：`SSH` / `SYSTEMD` / `DOCKER_CLI` / `FILE_READ` / `SHELL`（M1 最小集；M6+ 按需扩）
- `hostlens.targets.base.ExecResult` Pydantic 模型：`exit_code` / `stdout` / `stderr` / `duration_seconds` / `timed_out`（明确 `timed_out=True` 与 `exit_code != 0` 是两种独立状态）
- `hostlens.targets.local.LocalTarget`：`asyncio.create_subprocess_shell(cmd, env=...)` 实现；`type="local"`；capabilities = `{SHELL, FILE_READ}` + 运行时探测 docker CLI 可加 `DOCKER_CLI`
- `hostlens.targets.registry.TargetRegistry`：按 name 索引 target 实例；`register(target)` / `get(name)` / `names()` / `list()` 接口；name 冲突 raise `TargetError`
- `hostlens.targets.config.TargetsConfig` Pydantic 模型 + loader：从 `~/.config/hostlens/targets.yaml` 加载 target 配置（M1 LocalTarget 单条配置即可；SSHTarget 见下）

**新增（ssh-execution-target 实现 + 凭据约束）：**

- `hostlens.targets.ssh.SSHTarget`：基于 `asyncssh` 的实现；`type="ssh"`；capabilities = `{SSH, SHELL, FILE_READ, SYSTEMD}`（SYSTEMD 在 capability 检测时按需加）
- 凭据加载：**仅支持 key 认证为主**（M1 也支持 password 但 doctor 必须 warn）；private_key path 与 password 都通过 `${ENV_VAR}` 占位从环境读，**禁止**明文落 yaml
- SSH env 传递的限制：远端 sshd 默认 `AcceptEnv` 仅允许 `LANG LC_*`；Hostlens 不假装能传任意 env，docs 与 doctor 必须明确说明 —— 真正传 secret 走 stdin 或临时 export 命令（M1 先实现 env 注入接口，docs 写清限制）
- 集成测试：CI 起 `linuxserver/openssh-server` 容器跑真实 sshd（**不**mock `asyncssh`，按 CLAUDE.md §6 测试规则）

**新增（CLI 命令集）：**

- `hostlens target add <name> --type local|ssh [--host ... --user ... --key-env VAR --port 22]`：写 yaml + 校验
- `hostlens target list [--json]`：列出已配置 target + 是否启用 + capability 集合
- `hostlens target remove <name>`：从 yaml 删除（默认交互确认，`--yes` 跳过；非交互无 `--yes` exit 1）
- `hostlens target test <name>`：跑一次 `echo hostlens-probe-$$` 验证连通性 + capability 探测
- `hostlens doctor` 增加 `targets` section：每个 target 连通性 + 凭据来源 + 明文密码警告

**修订（Tool Registry stub Protocol 落地）：**

- `hostlens.tools.base.ToolContext.target_registry` 字段从 stub Protocol 切到真实 `TargetRegistry` 类型
- `register_default_tools` 注入的 `list_targets` handler 现在能拿到真实 target 列表（之前 M2 提案标注"M1 落地前可用 stub"）；`TargetSummary` 字段映射（name / kind / display_name / description / capabilities / tags / enabled）从真实 target 派生

## 功能 (Capabilities)

### 新增功能

- `execution-target`: `ExecutionTarget` Protocol、`Capability` enum、`ExecResult` 模型、`TargetRegistry`、`LocalTarget` 实现、`hostlens target` CLI 命令集与 `targets.yaml` 配置加载
- `ssh-execution-target`: `SSHTarget` 实现、凭据从环境变量加载的约束、env 注入限制说明、集成测试用真实 sshd 容器

### 修改功能

- `tool-registry-capability-layer`: `ToolContext.target_registry` 字段类型从 stub Protocol 切到真实 `TargetRegistry`；`list_targets` 与 `run_inspector` ToolSpec 的 handler 现在能拿到真实 target 数据（M2 提案中标注的"stub 占位"被替换）

## 影响

**代码：**

- 新增 `src/hostlens/targets/{__init__.py, base.py, local.py, ssh.py, registry.py, config.py}`
- 新增 `src/hostlens/cli/target.py`（Typer 子命令组）；注册到 `cli/__init__.py`
- 修改 `src/hostlens/cli/doctor.py`：增加 targets 健康检查 section
- 修改 `src/hostlens/tools/base.py`：把 `TargetRegistry` 从 stub Protocol 切到真实类型 import；M2 落地的 stub 删除
- 修改 `src/hostlens/tools/default_tools.py`：`list_targets` handler 接通真实 registry 数据
- 新增测试：`tests/targets/test_local.py`、`tests/targets/test_ssh_integration.py`（docker-based）、`tests/cli/test_target.py`、`tests/tools/test_list_targets_with_real_registry.py`

**依赖：**

- 新增 runtime 依赖：`asyncssh ^2.18`
- 新增 dev 依赖：`pytest-docker ^3.1`（用于 sshd 容器集成测试 fixture）

**配置文件：**

- 新增 `~/.config/hostlens/targets.yaml` 约定路径；M0 已落地的 `Settings` 增加 `targets_config_path` 字段（默认 `~/.config/hostlens/targets.yaml`）

**文档：**

- 更新 `docs/ARCHITECTURE.md` §5：把"M1 落地"标注从"待办"改为本提案 PR 编号
- 新增 `docs/operations/targets.md`：targets.yaml 配置示例 + SSH 凭据 best practice + 远端 sshd `AcceptEnv` 限制说明
- README "快速开始"小节增加 `hostlens target add` 示例

**对外契约影响：**

- **CLI 命令**：新增 `hostlens target` 子命令组（add / list / remove / test）—— 这是 M0 之后第一次扩展 CLI 表面
- **Inspector schema**（未来）：M1 下一提案 `add-inspector-plugin-system` 的 `targets:` 字段值域 = 本提案落地的 target `type` 枚举（`local` / `ssh`）
- **Agent tool schema**：`list_targets` ToolSpec 输出从 stub 切到真实数据；`TargetSummary` schema 不变（已在 tool-registry-capability-layer spec 锁定）
- **MCP tool schema**：M7 才暴露，本提案不影响
- **Notifier Protocol / Schedule manifest**：不影响

## 非目标（Non-Goals）

明确**不在**本提案范围，防止范围蔓延：

- ❌ DockerTarget / KubernetesTarget 实现（M8 单独提案）
- ❌ macOS Keychain / Linux Secret Service / SOPS 加密密钥（M5+ 路线，本提案仅支持环境变量占位）
- ❌ SSH 连接复用 / multiplex（`ControlMaster`）：M1 每次 exec 新建连接；性能优化推到 M6 之后基于真实 benchmark 再决定
- ❌ Bastion / Jump Host / Agent forwarding：M1 直连，bastion 推到有用户需求时
- ❌ SSH password 加密存储：M1 password 必须走 `${ENV_VAR}` 占位（明文落 yaml → doctor warn）
- ❌ Inspector 调度逻辑：下一提案 `add-inspector-plugin-system` 处理
- ❌ Capability 自动发现的完备性：M1 只做 SSH/LOCAL 基础检测；SYSTEMD / DOCKER_CLI 探测靠运行时跑 `systemctl --version` / `docker --version` 简单 probe，false negative 可接受
- ❌ 写操作 target API：M1 `exec` 只用于读类命令（与 M2 ToolRegistry `side_effects ∈ {none, read}` 一致）；M9 Remediation 才扩展写语义
- ❌ Target health 持续监控 / 自动 disable 失败 target：M1 失败由调用方处理，不做后台守护

## Failure Modes

| 故障 | 行为 | 用户可见状态 |
|---|---|---|
| LocalTarget exec 超时 | `asyncio.wait_for` 取消 subprocess + 发 SIGKILL；返回 `ExecResult(timed_out=True, exit_code=-1)` | finding-level: `inspector_status: timeout`（M1 下一提案体现）；本提案 ExecResult 必须把 `timed_out` 与 `exit_code` 分开 |
| SSHTarget 主机不可达（DNS / 拒绝 / 防火墙） | 单 target 标 `target_unreachable`；其它 target 继续；error log 含目标 host + 错误码（**不含**凭据） | run status: `partial`（M4 Scheduler RunStatus 兜底） |
| SSHTarget 凭据错误（key permission denied / wrong password） | raise `TargetError("ssh_auth_failed", target=name)`；CLI 显示 `hostlens target test <name>` 建议 | exit 1；doctor 也能预先 detect |
| `targets.yaml` 引用的 `${ENV_VAR}` 未设置 | 加载时 raise `ConfigError("missing_env_var", var_name=name, target=name)`；**禁止**默默用空 string 当 password | hostlens doctor exit 1，CLI 命令启动 fail-fast |
| SSH env 注入但远端 sshd 拒收（`AcceptEnv` 限制） | env 被远端静默丢弃；本地无法检测；docs 必须说明；M1 不在 runtime 验证（成本太高） | docs 警告 |
| LocalTarget read_file 路径不存在 | raise `FileNotFoundError`；上层（Inspector）按 `requires_files` 自动 skip | finding-level: `requires_unmet` |

## Operational Limits

参考 docs/OPERABILITY.md §1：

- **单 Inspector exec 默认超时**：60s（`concurrency.inspector_timeout_seconds`）；本提案的 `ExecutionTarget.exec(timeout=...)` 接受调用方传入值，**不**在 target 层加默认
- **单 target 并行 exec 数**：4（`concurrency.max_concurrent_inspectors_per_target`）—— 本提案不实现 semaphore（Inspector Runner 层负责，下一提案）；但 SSHTarget 内部**必须**为每个 exec 独立新建 SSH connection（M1 不复用），保证并行调用不竞争同一 channel
- **同时巡检 target 数**：8（`concurrency.max_concurrent_targets`）—— 同上，本提案不实现
- **TargetRegistry 实例数**：进程内单例，由 Settings 注入时构造
- **SSH 连接建立超时**：10s（asyncssh `connect_timeout`），可在 targets.yaml per-target 配
- **read_file 大小上限**：M1 默认 10 MB，超出 raise `TargetError("file_too_large")`，防止 SSH 一次拉巨大日志 OOM

## Security & Secrets

参考 docs/OPERABILITY.md §7：

- **密钥来源**：仅环境变量（`${ENV_VAR}` 占位）；macOS Keychain / SOPS 加密留到 M5+
- **明文密码警告**：`targets.yaml` 中 `password` 字段不通过 `${...}` 占位（即字面量） → loader 接受但 doctor 标 warning；future M2+ 可升级为加载时 error
- **凭据脱敏**：所有 SSH 连接错误日志必须经过 `core/logging.redact_sensitive`；error message 与 traceback **禁止**含 password / private_key 内容
- **攻击面**：新增 SSH client 能力 = 给 hostlens 进程加了对外发起 SSH 连接的能力；不引入入站监听端口；M9 Remediation 才会有"通过 SSH 改远端状态"的能力（届时再做 RBAC）
- **EUID == 0 拒绝**：本提案 CLI 命令 `target add` / `target remove` 不直接触发远端写操作，按 CLAUDE.md §4.5 仍可允许 root 跑（M9 Remediation 时才强制拒绝）；但 doctor 必须警告 root 运行 daemon

## Cost / Quota Impact

参考 docs/OPERABILITY.md §3：

- **零 LLM 调用**：本提案纯基础设施，不调 Anthropic API
- **零 token 消耗**：CI 集成测试不需要 cassette（不调 LLM）
- **Anthropic 配额**：不影响（仅基础设施）
- **未来影响**：M2 Agent loop 通过 `run_inspector` ToolSpec 调本提案接口；每次 inspector run 的 LLM token = M2 提案预算

## Demo Path

交付后 5 分钟本地 reproduce（**无 SSH 真实服务器、无付费 API**）：

1. `pip install -e ".[dev]"`
2. 启 sshd 容器：`docker run -d -p 2222:2222 -e USER_NAME=hostlens -e PASSWORD_ACCESS=true -e USER_PASSWORD=demo linuxserver/openssh-server`
3. 配 LocalTarget：`hostlens target add my-local --type local`
4. 配 SSHTarget：`HOSTLENS_DEMO_SSH_PASSWORD=demo hostlens target add my-ssh --type ssh --host localhost --port 2222 --user hostlens --password-env HOSTLENS_DEMO_SSH_PASSWORD`
5. 验证：`hostlens target list --json | jq` —— 看到两个 target 与各自 capabilities
6. 连通性：`hostlens target test my-local` 与 `hostlens target test my-ssh` 都返回 ok + 探测到的 capabilities
7. doctor：`hostlens doctor --json | jq .targets` —— 看到健康状态
8. （选）跑 M2 已落地的 `list_targets` ToolSpec：在 Python REPL 里 `from hostlens.tools.default_tools import register_default_tools; from hostlens.tools.registry import ToolRegistry; r = ToolRegistry(); register_default_tools(r); print(asyncio.run(r.dispatch("list_targets", ListTargetsInput(), ctx)).model_dump())` 验证 target_registry 注入链路通了

记录在 `examples/m1-targets/README.md`。
