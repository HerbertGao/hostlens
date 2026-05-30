## 上下文

三块下游已稳定：

- **`AgentLoop`**（`agent/loop.py`）：`AgentLoop(backend, tool_adapter, settings, *, system=None)`；`run(intent) -> LoopResult`。**无 logger、无事件**：`run()` 内 `while` 按 6 个 stop_reason 推进，`_run_tool_turn` 用 `asyncio.gather` 并行 dispatch 每个 `tool_use` 块，`_dispatch_one` 产出 `(tool_result_block, ToolInvocation)`，`_finalize` 默认 `final_text=""` 收尾。
- **`PlannerAgent`**（`agent/planner.py`）：`run(intent) -> PlannerResult`，仅 `await self._loop.run(intent)` 再收敛。
- **`hostlens inspect`**（`cli/inspect.py`）：M1 单 Inspector 命令，`--inspector` 必填、`--format md|json`、`--output`、4 值退出码（3>2>1>0）、严格 stdout/stderr 分离、CLI 边界 `except Exception` 包成 `internal: <kind>: <msg>` 不泄露 traceback、`_with_restored_structlog_config` 保护全局 structlog 配置。

缺口：没有 CLI 触发 `PlannerAgent`；且要满足「实时显示 Agent 思考与工具调用」必须给黑盒 loop 加观测面。本设计（经与 Codex 对抗性讨论确定）引入**最小类型化 `LoopEvent` observer 抽象**。

约束：CLAUDE.md §4.1（手写 loop 可读，遥测不污染核心控制流）/ §4.2（Planner 调度者）/ §2 §4（可观测早晚要做，但本提案只立 UI 级事件，不内联日志）/ §8（架构清晰度 > 功能广度）；ADR-005（重试单一收口 loop）/ ADR-008（backend 私有）。

## 目标 / 非目标

**目标：**
- 给 `AgentLoop.run` / `PlannerAgent.run` 加**可选** observer（默认 None=no-op，向后兼容）。
- 定义稳定、类型化的 `LoopEvent` 集合与 `LoopObserver` Protocol，覆盖 turn/模型响应/工具开始/工具完成/收尾。
- 新增 `hostlens inspect --intent`：与 `--inspector` 互斥、装配并运行 PlannerAgent、Rich Live 实时进度(→stderr)、输出 narrative+findings+遥测(→stdout)、退出码映射、不泄露 traceback。
- 单测(FakeBackend)+CLI 回放(PlaybackBackend)在无 SSH/无付费 API 下跑通。

**非目标：**
- 不做 token 流式、不做完整 structlog/OTel observability（独立后续提案）、不组装 Report、不改 LoopResult/PlannerResult schema、不改重试/预算/stop_reason 逻辑、不把 run 改成 async generator。

## 决策

### D-1：observer 用「单方法 `on_event(event)` + 类型化事件 union」，不是多方法回调

**选择**：
```python
# agent/events.py
@dataclass(frozen=True)
class TurnStarted:    turn: int                      # 1-based, 即将发起的这一轮
@dataclass(frozen=True)
class ModelResponded: turn: int; stop_reason: str; text: str   # 本轮 assistant 文本(可"")
@dataclass(frozen=True)
class ToolStarted:    turn: int; tool_name: str; tool_use_id: str; tool_input: dict[str, Any]
@dataclass(frozen=True)
class ToolCompleted:  turn: int; invocation: ToolInvocation     # 已记录的 output|error
@dataclass(frozen=True)
class RunFinalized:   terminal_status: str; turns: int

LoopEvent = TurnStarted | ModelResponded | ToolStarted | ToolCompleted | RunFinalized

class LoopObserver(Protocol):
    def on_event(self, event: LoopEvent) -> None: ...
```
loop / planner 的 `run` 签名加 `*, observer: LoopObserver | None = None`。

**理由**：
- 单 `on_event` + union 比 N 个具名方法更易扩展（加事件不破坏既有 observer）、易 type-discriminate（`match event:`）、Protocol 面最小。
- 事件 frozen dataclass（非 Pydantic）：纯进程内传值、无需校验/序列化，零依赖、最轻。
- `ToolCompleted` 直接带 `ToolInvocation`（loop 已有的记录），observer 想看 output/error 自取，不重复造结构。

**替代**：(A) N 个具名方法（`on_turn_start` 等）—— 否决：加事件即破坏 observer 实现，Protocol 面臃肿。(B) Pydantic 事件 —— 否决：进程内 UI 事件无需校验/序列化开销。

### D-2：observer 同步、emit 点 None-guard、loop fail-loud（不防御性包裹）

**选择**：`on_event` 同步（`-> None`）。loop 在 emit 点 `if observer is not None: observer.on_event(event)` —— **直接调用，不加 try/except**。observer 的契约是「`on_event` 不得抛出」；隔离责任在 observer 自身（见 D-7：`RichLiveObserver` 内部自吞渲染错误）。

**理由**：
- **fail-loud，不写防御性 fallback**（CLAUDE.md 红线 / §6「错误只在边界处理」）：在核心 loop 里 try/except 吞掉调用方回调异常，是针对「外部代码 bug」的防御性兜底，与 loop 既有 fail-loud 契约（`ToolError`/`ToolPolicyViolation`/取消都上抛）不一致。observer 是第一方代码（CLI/测试），契约「不抛」可被强制；真有 bug 让它 fail-loud 暴露，比静默吞掉更符合项目价值观。
- asyncio 单线程内，emit 在协程同步段执行、彼此不交错（即便并行 tool dispatch，`on_event` 调用各自原子），故同步回调天然并发安全；省去 `await observer.on_event` 在热路径铺开。
- 约束 observer 非阻塞（OPERATIONAL）：Rich `Live.update` 廉价，满足。

**替代**：
- (A) loop `_safe_emit` 吞异常 —— **否决**（第 1 轮 Codex review 指出违反「不写防御性 fallback」红线；隔离下沉到 observer 更干净）。
- (B) async `on_event` —— 否决：tool dispatch 在 `gather` 内，await UI 会把渲染塞进并行关键路径，且多数 observer（Rich）本就同步。

### D-3：emit 点（loop.py 改动，全部 additive、None 时 no-op）

| 事件 | 位置 | 数据 |
|---|---|---|
| `TurnStarted(turn=turns+1)` | `while` 体内、预算/turn 守卫通过后、`_call_with_retry` 前 | 即将发起轮号 |
| `ModelResponded(turn, stop_reason, text)` | 拿到 response、`turns+=1`、累计 usage 后 | `_join_text(response)`（tool_use 轮常为 ""，仅展示用） |
| `ToolStarted(...)` | `_dispatch_one` **入口、任何分支判断之前**（含幻觉名拦截、policy、malformed args 之前） | block 的 name/id/input |
| `ToolCompleted(turn, invocation)` | `_dispatch_one` 每条 return 路径前（**所有**分支：成功 / 幻觉名 / error envelope 都产出一条 invocation） | 该 invocation |
| `RunFinalized(terminal_status, turns)` | `_finalize` 内、构造 LoopResult 前 | 终态 |

**ToolStarted 对每个块都发；ToolCompleted 只对产出 `ToolInvocation` 的块发**（修正第 1 轮 Codex review：原把 ToolStarted 放在幻觉拦截之后会让幻觉工具从事件流消失）。`_dispatch_one` 有 4 条 **return** 路径（幻觉名 / TypeError / error envelope / 成功），每条产出一条 `ToolInvocation`；ToolStarted 在入口发一次、ToolCompleted 在每条 return 前发一次，与 `LoopResult.tool_invocations` 一一对应。

**fail-loud 路径不发 ToolCompleted**（第 2 轮 Codex review 修正）：`ToolPolicyViolation` / output-contract `ToolError` / 已注册 handler 内部 `KeyError` / `CancelledError` 在 `_dispatch_one` **不被捕获、直接上抛**（既有 fail-loud 路由，本提案 additive 不改），**不**产出 `ToolInvocation`。这些块已发的 `ToolStarted` 没有配对 `ToolCompleted`，且异常传出 `run`（不发 `RunFinalized`、不返回 `LoopResult`）。observer 契约：**不得假设每个 `ToolStarted` 必有 `ToolCompleted`**。

**事件顺序是偏序非全序**：单轮并行 `gather` dispatch 多个块时，各块 `ToolStarted`/`ToolCompleted` 会交错（asyncio 在 `await dispatch` 处切换）。loop 仅保证 turn 级顺序 + 同块 Started→Completed + 可按 `turn`/`tool_use_id` 关联。spec 与测试不得假设多工具全序。

observer 经 `run` 局部变量下传：`run` 自己 emit TurnStarted/ModelResponded/RunFinalized；`_run_tool_turn(response, advertised_names, observer, turn)` → `_dispatch_one(block, names, observer, turn)` emit ToolStarted/ToolCompleted。observer **不存 self**（它是 per-run 的，run 是可重入的）。`_finalize` 加 observer 形参（run 调用处传入）以 emit RunFinalized。

**关键**：所有 emit 用 `if observer is not None:` 包裹 → `observer=None`（含全部既有调用方/测试）走原路径，行为零变化。这是 agent-loop/planner-agent 用 ADDED（新增正交行为）的依据；而 `inspect-cli-command` 因 `--inspector` 必填→可选改变了既有已发布行为，用 MODIFIED 复述完整需求。

### D-4：CLI `--intent` 集成进 `inspect` 命令，互斥校验

**选择**：`--inspector` 默认改 `None`（不再 `...` 必填）；新增 `--intent: str | None = None`。命令体最前做互斥校验：
- both None → `usage error`（exit 3）「必须提供 --inspector 或 --intent 之一」
- both set → exit 3「--inspector 与 --intent 互斥」
- 仅 `--inspector` → 走现有 M1 路径（完全不变）
- 仅 `--intent` → 走新 intent 路径

intent 路径助手放 `cli/_intent.py`（装配 + RichLiveObserver + 渲染），inspect_cmd 仅做分流，保持单文件不臃肿。

**理由**：TODO 明确 `hostlens inspect <target> --intent`；复用同命令避免 `inspect`/`diagnose` 概念分裂。`--inspector` 由必填改可选是向后兼容（原调用仍有效）。

**替代**：新子命令 `hostlens diagnose` —— 否决：偏离 TODO 字面，且 inspect 已是「对 target 出报告」的语义位。

### D-5：intent 路径装配 PlannerAgent

```python
backend = create_backend(settings)                      # anthropic_api / playback
registry = ToolRegistry(); register_default_tools(registry)
target_registry = _load_target_registry()               # 复用 inspect 现有助手
inspector_registry = build_registry_from_search_paths(...)
def context_factory() -> ToolContext:                    # 每次 dispatch 新建(fresh cancel)
    return ToolContext(target_registry, inspector_registry, settings,
                       logger, NoopApprovalService(), asyncio.Event())
planner = PlannerAgent(backend, registry, settings, context_factory)
observer = RichLiveObserver(console=Console(stderr=True))
result = asyncio.run(planner.run(intent, observer=observer))
```
backend 仅传 PlannerAgent→AgentLoop，**不进** context_factory 产出的 ToolContext（ADR-008，PlannerAgent 已保证）。

### D-6：intent 输出与退出码

**输出**（沿用 `--format`/`--output`）：
- md：`narrative`（已是 markdown）+ 一个 findings 摘要表（severity / message / tags）+ 一行遥测（turns / terminal_status / input+output tokens）。findings 空时仅 narrative + 遥测。
- json：`PlannerResult.model_dump_json()`（含 narrative/findings/loop_result/intent，下游可解析）。
- 报告 → stdout（或 `--output` 文件）；Rich Live 实时进度 → stderr。

**退出码**（沿用 inspect 4 值 3>2>1>0）：
- `0` healthy：terminal_status=`ok` 且无 critical finding
- `1` business critical：terminal_status=`ok` 且 ≥1 `severity=="critical"` finding
- `2` 降级/失败：terminal_status ∈ {degraded_*, failed_api_unavailable, empty_response}
- `3` usage/config：参数互斥违规、backend 未配置（ConfigError）、`--output` 写失败、`--format` 非法

**理由**：与 `--inspector` 路径退出码语义一致，脚本/CI 可统一判读；critical-finding 判定复用同一逻辑（作用于 `PlannerResult.findings`）。

### D-7：RichLiveObserver

`agent/` 不依赖 Rich（保持 Agent 层纯净）；`RichLiveObserver` 放 **`cli/_intent.py`**（CLI 层才依赖 Rich）。它 `match event` 维护一个 Rich `Tree`/`Table`（轮次 → 工具调用 → ok/err），`Live`(stderr) 增量刷新；`RunFinalized` 时停 Live。实现 `LoopObserver` Protocol（结构化鸭子类型，无需继承）。

**`on_event` 内部自吞渲染错误**（D-2 把隔离责任下放到 observer）：`RichLiveObserver.on_event` 整体包一层 `try/except Exception`，渲染失败时降级为静默/纯文本一行，**绝不向 loop 抛**——保证一次 Rich 渲染 glitch 不会 fail-loud 掉整个巡检。这是 observer 自身的边界处理，不是 loop 的防御性 fallback。测试用纯 Python `RecordingObserver`（append 事件到 list、不抛）断言序列，不依赖 Rich 渲染。

## 风险 / 权衡

- **[改已归档 agent-loop spec]** → 接受：经新 OpenSpec change 以 ADDED Requirements 记录，纯 additive、observer=None 行为不变、既有测试不动。纯 CLI 方案做不到真实时（tool_invocations 仅 run 后存在）。
- **[loop fail-loud：buggy observer 会掀翻巡检]** → 接受并缓解：observer 是第一方（CLI/测试），契约「on_event 不抛」；`RichLiveObserver` 内部自吞渲染错误降级为纯文本，所以正常用户路径下 UI glitch 不会 fail-loud。代价是若有人塞一个会抛的自定义 observer，巡检会崩——这与「不写防御性 fallback、第三方契约违约就 fail-loud 暴露」一致，是有意取舍（第 1 轮 Codex review 据此否决了 loop 端 try/except）。
- **[emit 点散布 loop 多处影响可读性]** → 缓解：用单行 `if observer is not None: observer.on_event(Event(...))`，事件名自解释；core 控制流仍只表达领域转换，无 try/except 噪声。
- **[--timeout 与 --intent 组合语义]** → 决定：`--timeout` 仅对 `--inspector` 路径有意义（它改 manifest collect 超时）；intent 路径忽略并在 stderr 提示一行（不报错，避免脆性），Agent 的工具超时由 ToolSpec 固定。
- **[Rich Live 在非 TTY / 管道 stderr 下乱码]** → 缓解：Rich 检测非 TTY 自动降级为逐行输出；测试在 CliRunner（非 TTY）下断言关键文本出现即可，不断言动画帧。

## Migration Plan

向后兼容，无破坏。`AgentLoop.run`/`PlannerAgent.run` 加可选 kwarg（默认 None）；`hostlens inspect --inspector` 原调用不变（仅由必填变可选 + 新增互斥 --intent）。新增 `agent/events.py`、`cli/_intent.py`。回滚 = 移除 observer 参数与 intent 分支、删新增文件，下游无强依赖（observer 默认 None）。

## Open Questions

- **`ModelResponded.text` 是否含 thinking？** M2 无 extended_thinking（capability False），text 即 assistant 可见文本；未来接 thinking 时再加 `ThinkingDelta` 事件，本提案不预留。
- **RichLiveObserver 是否显示 token usage 进度条？** 倾向 M2.7 只显示轮次/工具树 + 末尾总 usage，进度条留到有 streaming 时；本提案不做。
- **取消（Ctrl-C）是否经 observer 发 `RunCancelled` 事件？** 倾向不加 —— 取消由 CLI 现有 KeyboardInterrupt 处理收口，loop 层取消语义接入是 M2.7 之外；本提案 observer 不含取消事件。
