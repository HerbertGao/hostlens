## 修改需求

### 需求:`ToolContext` 必须包含 M2 字段最小集且禁止持有 LLMBackend

`hostlens.tools.base.ToolContext` 必须是 dataclass（`@dataclass(frozen=True)`），M2 字段集**恰好**为：

- `target_registry: TargetRegistry`（**M1 已落地，必须 import 自 `hostlens.targets.registry.TargetRegistry`；禁止保留 stub Protocol fallback**）
- `inspector_registry: InspectorRegistry`（M1 落地前可用 stub Protocol —— 本变更不动 InspectorRegistry，下一提案 `add-inspector-plugin-system` 再切真实类型）
- `config: Settings`（M0 已落地）
- `logger: structlog.BoundLogger`
- `approval_service: ApprovalService`（M2 必须传 `NoopApprovalService` 真实实例，**禁止** `None`）
- `cancel: asyncio.Event`

**禁止**字段：`llm_backend` / `anthropic_client` / `messages_create` 等任何 LLM 调用入口（ADR-008：Backend 是 AgentLoop 私有依赖）。

#### 场景:ToolContext 字段集严格

- **当** 检查 `dataclasses.fields(ToolContext)` 的 name 集合
- **那么** 必须**恰好**返回 `{"target_registry", "inspector_registry", "config", "logger", "approval_service", "cancel"}`（不多不少）

#### 场景:ToolContext 实例不可变

- **当** 已实例化的 `ctx` 试图赋值 `ctx.logger = other_logger`
- **那么** 必须 raise `dataclasses.FrozenInstanceError`

#### 场景:approval_service 不允许 None

- **当** 试图实例化 `ToolContext(..., approval_service=None)`
- **那么** 必须在类型检查阶段（mypy --strict）报错（`ApprovalService` 不是 `Optional`）；运行时调用 `ctx.approval_service.request_approval(...)` 应使用 `NoopApprovalService` 的真实实现

#### 场景:target_registry 是真实 TargetRegistry 类型

- **当** 检查 `ToolContext.__annotations__["target_registry"]`
- **那么** 必须解析为 `hostlens.targets.registry.TargetRegistry` 真实类型（**不**是 stub Protocol 或 `typing.Any`）
- **且** `hostlens.tools.base` 模块的 import 段必须含 `from hostlens.targets.registry import TargetRegistry`，**禁止**保留 stub Protocol 类定义或 `if TYPE_CHECKING: ...` 的 Protocol fallback
- **且** 旧 stub Protocol 上的 `list_summaries()` 方法签名必须从 `hostlens.tools.base` 中**完全删除**（**禁止**保留为 backward compat 别名 —— 真实 `TargetRegistry.list()` 取代它）

### 需求:M2 首批 ToolSpec 必须含 `run_inspector` / `list_inspectors` / `list_targets`

`hostlens.tools.default_tools` 模块必须导出 3 个 ToolSpec，policy 元数据严格按以下取值：

| ToolSpec | surfaces | side_effects | sensitive_output | requires_approval | timeout |
|---|---|---|---|---|---|
| `run_inspector` | `{"agent"}` | `"read"` | `True` | `False` | 30.0 |
| `list_inspectors` | `{"agent"}` | `"none"` | `False` | `False` | 5.0 |
| `list_targets` | `{"agent"}` | `"none"` | `True` | `False` | 5.0 |

**handler 实现契约（M1 落地后变更）**：

- M2 stub 阶段 `list_targets_handler` 调用 `ctx.target_registry.list_summaries()` —— 该方法属于 M2 的 stub `TargetRegistry` Protocol
- M1 `execution-target` spec 把 `TargetRegistry` 真正落地，其 API 是 `list() -> list[ExecutionTarget]`（**没有** `list_summaries()` 方法）
- M1 落地 PR 必须**同时**：
  1. 把 `list_targets_handler` 从 `ctx.target_registry.list_summaries()` 迁移到 `ctx.target_registry.list()`
  2. 在 handler 内执行 `ExecutionTarget → TargetSummary` 投影（应用本 spec §需求:`TargetSummary` 输出 schema 必须脱敏 的 scrub + 字段名 allowlist + capability allowlist 过滤）
  3. 投影来源：`name` ← `target.name`；`kind` ← `target.type`；`capabilities` ← `[c.value for c in target.capabilities if c.value in CAPABILITY_ALLOWLIST]`（按字典序）；`display_name` / `description` / `tags` / `enabled` ← 从对应 `TargetEntry`（`execution-target` spec 定义）派生（这些字段在 `ExecutionTarget` Protocol 上不存在 —— `TargetRegistry` 必须**同时**持有 `ExecutionTarget` 实例与对应 `TargetEntry` 元数据，handler 通过 registry 拿到两者）
- M1 落地 PR 必须删除 `list_summaries()` 的所有调用方与定义；任何依赖 stub 旧 API 的测试 fixture 必须同 PR 替换为真实 `TargetRegistry` + `LocalTarget` 装配

#### 场景:run_inspector ToolSpec 元数据

- **当** 装配后 `spec = registry.get("run_inspector")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "read"` / `spec.sensitive_output is True` / `spec.requires_approval is False` / `spec.timeout == 30.0`

#### 场景:list_inspectors ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_inspectors")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "none"` / `spec.sensitive_output is False` / `spec.timeout == 5.0`

#### 场景:list_targets ToolSpec 元数据

- **当** 装配后 `spec = registry.get("list_targets")`
- **那么** `spec.surfaces == {"agent"}` / `spec.side_effects == "none"` / `spec.sensitive_output is True` / `spec.timeout == 5.0`

#### 场景:list_targets handler 投影真实 TargetRegistry 数据且应用脱敏 + allowlist

- **当** 构造真实 `TargetRegistry` 实例含 2 个 target：(a) `LocalTarget("safe-local")` + `TargetEntry(name="safe-local", display_name="Local Dev", tags=["dev"], enabled=True)`；(b) `SSHTarget("prod-ssh")` + `TargetEntry(name="prod-ssh", display_name="login as admin@10.0.0.5", tags=["prod"], enabled=True, password="literal-pwd-do-not-leak-xyz123")`
- **当** 实例化 `ctx = ToolContext(target_registry=registry, ...)`，`await registry_tool.dispatch("list_targets", {}, ctx)`
- **那么** 返回的 `ListTargetsOutput.targets` 必须含 `safe-local`（带 `display_name="Local Dev"` / `tags=["dev"]` / `capabilities` 来自 `LocalTarget.capabilities` 与 `CAPABILITY_ALLOWLIST` 的交集，按字典序）
- **且** `prod-ssh` 必须被**整条 skip**（display_name 含 IPv4 + 凭据特征触发 scrub_inventory_string 规则）；structlog warning 记录 skip 原因码
- **且** `ListTargetsOutput.model_dump_json()` 必须**不**含 `"literal-pwd-do-not-leak-xyz123"` / `"10.0.0.5"` / `"admin"` 任意子串

#### 场景:TargetSummary metadata 字段必须来自 TargetEntry 而不是 ExecutionTarget Protocol

- **当** 测试代码定义一个 fake `ExecutionTarget` 实现（普通 class，非 Pydantic，方便动态属性）：
  ```python
  class _FakeTargetWithExtraAttr:
      name = "t1"
      type = "local"
      capabilities: set[Capability] = {Capability.SHELL}
      display_name = "FROM_TARGET_INSTANCE"  # 故意在 target 实例上加一个 Protocol 未声明的属性
      async def exec(self, cmd, *, timeout, env=None): ...
      async def read_file(self, path): ...
  ```
- **当** 注册 `_FakeTargetWithExtraAttr()` 实例 + `TargetEntry(name="t1", type="local", display_name="FROM_ENTRY", enabled=True)` 到 registry，调用 `list_targets` handler 处理 `t1`
- **那么** 返回的 `TargetSummary.display_name` 必须等于 `"FROM_ENTRY"`（来自 `TargetEntry`），**不**等于 `"FROM_TARGET_INSTANCE"`
- **理由**：`ExecutionTarget` Protocol 上**不**暴露 `display_name` / `description` / `tags` / `enabled` 字段；handler 必须通过 `TargetRegistry.get_entry(name)` 拿这些 metadata，避免误用 target 实例上偶然存在的同名属性；用 fake target 而非 `LocalTarget` 是因为后者可能是 Pydantic / dataclass 不允许任意 setattr

### 需求:`TargetSummary` 输出 schema 必须脱敏（M2 + M7-safe）

`list_targets` 的 `output_schema = ListTargetsOutput`，其中 `ListTargetsOutput.targets: list[TargetSummary]`。`TargetSummary` 必须**恰好**包含以下字段（不多不少）：

- `name: str`
- `kind: Literal["local", "ssh", "docker", "k8s"]`（与 `docs/ARCHITECTURE.md` §5 ExecutionTarget Protocol 已锁定的 `type` 枚举一致；**禁止**使用 `"kubernetes"` 等异名）
- `display_name: str | None`
- `description: str | None`
- `capabilities: list[str]`
- `tags: list[str]`
- `enabled: bool`

**字段名禁止集**：以下字段名**禁止**出现在 `TargetSummary.model_fields`：

`password` / `token` / `private_key` / `ssh_key_path` / `key_path` / `connection_string` / `dsn` / `url` / `host` / `hostname` / `ip_address` / `port` / `username` / `env` / `secret_ref` / `raw_config`

**字段值脱敏约束（对所有 string 类型字段：`name` / `display_name` / `description` / `capabilities[*]` / `tags[*]`）**：

`list_targets_handler` 在构造 `TargetSummary` 时**必须**对所有 string 类型字段值应用 `hostlens.tools.schemas.list_targets.scrub_inventory_string` 函数；scrub 必须按以下正则模式拒绝或脱敏：

- 路径子串：`/Users/[^/\s]+` / `/home/[^/\s]+` / `\.ssh(/|$)` / `\.aws/credentials` / `\.kube/config` —— 命中则**整个 target 被 skip**（不是脱敏后保留，避免给攻击者半张信息地图）
- IPv4 / IPv6 字面量：`\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}` / IPv6 简化模式 —— 命中则整个 target 被 skip
- 凭据特征：`[A-Za-z]+_(KEY|TOKEN|SECRET|PASSWORD)=[^\s]+` / `[Bb]earer\s+[\w.-]+` / `sk-[a-zA-Z0-9]{20,}` —— 命中则整个 target 被 skip
- 形如 `(?:user|username|usr)\s+\S+`（"user / username / usr" 关键词后紧跟一个标识符 token，按词边界 `\b` 判断独立成词）—— 命中则**仅替换紧跟的标识符 token 为 `"***"`**，保留前缀关键词与剩余上下文；target **不** skip；运维复合词如 `"user-service"` 不触发

被 skip 的 target 必须记录 structlog warning 含被 skip 的原因码（不含敏感字段值本身）。

**capability allowlist（M1 落地后必须与 `Capability` Enum 严格相等）**：

`hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST` 必须定义为 `frozenset({c.value for c in Capability})`（**M1 落地后**——`Capability` Enum 由 `execution-target` spec 定义并 import）。**禁止**：

- 静态硬编码字面量（如 `frozenset({"shell", "file_read", ...})`）—— 易与 Enum 漂移
- 含 Enum 尚未定义的 placeholder 值（如 M2 stub 阶段的 `file_write` / `docker` / `k8s_exec` 是预留 placeholder；M1 落地后**必须**删除，到 M8/M9 才回填）

#### 场景:TargetSummary 字段集恰好

- **当** 检查 `TargetSummary.model_fields` 的 key 集合
- **那么** 必须**恰好**返回 `{"name", "kind", "display_name", "description", "capabilities", "tags", "enabled"}`（不多不少）

#### 场景:list_targets 保留安全规划字段

- **当** Agent 调用 `list_targets`
- **且** 已配置 target `name="prod-web"`、`kind="ssh"`、`capabilities=["shell", "file_read"]`、`tags=["web", "prod"]`、`enabled=True`
- **那么** 返回的 `TargetSummary` 必须含 `name="prod-web"` / `kind="ssh"` / `capabilities=["shell", "file_read"]` / `tags=["web", "prod"]` / `enabled=True`
- **且** 返回值可被 Planner 用于选择后续 `run_inspector` 的 target

#### 场景:list_targets 不泄露 ssh_key 路径

- **当** Agent 调用 `list_targets` 且某 target 配置了 `key_path="/Users/alice/.ssh/id_rsa"`、`host="10.0.0.5"`、`username="admin"`、`password="secret123"`
- **那么** 返回的 `TargetSummary` 必须**不**含 `key_path` / `host` / `username` / `password` 字段
- **且** `ListTargetsOutput.model_dump_json()` 返回的 string 中**禁止**含 `/Users/`、`/home/`、`.ssh`、`id_rsa`、`10.0.0.5`、`admin`、`secret123` 任意子串
- **且** 原始 target 配置中的 credential / secret reference / connection string 不得出现在 `ListTargetsOutput.model_dump()` 的任何位置

#### 场景:name / display_name / description 含敏感子串时整 target 被 skip

- **当** 某 target 配置 `name="prod-web"` 但 `display_name="login as admin@10.0.0.5"`（display_name 字段值含 IPv4 + 凭据特征）
- **那么** 该 target 必须从 `ListTargetsOutput.targets` 中**整条 skip**（不是仅 display_name 脱敏后保留），structlog warning 记录 skip 原因码 `"sensitive_substring_in_display_name"`（**不**含原始字段值）
- **且** `ListTargetsOutput.model_dump_json()` 中**禁止**含 `10.0.0.5` 子串

#### 场景:tags 含 IPv4 子串时整 target 被 skip

- **当** 某 target 配置 `tags=["prod", "db", "192.168.1.42"]`
- **那么** 该 target 必须从输出中整条 skip；structlog warning 含 skip 原因码 `"sensitive_substring_in_tags"`

#### 场景:description 含 username 关键词时邻接标识符被局部替换

- **当** 某 target 配置 `description="Owned by user alice, contact via slack"`（"user" 关键词后紧跟独立标识符 token "alice"；description 不含路径 / IP / 凭据）
- **那么** target **不**被 skip；`description` 字段值经过 scrub 后输出为 `"Owned by user ***, contact via slack"`（**仅替换紧跟 "user" 的标识符 token**，保留前缀关键词与剩余上下文）；输出字段值**不含** "alice" 子串；其他字段保留原值

#### 场景:运维 tag 常用词不被误伤

- **当** 某 target 配置 `tags=["user-service", "auth-microservice"]`（"user" 是复合词的一部分，非独立 token）
- **那么** target **不**被 skip；`tags` 字段值保留原样（scrub 必须按词边界 `\b` 判断"独立 token"，不误伤复合词）

#### 场景:CAPABILITY_ALLOWLIST 派生自 Capability Enum

- **当** M1 落地后检查 `hostlens.tools.schemas.list_targets.CAPABILITY_ALLOWLIST`
- **那么** 必须严格相等于 `frozenset({c.value for c in hostlens.targets.base.Capability})`（M1 阶段 = `{"shell", "file_read", "ssh", "systemd", "docker_cli"}`）
- **且** 源码层面必须看到 `CAPABILITY_ALLOWLIST = frozenset({c.value for c in Capability})` 形式的派生表达式（**禁止**硬编码字面量集合，避免与 Enum 漂移）

#### 场景:list_targets 投影过滤 allowlist 外 token

- **当** 某 target 内部 `capabilities = {Capability.SHELL, Capability.SSH}` 加上一个未来值（mock 注入的 `"internal_admin_root"` 假 token）
- **那么** `TargetSummary.capabilities` 输出**只**含 `["shell", "ssh"]`（按字典序）；`"internal_admin_root"` 被静默剔除；handler 必须产生 structlog warning 记录被剔除的 token
