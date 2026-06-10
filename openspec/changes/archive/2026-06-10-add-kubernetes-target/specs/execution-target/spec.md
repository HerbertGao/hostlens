# execution-target 规范增量

## MODIFIED Requirements

### 需求:`Capability` Enum 必须含 M1 最小集且与 ToolRegistry allowlist 严格相等

`hostlens.targets.base.Capability` 必须是 `enum.Enum`，M1 阶段**恰好**含以下 5 个成员（不多不少）：

- `SHELL = "shell"`：能跑 shell 命令（所有 M1 target 都有）
- `FILE_READ = "file_read"`：能读文件（所有 M1 target 都有）
- `SSH = "ssh"`：通过 SSH 协议访问（仅 SSHTarget）
- `SYSTEMD = "systemd"`：远端有 systemd（运行时探测）
- `DOCKER_CLI = "docker_cli"`：远端能跑 `docker` CLI（运行时探测）

未来扩展由对应里程碑提案负责（**禁止**本提案预留 M9 才用的 placeholder）：M9 加 `FILE_WRITE` 等。**`add-kubernetes-target`（M8 K8s 半边）经评审决定 NOT 新增 Capability**——KubernetesTarget 提供既有 `{SHELL, FILE_READ}` 并懒探测既有 `SYSTEMD`/`DOCKER_CLI`（与 DockerTarget 同模型），inspector 声明的是 target-agnostic 能力（如 `SHELL`）而非 `K8S_EXEC`，故**无 `K8S_EXEC` 成员**；早期文档/注释里「M8 加 K8S_EXEC」的前向引用已 stale，由本提案订正（含 `base.py` / `list_targets.py` / `test_capability.py` 注释）。

Enum 成员名必须**全大写**，值必须**全小写**（与 docs/ARCHITECTURE.md §5 一致）。**禁止**在加载 Inspector manifest 时接受 Enum 之外的 capability token —— 未知 capability 必须在 manifest 加载时 raise（防止 silent skip）。

#### 场景:Capability 恰好含 M1 最小集

- **当** 检查 `set(Capability.__members__.keys())`
- **那么** 必须**恰好**为 `{"SHELL", "FILE_READ", "SSH", "SYSTEMD", "DOCKER_CLI"}`（不多不少；防止偷偷预留未来 milestone 才用的 placeholder；`add-kubernetes-target` 不加成员，集合保持 5 个）

#### 场景:Capability 值是小写 string

- **当** 检查每个 `Capability` 成员的 `.value`
- **那么** 必须是该成员名的 lower case（如 `Capability.SSH.value == "ssh"`）

#### 场景:capabilities 与 `CAPABILITY_ALLOWLIST` 严格相等

- **当** 同时检查 `frozenset({c.value for c in Capability})` 与 `hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST`
- **那么** 两者**必须严格相等**（M1 Capability Enum 是 SOT；M1 落地后 allowlist 必须更新为 `frozenset({c.value for c in Capability})`；原 M2 占位值 `file_write` / `docker` 必须删除，到对应 milestone 才回填——`k8s_exec` **不**回填，`add-kubernetes-target` 不引入该 capability）

### 需求:`TargetsConfig` 必须从 yaml 加载且环境变量占位展开

`hostlens.targets.config.TargetsConfig` 必须是 Pydantic v2 模型：

- 顶层结构：`version: Literal["1"]` + `targets: list[TargetEntry]`
- `TargetEntry` 通用字段（**所有 type 共有**）：
  - `name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_\-]{0,63}$")]`（必填；正则与 `ExecutionTarget.name` 约束严格一致 —— Pydantic 在 yaml 加载时 enforce，**禁止**仅在 Protocol 文档上声明而 loader 不校验）
  - `type: Literal["local", "ssh", "replay", "docker", "k8s"]`（必填，discriminator；`docker` 在 `add-docker-target` 提案落地路由到 `DockerEntry`，`k8s` 在 `add-kubernetes-target` 提案落地路由到 `K8sEntry`。`replay` 由 `replay-execution-target` spec 定义其 entry 字段集与运行时语义——本需求只负责把它纳入 discriminator 值域，不在此重复其字段说明）
  - `enabled: bool = True`（默认 enabled；可在 yaml 显式设 false 暂停某 target）。**disabled 行为约定**（**禁止**漂移）：
    - loader：`load_targets_config` 仍加载 disabled target 到 `TargetsConfig.targets`（**不**过滤）
    - registry 装配：`build_registry_from_config` 仍将 disabled target 注册到 registry（**不**过滤 —— 让 list_targets / doctor 能看到所有配置项，方便管理）
    - `registry.list()` / `list_entries()` / `names()` 返回所有 target（含 disabled）
    - `hostlens target test <name>` 对 disabled target 必须 exit 1 + stderr 含 `"target 'xxx' is disabled in targets.yaml"`，**不**触发连接
    - 任何 `ExecutionTarget.exec(...)` / `read_file(...)` 调用前必须检查 entry.enabled；disabled 时 raise `TargetError(kind="target_disabled", target=self.name)`，**不**触发底层连接
    - `hostlens doctor` 对 disabled target 标 `connectivity: "skipped"`（已在 doctor 需求里规定）
    - `list_targets` ToolSpec handler 行为对齐 M2 锁定的 `ListTargetsInput.include_disabled: bool = False` 字段语义：默认 `include_disabled=False` 时 handler **必须过滤掉** `enabled=False` 的 target（输出**只含** enabled）；`include_disabled=True` 时输出**所有** target（含 `enabled=False`），每条 `TargetSummary.enabled` 如实反映；这保持字段名与行为一致，避免"字段叫 include_disabled 但默认仍返回 disabled"的语义混乱
  - `display_name: str | None = None`（人类友好名，可选；缺省时 list_targets 投影用 `name`）
  - `description: str | None = None`（可选说明）
  - `tags: list[str] = Field(default_factory=list)`（默认空 list；Pydantic v2 必须用 `default_factory` 而不是可变默认值 `[]` 避免实例间共享；list_targets 投影直接透传）
- `TargetEntry` SSH-specific 字段集**恰好**为 `{host, user, port, key_path, password, passphrase, connect_timeout}`（7 个字段；`connect_timeout: int | None = None` 允许 per-target override `asyncssh.connect(connect_timeout=...)` 默认值；`extra="forbid"` 防 typo）
- `TargetEntry` Docker-specific 字段集（`type: docker` → `DockerEntry`，**恰好** 2 个字段，`extra="forbid"` 防 typo）：
  - `container: Annotated[str, Field(min_length=1)]`（必填，**非空**）—— 目标容器的 name 或 id。`min_length=1` 由 Pydantic 在 yaml 加载时 enforce（`container: ""` 必须 raise `pydantic.ValidationError`，**不**接受空字符串后在 runtime 才暴露成 `container_not_found`）
  - `docker_host: str | None = None`（可选）—— docker daemon 端点；缺省（`None`）时 DockerTarget 用 `docker.from_env()`（默认本机 `unix:///var/run/docker.sock`）。本提案范围内仅支持本机 unix socket / 无凭据端点；**远程 docker over TCP+TLS 的凭据加载是 follow-up（非目标），不在本字段语义内**。**docker_host 接受集精确定义**（loader 校验，非 `None` 时）：**接受集是唯一窄例外、拒绝是默认 catch-all**——`docker_host` 当且仅当满足「`startswith("unix:///")`（**大小写敏感**小写 `unix://` + **socket 路径以 `/` 开头的绝对路径**，即整体三斜杠起步 `unix:///...`）**且** `unix:///` 之后**非空**」时才接受；**其余任何值一律** raise `ConfigError(kind="docker_host_remote_not_supported", field="docker_host", target=target_name)`。示例性拒绝输入（非穷举，凡不满足接受谓词都拒）：① 远程 scheme（`tcp://` / `ssh://` / `http://` / `https://` / `npipe://`）；② 无 scheme 的裸路径（如 `/var/run/docker.sock`——docker-py 虽接受裸路径，但本提案要求显式 `unix://` 前缀消除歧义）；③ 空 socket 路径（`unix://`）；④ 大小写不符的 scheme（`UNIX://x` / `Unix://x`——`startswith` 大小写敏感，不满足接受谓词即落入默认拒绝）；⑤ socket 路径非绝对（`unix://foo`，相对无前导斜杠——歧义相对 socket，要求绝对路径消除歧义）。**不**静默接受明文 TCP / 裸路径 / 任何歧义端点（否则用户可配出无 TLS 的明文 daemon 连接或歧义端点，与本提案 Security「不扩大网络攻击面、仅本机 socket」声称冲突）
  - **凭据约定**：DockerEntry **不含**任何 secret 字段（本提案默认本机 socket）；`${ENV}` 占位规则对 DockerEntry 无适用 secret 字段——由于既有占位校验是**字段名 allowlist**（`${...}` 仅允许出现在 `password` / `passphrase`），`container` / `docker_host` 含 `${...}` 时已被既有 placeholder walker 自动拒绝为 `env_placeholder_not_allowed_here`，无需 DockerEntry 额外加逻辑
  - **两类校验的执行顺序**（必须固定）：`${...}` 占位拒绝发生在 `_expand_placeholders` 阶段（`model_validate` **之前**，字段名 allowlist 判定）；`docker_host` 的 `unix://` scheme 校验需要 typed `DockerEntry`，发生在 `model_validate` **之后**（实现为遍历 typed entries 的 loader 步骤，或 `DockerEntry` 的 `field_validator` 中 raise `ConfigError` —— **不**能用 `Field(pattern=...)`，因为它 raise `ValidationError` 而非 `ConfigError`）。故 `docker_host: ${X}` 必须**先**命中 `env_placeholder_not_allowed_here`（占位阶段），轮不到 scheme 校验
- `TargetEntry` K8s-specific 字段集（`type: k8s` → `K8sEntry`，**恰好** 5 个字段，`extra="forbid"` 防 typo）：
  - `pod: Annotated[str, Field(min_length=1)]`（必填，**非空**）—— 目标 pod 名。`min_length=1` 由 Pydantic 在 yaml 加载时 enforce（`pod: ""` 必须 raise `pydantic.ValidationError`，不接受空 pod 引用后在 runtime 才暴露成 `pod_not_found`）
  - `namespace: str = "default"`（可选，默认 `"default"`）—— pod 所在 namespace
  - `container: str | None = None`（可选）—— 多容器 pod 内指定容器名；缺省（`None`）走 pod 默认容器（k8s exec API `container=None`）。**与 DockerEntry.container 语义不同**：DockerEntry.container 是必填的容器引用，K8sEntry.container 是 pod 内可选的容器选择器
  - `kubeconfig: str | None = None`（可选）—— kubeconfig 文件路径；缺省时按标准顺序解析（`$KUBECONFIG` / `~/.kube/config`），或在 in-cluster 环境用 ServiceAccount。**路径本身非 secret**（凭据在文件内容 / ServiceAccount token，不落 yaml）
  - `context: str | None = None`（可选）—— kubeconfig 内的 context 名；缺省用 kubeconfig 的 current-context
  - **凭据约定**：K8sEntry **不含**任何明文 secret 字段（凭据走 kubeconfig 文件 / in-cluster ServiceAccount，配置只存路径/context 引用）；`${...}` 占位对 K8sEntry 所有字段（`pod` / `namespace` / `container` / `kubeconfig` / `context`）均**不允许**——它们都是非 secret 字段，含 `${...}` 时被既有 placeholder walker 拒绝为 `env_placeholder_not_allowed_here`
- **凭据字段命名约定**（与 CLI 参数 + proposal Demo Path 严格一致）：
  - `key_path: str | None` —— SSH 私钥文件路径（路径本身非 secret，文件内容才是）；CLI 参数 `--key-path PATH`
  - `password: str | None` —— SSH 密码；CLI 参数 `--password-env VAR`（CLI 不接受明文 `--password`，仅 env 占位）；yaml 中可以是 `${VAR}` 占位或字面值（字面值触发 doctor warn）
  - `passphrase: str | None` —— 加密私钥的 passphrase；CLI 参数 `--passphrase-env VAR`；yaml 同 `password` 规则
- yaml 中 `${VAR_NAME}` 占位必须在加载时展开（从 `os.environ` 读取）；未设置时 raise `ConfigError(kind="missing_env_var", var_name=VAR_NAME, target=target_name)`（依赖 M1 落地的 ConfigError 扩展，见下方需求 §需求:`ConfigError` 必须扩展支持结构化 kind/extra 字段）
- `${...}` 占位**仅**允许出现在 secret 字段（`password` / `passphrase`）—— 出现在 `host` / `user` / `port` / `key_path` / `container` / `docker_host` / `pod` / `namespace` / `kubeconfig` / `context` 等非 secret 字段时 raise `ConfigError(kind="env_placeholder_not_allowed_here", field=field_name, target=target_name)`
- 加载文件不存在时返回空 `TargetsConfig(version="1", targets=[])`（**不**是 TargetRegistry——装配 registry 由 `build_registry_from_config` 负责）；`load_targets_config` 必须**同时**通过 structlog 输出 INFO 级日志「config file not found, returning empty TargetsConfig」+ doctor 会以 hint 状态显示「没有任何已配置 target，跑 `hostlens target add` 开始」—— 不 raise 但也不静默通过

加载入口：`hostlens.targets.config.load_targets_config(path: Path) -> TargetsConfig`

#### 场景:`${ENV}` 占位展开

- **当** yaml 含 `password: ${HOSTLENS_DEMO_PWD}`，环境变量 `HOSTLENS_DEMO_PWD=demo123`
- **那么** 加载后的 `TargetEntry.password == "demo123"`（占位被替换）

#### 场景:env 未设置 raise ConfigError

- **当** yaml 含 `password: ${UNSET_VAR}`，环境无 `UNSET_VAR`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"missing_env_var"`，含 var 名与 target name

#### 场景:占位出现在非 secret 字段 raise

- **当** yaml 含 `host: ${HOST_PLACEHOLDER}` 或 `user: ${USER_PLACEHOLDER}` 或 `container: ${CONTAINER_PLACEHOLDER}`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"env_placeholder_not_allowed_here"`（防止 host/user/container 通过 env 注入意外暴露）

#### 场景:配置文件不存在返回空 TargetsConfig

- **当** `~/.config/hostlens/targets.yaml` 不存在
- **那么** `load_targets_config(path)` 必须返回 `TargetsConfig(version="1", targets=[])`，**不** raise

#### 场景:unknown type raise

- **当** yaml 含 `type: vm`
- **那么** 加载必须 raise `pydantic.ValidationError`（type 字段是 Literal）

#### 场景:TargetEntry name 不匹配正则 raise

- **当** yaml 含 `name: Prod-Web`（含大写）或 `name: 1web`（数字开头）或 `name: prod web`（含空格）
- **那么** 加载必须 raise `pydantic.ValidationError`，错误指明 name 必须匹配 `^[a-z][a-z0-9_\-]{0,63}$`（loader 强制 enforce，不依赖 ExecutionTarget Protocol 文档的声明）

#### 场景:TargetEntry SSH 字段集严格

- **当** SSH `TargetEntry` 实例化时多传一个未声明字段（如 `agent_forwarding=True` 或 `compression=False`）
- **那么** 必须 raise `pydantic.ValidationError`（`extra="forbid"`），错误指明 unknown field name
- **且** SSH 字段集**恰好**是 `{host, user, port, key_path, password, passphrase, connect_timeout}` 7 个

#### 场景:TargetEntry docker 字段集严格

- **当** yaml 含 `type: docker` 且省略 `container` 字段
- **那么** 加载必须 raise `pydantic.ValidationError`（`container` 必填）
- **且** `container: ""`（空字符串）必须 raise `pydantic.ValidationError`（`min_length=1`），**不**接受空容器引用
- **且** docker `TargetEntry` 多传未声明字段（如 `host=...` 或 `image=...`）必须 raise `pydantic.ValidationError`（`extra="forbid"`）；Docker-specific 字段集**恰好**是 `{container, docker_host}` 2 个

#### 场景:type docker 路由到 DockerEntry

- **当** yaml 含 `name: web-ct`、`type: docker`、`container: my-app`
- **那么** 加载后该条目必须是 `DockerEntry` 实例，`type == "docker"`、`container == "my-app"`、`docker_host is None`

#### 场景:docker_host 远程 scheme 被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: tcp://10.0.0.5:2376`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（只允许 `unix://` 或缺省；不静默接受明文 TCP 端点）

#### 场景:docker_host 无 scheme 裸路径被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: /var/run/docker.sock`（无 `unix://` 前缀的裸路径）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（要求显式 `unix://` 前缀消除歧义；裸路径与空 `unix://` 同样拒绝）

#### 场景:docker_host 空 unix:// 被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: unix://`（`unix://` 之后 socket 路径为空）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（socket 路径必须非空）

#### 场景:docker_host 相对 socket 路径被拒

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: unix://foo`（socket 路径 `foo` 相对、无前导 `/`）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"docker_host_remote_not_supported"`（socket 路径须绝对，`unix:///...` 三斜杠起步）

#### 场景:docker_host 合法 unix:// 被接受

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: unix:///var/run/docker.sock`
- **那么** 加载必须**成功**，该条目是 `DockerEntry`、`docker_host == "unix:///var/run/docker.sock"`（合法本机 socket 端点被保留，**不** raise——验证 loader 不是「全拒型」）

#### 场景:docker_host 占位先于 scheme 校验命中

- **当** yaml 含 `type: docker`、`container: x`、`docker_host: ${SOME_VAR}`
- **那么** 加载必须 raise `ConfigError`，kind 为 `"env_placeholder_not_allowed_here"`（占位拒绝在 `model_validate` 前的字段名 allowlist 阶段命中，**先于** `unix://` scheme 校验）

#### 场景:type k8s 路由到 K8sEntry

- **当** yaml 含 `name: web-pod`、`type: k8s`、`pod: my-app`
- **那么** 加载后该条目必须是 `K8sEntry` 实例，`type == "k8s"`、`pod == "my-app"`、`namespace == "default"`（缺省）、`container is None`、`kubeconfig is None`、`context is None`

#### 场景:TargetEntry k8s 字段集严格

- **当** yaml 含 `type: k8s` 且省略 `pod` 字段
- **那么** 加载必须 raise `pydantic.ValidationError`（`pod` 必填）
- **且** `pod: ""`（空字符串）必须 raise `pydantic.ValidationError`（`min_length=1`），**不**接受空 pod 引用
- **且** k8s `TargetEntry` 多传未声明字段（如 `image=...` 或 `host=...`）必须 raise `pydantic.ValidationError`（`extra="forbid"`）；K8s-specific 字段集**恰好**是 `{pod, namespace, container, kubeconfig, context}` 5 个

#### 场景:k8s 非 secret 字段占位被拒

- **当** yaml 含 `pod: ${POD_VAR}`（或 `namespace: ${NS_VAR}` / `kubeconfig: ${KC}` / `context: ${CTX}` / `container: ${C}`，type: k8s）
- **那么** 加载必须 raise `ConfigError`，kind 为 `"env_placeholder_not_allowed_here"`（K8sEntry **全 5 字段** pod/namespace/container/kubeconfig/context 均非 secret，不允许 env 占位）
