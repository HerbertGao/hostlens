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
