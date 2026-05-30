## 为什么

M2.4 已交付 `PlannerAgent`（自然语言 intent → 自选 Inspector → 收敛成 `PlannerResult`），但**没有任何 CLI 入口能触发它** —— 现有 `hostlens inspect` 仍是 M1 的单 Inspector 命令（`--inspector` 必填）。Hostlens 的简历价值「用户说人话 → Agent 自己规划巡检 → 出报告」目前只能在测试里跑，演示不出来。

同时，TODO 2.7 要求「**实时流式输出 Agent 思考与工具调用（Rich live display）**」。但 `AgentLoop.run()` 当前是黑盒：无 logger、无事件、跑完才一次性返回 `LoopResult`（M2 backend `streaming=False`，无 token 流式）。要让用户在 Agent 跑的过程中看到它在调哪个工具、拿到什么结果，必须给 loop 加一个**最小的、可选的观测面** —— 否则只能退化成 spinner + 事后清单，对不上「实时」。**澄清「思考」的范围**：M2 无 token 流式、无 extended_thinking，所以本提案的「实时」是 **turn/工具级**（逐轮显示调了哪个工具、拿到什么结果，以及每轮 `messages_create` 返回后的 assistant 文本——`ModelResponded.text`，仅展示用、在 tool_use 轮常为空），**不是**逐 token 流出模型的内心独白。真·token/thinking 流式留待后续 backend 能力。

现在做：PlannerAgent 刚稳定，CLI 是它唯一缺的下游消费方；observer 抽象一旦立住，未来 structlog/OTel 全链路可观测（§2/§4）能直接建在同一事件流上，不必再动 loop。

## 变更内容

1. **`AgentLoop` 增加可选 observer（additive，向后兼容）**：`AgentLoop.run(intent, *, observer=None)`。在 turn 边界与工具 dispatch 边界发出**类型化 `LoopEvent`**（`TurnStarted` / `ModelResponded` / `ToolStarted` / `ToolCompleted` / `RunFinalized`）。`observer=None`（默认）= 完全 no-op，现有行为与全部已归档测试不变。observer 是外挂，循环核心控制流不被遥测污染（CLAUDE.md §4.1 手写 loop 可读）。

2. **`PlannerAgent.run` 透传 observer**：`PlannerAgent.run(intent, *, observer=None)` 把 observer 原样交给内部 `AgentLoop.run`，不解释不拦截。

3. **新增 `hostlens inspect <target> --intent "<自然语言>"`**：
   - `--intent` 与 `--inspector` **互斥**；恰好提供其一，缺失或同时提供都是 usage error（exit 3）。
   - `--intent` 路径装配 `PlannerAgent`（`create_backend(settings)` + `register_default_tools(ToolRegistry())` + context_factory 产出含 target/inspector registry 的 `ToolContext`），挂一个 CLI 端 `RichLiveObserver` 把 `LoopEvent` 渲染成 Rich Live 实时进度（→ **stderr**），跑完把最终结果（→ **stdout**）渲染输出。
   - 输出：md 模式 = `narrative`（LLM 综述 markdown）+ findings 摘要表 + loop 遥测（turns / terminal_status / token usage）；json 模式 = `PlannerResult` 的 `model_dump_json`。**不组装 `Report`**（M2.4 的非目标延续，Report 是 M3）。
   - 退出码沿用 inspect 的 4 值语义（见下）。

4. **新增 `LoopObserver` Protocol + `LoopEvent` 类型**（`agent/` 内）：供 loop 发事件、CLI/测试/未来 observability 实现。

**不**改 `LoopResult` schema、不改 PlannerResult、不改 stop_reason 推进/重试/预算逻辑、不引入 token 流式。

## 功能 (Capabilities)

### 新增功能
（无新 capability —— `--intent` 是既有 `inspect-cli-command` capability 的扩展，见下「修改功能」。该 spec 的目的段本就预告「M2 Planner Agent 提案将扩展为 `--intent` 自然语言入口」。）

### 修改功能
- `inspect-cli-command`: **MODIFIED** —— `--inspector` 由必填改为可选、新增 `--intent` 选项、二者互斥、「缺 --inspector 报错」场景改为「缺二者之一报错」+「互斥」场景（这些改变既有已发布行为，故用 MODIFIED 复述完整需求）；**ADDED** —— `--intent` 路径的 PlannerAgent 装配/observer/stderr-stdout 分离、md/json 输出、由 PlannerResult 映射的退出码（新增正交行为）。
- `agent-loop`: **ADDED** —— `run(intent, *, observer=None)` 在 turn/工具边界发出类型化 `LoopEvent`；`observer=None` 时完全 no-op，既有需求行为不变（纯增量、向后兼容）。
- `planner-agent`: **ADDED** —— `PlannerAgent.run(intent, *, observer=None)` 透传 observer 到 `AgentLoop.run`（纯增量）。

## 影响

- **新增代码**：`src/hostlens/agent/events.py`（`LoopEvent` 类型 + `LoopObserver` Protocol）、`src/hostlens/cli/_intent.py`（PlannerAgent 装配 + `RichLiveObserver` + 输出渲染助手；或内联进 `cli/inspect.py`，由 design 定）。
- **修改代码**：`src/hostlens/agent/loop.py`（run 加 observer 参数 + 5 处 emit）、`src/hostlens/agent/planner.py`（run 加 observer 透传）、`src/hostlens/cli/inspect.py`（`--inspector` 改可选 + `--intent` 选项 + 互斥校验 + 分流到 intent 路径）。
- **新增测试**：`tests/agent/test_loop_observer.py`（事件序列断言 + 默认 None 无回归）、`tests/agent/test_planner.py` 增 observer 透传用例、`tests/cli/test_inspect_intent.py`（互斥校验/退出码/stdout-stderr 分离/PlaybackBackend 回放出报告）。
- **对外契约影响**：
  - **CLI 命令**：`hostlens inspect` 新增 `--intent`，`--inspector` 由必填改为可选。原 `inspect <t> --inspector <i>` 调用**行为不变**；唯一改变的既有行为是「裸 `inspect <t>` 缺 --inspector」的报错——从 Typer `Missing option '--inspector'` 变为命令体互斥校验「必须提供 --inspector 或 --intent 之一」（仍 exit 3）。此行为变更经 `inspect-cli-command` spec MODIFIED 显式记录。
  - **Agent loop 行为**：`AgentLoop.run` / `PlannerAgent.run` 新增可选 kwarg（additive）；`LoopEvent` / `LoopObserver` 是新公开类型。
  - **Inspector schema / Notifier / MCP / Schedule / Report schema**：均无变更。
- **依赖**：不引入新第三方依赖（Rich 已是 CLI 依赖；Anthropic SDK 经 backend）。

## 非目标 (Non-Goals)

- **不做 token 级流式**：M2 backend `streaming=False`，`messages_create` 不支持逐 token；实时进度是 **turn/工具级**（每轮 `messages_create` 返回后的摘要 + 工具调用/结果），不是逐字。token 流式留待未来 backend streaming 能力落地。
- **不做完整 structlog/OTel 可观测**：M2.7 只立 UI 级稳定类型化 `LoopEvent` 抽象；把事件桥接到 structlog 事件 / OTel span（`StructlogObserver` 等）是独立后续提案，建在本提案的事件流之上 —— 不在 loop 内联日志（保持核心控制流干净）。
- **不组装结构化 `Report`**：intent 输出 = narrative + findings 摘要 + 遥测，沿用 M2.4 决策，完整 Report 组装 + 关联是 M3。
- **不做 Diagnostician / 根因关联**（M3）、**不做调度触发**（M4）、**不做 MCP 投影**（M7）。
- **不把 observer 做成 async generator**：generator 无法既 yield 事件又干净 `return LoopResult`，会破坏 `PlannerAgent.run` 的 `await loop.run()` 与 `PlannerResult` 返回契约；用 observer 回调。
- **不改 `LoopResult` / `PlannerResult` schema**、不改重试/预算/stop_reason 逻辑。

## Failure Modes

1. **backend 未配置 / `ANTHROPIC_API_KEY` 缺失**：`--intent` 需要真实 backend。`create_backend(settings)` 抛 `ConfigError` → CLI 映射 exit 3（配置/usage 错误类）+ 一行 stderr 提示（指向 `hostlens doctor`），不泄露 traceback。
2. **Agent 跑成降级/失败**（`degraded_max_turns` / `degraded_token_budget` / `failed_api_unavailable` / `degraded_rate_limited`）：CLI 不重试（loop 单一收口），透传 terminal_status → exit 2；仍把已收集的 `findings` + （可能为空的）`narrative` 输出，并在 stderr 标注降级原因。降级而非崩溃。
3. **observer 回调内部出错（如 Rich 渲染异常）**：loop **不**对 `on_event` 做防御性 try/except（遵循「错误只在边界处理、不写防御性 fallback」红线，保持 loop fail-loud）。observer 的契约是「`on_event` 不得抛出」——隔离责任在 observer 自身：CLI 的 `RichLiveObserver` 在其 `on_event` 内部自吞渲染错误并降级为纯文本/静默，使一次渲染故障不影响巡检；测试用的 `RecordingObserver` 只 append 不抛。observer 是第一方代码（CLI/测试），契约可被强制。
4. **Ctrl-C / 取消**：沿用 inspect 现有处理 —— 包成一行 `internal: cancelled: ...` → exit 2，不泄露 asyncio 取消栈；Rich Live 正常收尾不残留终端状态。
5. **PlaybackBackend cassette miss（replay demo）**：`CassetteMiss` 是非可重试 `BackendError`，loop 的 `_call_with_retry` **只**重试 `BackendRateLimited`/`BackendUnavailable`，其余 backend 错误**原样上抛**（不经 terminal_status 降级）。故 `--intent` 路径下 cassette miss 会传播到 CLI 边界，被 `except Exception` 包成一行 `internal: CassetteMiss: ...` → exit 2，不泄露 traceback、不消耗真实 API。**这是测试 fixture 失败，不是优雅降级**：采用 M2.4 已验证的 record-then-replay cassette 生成方式即可保证不 miss（多轮 messages 键天然匹配）。

## Operational Limits

- **并发预算**：observer 不引入新并发；Agent 并行 `tool_use` 仍受 AgentLoop + Inspector Runner 两级 `asyncio.Semaphore` 约束（OPERABILITY §1）。
- **observer 必须非阻塞**：`on_event` 在 event loop 线程同步执行（asyncio 单线程内 emit 不并发交错），实现里禁止阻塞 IO / 长耗时同步工作，否则拖慢 Agent 循环。Rich `Live.update` 是廉价同步操作，满足。
- **Token / turn 预算**：复用 `agent.token_budget_input`（默认 100K）/ `token_budget_output`（30K）/ `max_turns`（20），由 AgentLoop 强制，CLI 不放宽。
- **超时**：单次 `messages_create` 60s（loop 内）、单工具 30s（ToolSpec），均下游已定，`--intent` 不新增超时层。`--timeout` 选项仅对 `--inspector` 路径生效（intent 路径忽略并在 stderr 提示，或 design 决定拒绝组合）。

## Security & Secrets

- **不引入新密钥**：复用 `ANTHROPIC_API_KEY`（backend 持有）。
- **脱敏**：工具 error envelope 已在 `ToolsAdapter.dispatch` 过 `scrub_exception_message`；`LoopEvent.ToolCompleted` 携带的是 `ToolInvocation`（output/error 已是脱敏后的 dict），`RichLiveObserver` 渲染时不二次拼接异常原文。`run_inspector` 输出 `sensitive_output=True`（进程/端口元数据）—— 实时显示与最终报告都不写日志正文；渲染边界遵循既有 reporting redact 约定。
- **stdout/stderr 分离**：最终报告 → stdout（可管道/重定向）；Rich Live 实时进度 → stderr。保证脚本消费 stdout 时不被进度动画污染。
- **攻击面**：`--intent` 字符串仅作为 user message 传给模型，**不进入任何 shell/命令渲染路径**（命令只能由 Inspector manifest 经受控渲染产生）；不暴露 `exec_arbitrary_command`，能力面仍由 ToolRegistry `surfaces` + Inspector 限制。
- **读操作命令**：`--intent` 是只读巡检入口，容忍 `EUID==0`（与现有 inspect 一致）。

## Cost / Quota Impact

- **每次 `--intent` run**：1 次发现轮 + N 次 inspector 轮 + 1 次综述轮 ≈ 3–6 次 `messages_create`，上限由 `max_turns=20` 兜底；token 受预算硬约束（input 100K / output 30K）。
- **Prompt caching**：M2.4 已保证系统提示词以 list[text block] 跨 run 稳定、可被 loop 缓存；`--intent` 多轮内复用系统块降低重复 input token（命中率断言留 M2.5）。
- **测试 / demo 成本**：单测走 FakeBackend（零 API），CLI 测试与 demo 走 PlaybackBackend + cassette（零 API）；CI 默认 replay 不消耗 Anthropic 配额。`--intent` 真实调用仅在用户显式配 `anthropic_api` backend 时发生。

## Demo Path

无 SSH、无付费 API（cassette replay）：

```bash
pip install -e ".[dev]"
# 单测：observer 事件序列 + PlannerAgent 透传
pytest tests/agent/test_loop_observer.py tests/agent/test_planner.py -m "not live" -q
# CLI 端到端回放：PlaybackBackend + cassette 跑 --intent，验证实时进度(stderr) + 报告(stdout) + 退出码
pytest tests/cli/test_inspect_intent.py -k playback -q
```

期望：CLI 测试断言 stderr 出现 turn/工具实时行、stdout 出 narrative + findings 摘要、terminal_status=ok → exit 0、结果稳定（cassette 决定性回放）。配好真实 backend 后：`hostlens inspect <target> --intent "检查这台机器的健康状况"` 在终端看到 Agent 逐轮调 Inspector 的 Rich Live 进度，最后打印综述报告。
