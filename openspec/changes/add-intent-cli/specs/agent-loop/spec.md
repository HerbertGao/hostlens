## ADDED Requirements

### 需求:`AgentLoop.run` 可选 observer 接收类型化 LoopEvent

`AgentLoop.run` 必须接受一个可选关键字参数 `observer`（`LoopObserver | None`，默认 `None`）。当 `observer` 为 `None` 时，loop 的行为必须与未引入 observer 前完全一致 —— 不发任何事件、不改变控制流、不影响 `LoopResult`、既有测试不变。

当 `observer` 非 `None` 时，loop 必须在以下边界发出类型化 `LoopEvent` 并调用 `observer.on_event(event)`：
- 每轮发起模型调用前发 `TurnStarted`；
- 收到模型响应并完成本轮 usage 累计后发 `ModelResponded`（含 `stop_reason` 与本轮 assistant 文本 `text`）；
- **每个** `tool_use` 块在进入 `_dispatch_one` 时（任何分支判断之前）发 `ToolStarted`；对**产出 `ToolInvocation` 的块**——成功、幻觉工具名、malformed args（`TypeError`）、handler 异常 error envelope——在得到该 `ToolInvocation` 后发 `ToolCompleted`（与 `LoopResult.tool_invocations` 一一对应）；
- 终态收尾时发 `RunFinalized`（含 `terminal_status` 与 `turns`）。

**fail-loud 路径不发 `ToolCompleted`**（保持 loop 既有错误路由不变、本提案 additive 不改）：`ToolPolicyViolation`、output-contract `ToolError`、已注册 handler 内部 `KeyError`、`CancelledError` 等在 `_dispatch_one` **不**产出 `ToolInvocation`、直接上抛中断该 turn（取消 sibling）。这些块可能已发过 `ToolStarted`，但**不会**有配对的 `ToolCompleted`，且整个 `run` 抛出异常、**不**发 `RunFinalized`、**不**返回 `LoopResult`。observer 不得假设每个 `ToolStarted` 必有 `ToolCompleted`。

**`on_event` 契约**：observer 实现的 `on_event` 必须同步、非阻塞、且**不得抛出异常**。loop 直接调用 `observer.on_event(event)`，**不**对其做防御性 try/except 包裹（遵循「错误只在边界处理、不写防御性 fallback」红线）—— observer 自身负责吞掉并隔离其内部（如渲染）错误。`LoopEvent` 必须是不可变值对象（frozen dataclass），`LoopObserver` 必须是结构化 `@runtime_checkable` Protocol（实现方无需继承）。

**`ModelResponded.text`**：仅供展示用，取自该轮响应的文本块拼接（`_join_text`）；在 `tool_use` 轮次中模型常只发 tool_use 块而无文本，故 `text` 允许为空字符串。observer 不得假设它非空，也不得依赖它表达「thinking」（M2 无 extended_thinking / streaming）。

**事件顺序保证（偏序，非全序）**：单轮内若并行 dispatch 多个 `tool_use` 块（loop 用 `asyncio.gather`），各块的 `ToolStarted` / `ToolCompleted` **可能交错**，loop 仅保证：(a) turn 级顺序正确（`TurnStarted` 先于该轮的工具事件、晚于工具事件的下一轮 `TurnStarted`）；(b) 同一块的 `ToolStarted` 先于其 `ToolCompleted`；(c) 可通过事件携带的 `turn` 与 `tool_use_id` 关联归属。observer / 测试不得假设多工具的事件全序。

#### 场景:observer=None 时行为不变
- **当** 以默认 `observer=None` 调用 `AgentLoop.run(intent)`
- **那么** 不得发出任何事件，`LoopResult` 与控制流必须与引入 observer 前一致

#### 场景:单工具多轮发出有序事件序列
- **当** 传入一个记录事件的 observer，Agent 经「单个 tool_use 工具的 tool_use 轮 → end_turn 轮」完成
- **那么** observer 必须按序收到 `TurnStarted` → `ModelResponded` → `ToolStarted` → `ToolCompleted` → `TurnStarted` → `ModelResponded` → `RunFinalized`，且 `RunFinalized.terminal_status` 与返回的 `LoopResult.terminal_status` 一致

#### 场景:同轮多工具并行事件按偏序与关联校验
- **当** 某轮并行 dispatch 两个 `tool_use` 块
- **那么** 每个块必须各发一对 `ToolStarted`/`ToolCompleted`（同块 Started 先于 Completed），两块之间的事件允许交错；测试必须按 `tool_use_id` 关联而非假设全序

#### 场景:工具完成事件携带对应 invocation
- **当** 某轮 dispatch 一个 `run_inspector` 工具
- **那么** 该次 `ToolCompleted.invocation` 必须等于最终出现在 `LoopResult.tool_invocations` 中的同一条记录（成功填 output / 失败填 error）

#### 场景:幻觉工具名也配对发出工具事件
- **当** 某轮的 `tool_use` 块是一个未注册（幻觉）工具名，loop 不调用 handler 直接产出 error invocation
- **那么** observer 仍必须收到该块的 `ToolStarted` 与 `ToolCompleted`（`ToolCompleted.invocation.error` 非空），事件流不遗漏该工具边界

#### 场景:fail-loud 工具路径不发 ToolCompleted
- **当** 某轮的 `tool_use` 块触发 fail-loud 路径（如 dispatch 抛 `ToolPolicyViolation`，不产出 `ToolInvocation`）
- **那么** 该块可能已发 `ToolStarted`，但**不得**有配对的 `ToolCompleted`；异常上抛中断该 turn，`run` 抛出异常、**不**发 `RunFinalized`、**不**返回 `LoopResult`（loop 既有 fail-loud 路由不被 observer 改变）

#### 场景:observer 不被 loop 防御性包裹
- **当** observer 的 `on_event` 抛出异常
- **那么** loop **不**捕获该异常（无防御性 try/except）—— 异常按正常 Python 语义传播；observer 实现有责任保证 `on_event` 不抛（如 CLI observer 内部自吞渲染错误）
