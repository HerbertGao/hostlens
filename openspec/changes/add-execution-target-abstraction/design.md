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
2. SSH 连接复用（`ControlMaster` / multiplex）—— 性能优化；M1 每次 exec 新建连接，benchmark 验证有性能问题后再做
3. macOS Keychain / Linux Secret Service / SOPS 加密密钥来源 —— M5+ 路线
4. SSH bastion / jump host / agent forwarding —— 用户需求出现后再做
5. `exec` 的写操作语义（"返回的 exit_code 之外允许修改远端状态"）—— M9 Remediation 才扩展；M1 `exec` 用于读类命令，与 Tool Registry `side_effects ∈ {none, read}` 一致
6. Target 自动 health 监控 / 自动 disable 失败 target —— 失败由调用方处理，不做后台守护
7. Capability 自动发现的完备性 —— M1 只做基础探测（SSH 默认 + 运行时 `systemctl --version` / `docker --version` probe），false negative 可接受

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

### 决策 3：`ExecResult.timed_out` 与 `exit_code` 字段分离

**选择**：`timed_out: bool` 与 `exit_code: int` 是独立字段，超时时 `timed_out=True, exit_code=-1`

**理由**：

- 调用方区分"命令跑完但返回非零"与"命令被 hostlens 主动取消"的语义需求强（前者 = 业务诊断信号，后者 = hostlens 自身配额信号）
- 单字段方案（用 `exit_code=-1` 表示超时）会让调用方写 `if result.exit_code != 0` 时把超时也当业务失败处理
- 显式 `timed_out` 配合 M4 Scheduler 的 RunStatus 与 M3 Diagnostician 的 finding `inspector_status: timeout` 自然对齐

**替代**：

- ❌ 单字段 `exit_code` 用魔数（-1 / -2 / -3）区分超时 / 取消 / 系统错误：语义不清
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

### 决策 6：SSHTarget 每次 exec 新建连接（M1 不复用）

**选择**：每次 `await target.exec()` 调用走 `asyncssh.connect()` → run → close 完整生命周期

**理由**：

- 实现简单，并行 exec 不竞争同一 channel
- M1 demo 路径每次 inspect 跑 1-3 个 Inspector，连接建立成本（~100-300ms）相对 Inspector exec 时间（多数 1-30s）可接受
- 连接复用引入的复杂性（pool / health check / per-target lock / 长连接 timeout）在没有 benchmark 数据前不值得做
- M6+ Inspector 数量上来后，再基于真实 metric 决定是否引入 connection pool（届时 SSHTarget 内部加 pool，对外 Protocol 不变）

**替代**：

- ❌ ControlMaster / connection pool：复杂度高；M1 还看不出 ROI
- ❌ 长连接 + keepalive：N×M 个 target × inspector 会爆连接数

### 决策 7：SSH `env` 注入接受调用方的 dict 但 docs 说明远端 sshd `AcceptEnv` 限制

**选择**：

- `SSHTarget.exec(env=...)` 内部把 dict 传给 `asyncssh.run(env=env)`
- 远端 sshd 默认只 `AcceptEnv LANG LC_*` —— 大多数 env var 会被静默丢弃
- docs 与 doctor 必须明确说明这一限制；M1 不在 runtime 验证（验证成本太高 + false alarm 率高）
- 真正传 secret 的 Inspector：要么走 stdin（更安全），要么走 cmd 内 `export VAR=...; actual_cmd`（loader 校验拒绝 secret 直接进 cmd）

**理由**：

- 假装"env 注入永远生效"会让用户配了 `PGPASSWORD` 跑 `mysql -p` 后失败但不知道为什么
- M1 把限制写明文档，比沉默失败好
- 验证 sshd `AcceptEnv` 需要远端配置读取或试错探测，成本与价值不匹配

**替代**：

- ❌ 把 env 转换为 `export VAR=val; cmd` 拼到 cmd string：secret 会进 process list 与 shell history，安全倒退
- ❌ 强制远端 sshd 加 `AcceptEnv *`：要求用户改 server 配置，体验差

### 决策 8：`TargetsConfig` 是 Pydantic 模型，targets.yaml 由 loader 显式校验

**选择**：

- `hostlens.targets.config.TargetsConfig` Pydantic v2 模型，含 `version: Literal["1"]` + `targets: list[TargetEntry]`
- 加载错误（schema / env_var 未设置 / 明文密码 / unknown type）有清晰文件路径 + 字段级 error
- 与 M0 `Settings` 风格一致（pydantic-settings）

**理由**：

- Pydantic 的 ValidationError 自动给出字段路径（`targets[1].password`），用户友好
- 与 Inspector manifest（下一提案）的加载方式对齐，降低维护成本

**替代**：

- ❌ 直接 `yaml.safe_load()` 后手工 dict 访问：报错不友好
- ❌ JSON Schema 校验：与项目其他模块不一致

## 风险 / 权衡

### 风险

1. **SSH 集成测试的 CI 稳定性**：CI 起 docker sshd 容器有 cold start 时间（~10s）+ 偶发网络抖动 → 缓解：用 pytest-docker fixture 复用容器；测试用 `pytest.mark.timeout(60)`；CI 失败时 retry 1 次（不是无限）
2. **AsyncSSH 依赖体积**：~2 MB，含 cryptography 子依赖 → 缓解：在 `pyproject.toml` 的 `[project.optional-dependencies]` 里分组（`core` 必装 / `ssh` 可选）—— 但 M1 SSHTarget 是核心场景，最终决定还是放 `core`，体积可接受
3. **明文密码 doctor warn 被忽略**：用户长期忽视 warning 把 prod 凭据明文写进 yaml → 缓解：M2+ 升级为加载时 error；M1 doctor warn 文本必须含修复步骤示例
4. **SSH env 注入限制踩坑**：用户配了 env 但 Inspector 跑不通 → 缓解：docs 顶部突出说明 + Inspector 提案（下一个）的 `secrets` 字段加载时强制走 env 路径并给出 sshd `AcceptEnv` 配置示例
5. **`exec` 收 string 引入 shell 注入风险**：本提案不做防御 → 缓解：本提案 spec 明确"shell 注入防御边界在 manifest 渲染层"；Inspector 提案必须实现 Jinja `| sh` filter 强制 + secret env-only 约束；本提案 docs 引用该边界

### 权衡

1. **`exec` shell-evaluated 换来 Inspector 表达力**：损失少量"argv 模式的安全感"，换来 manifest 可以写自然 shell 命令；M1 价值天平倾向后者
2. **每次 SSH exec 新建连接换来实现简单**：损失性能（每次 +100-300ms），换来代码简洁与并行无锁；M1 demo 路径可接受
3. **明文密码接受 + warn**：损失安全严格度，换来 demo 路径流畅；M1 不为安全洁癖牺牲上手体验
4. **不实现 capability 自动发现的完备性**：损失"零配置识别远端能力"，换来实现简单；用户偶尔遇到 false negative 时可手动在 yaml 标 capabilities

## Migration Plan

1. **Tool Registry `target_registry` 字段类型切换**：本提案 PR 同时修改 `src/hostlens/tools/base.py` 把 stub Protocol import 改为真实 `TargetRegistry`；M2 注释中的"M1 落地前可用 stub"删除
2. **`list_targets` handler 升级**：本提案 PR 同时让 `list_targets` handler 从真实 registry 取数据（之前返回空 list / 假数据）；M2 已通过的相关测试需要更新 fixture 提供真实 TargetRegistry
3. **配置文件向后兼容**：`targets.yaml` 是新文件，M0 用户没有 → 不需要迁移；loader 找不到文件时返回空 registry（不报错），doctor 提示用户运行 `hostlens target add`
4. **回滚策略**：本提案 = 新增模块 + Tool Registry 一个字段类型切换；回滚 = revert PR；M0 / M2 不依赖本提案的运行时数据，回滚后 `list_targets` 退回到 stub 状态（M2 测试需保留 stub fallback fixture 以便回滚）

## Open Questions

无 —— Protocol 与边界由 CLAUDE.md / ARCHITECTURE.md 钉死，决策都在上面列了替代方案。
