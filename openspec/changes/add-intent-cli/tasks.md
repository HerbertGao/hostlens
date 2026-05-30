## 1. LoopEvent / LoopObserver 抽象

- [x] 1.1 新建 `src/hostlens/agent/events.py`：定义 5 个 frozen dataclass 事件（`TurnStarted` / `ModelResponded` / `ToolStarted` / `ToolCompleted` / `RunFinalized`，字段见 design D-1）+ `LoopEvent` union 类型别名 + `LoopObserver` Protocol（`on_event(event: LoopEvent) -> None`，`@runtime_checkable`）。`ToolCompleted.invocation: ToolInvocation`（从 loop import，注意避免循环 import —— 必要时 `TYPE_CHECKING` 或把 ToolInvocation 留在 loop、events 仅 `from __future__ import annotations` 引用）。验收：mypy --strict 通过；`isinstance(obj, LoopObserver)` 对实现了 on_event 的对象为真。

## 2. AgentLoop 发事件（additive，observer=None 时 no-op）

- [x] 2.1 `agent/loop.py`：`run(intent, *, observer: LoopObserver | None = None)`。emit 点一律 `if observer is not None: observer.on_event(event)` —— **直接调用、不加 try/except**（loop fail-loud，不写防御性 fallback；隔离责任在 observer 自身，见 design D-2）。observer 经 `run` 局部下传 `_run_tool_turn(..., observer, turn)` → `_dispatch_one(..., observer, turn)`；`_finalize` 加 observer 形参。observer 不存 self。验收：observer=None 时零行为变化（既有 test_loop.py 全绿）。
- [x] 2.2 在边界 emit（design D-3）：`TurnStarted(turns+1)`（守卫通过后、`_call_with_retry` 前）、`ModelResponded(turn, stop_reason, text=_join_text(response))`（response 到手、turns+=1、usage 累计后；tool_use 轮 text 常为 ""）、`ToolStarted`（`_dispatch_one` **入口、任何分支判断之前**，对每个 tool_use 块都发）、`ToolCompleted`（`_dispatch_one` **4 条 return 路径**前——成功 / 幻觉名 / malformed(`TypeError`) / error envelope，与产出的 ToolInvocation 一一对应）、`RunFinalized(terminal_status, turns)`（`_finalize` 内构造 LoopResult 前）。**fail-loud 路径不发 ToolCompleted**：`ToolPolicyViolation` / `ToolError` / 已注册 handler 的 `KeyError` / `CancelledError` 在 `_dispatch_one` 不捕获、直接上抛（既有路由不改），这些块已发的 ToolStarted 无配对 ToolCompleted、异常传出 run 不发 RunFinalized。验收：见 §5（含幻觉工具发事件、fail-loud 不发 ToolCompleted、并行偏序）。

## 3. PlannerAgent 透传 observer

- [x] 3.1 `agent/planner.py`：`run(intent, *, observer: LoopObserver | None = None)`，`await self._loop.run(intent, observer=observer)`。不解释不消费。验收：observer 透传单测（§5.2）；不传 observer 行为不变（既有 test_planner.py 全绿）。

## 4. CLI `--intent` 集成

- [x] 4.1 `src/hostlens/cli/_intent.py`：`RichLiveObserver`（实现 LoopObserver，`match event` 维护 Rich Tree/Table 轮次→工具→ok/err，`Live(console=Console(stderr=True))` 增量刷新，`RunFinalized` 停 Live；**`on_event` 整体包 `try/except Exception` 自吞渲染错误降级为静默/纯文本一行，绝不向 loop 抛**——隔离责任在 observer 自身，见 design D-2/D-7）+ 装配助手 `build_planner(settings, target_registry, inspector_registry, logger) -> PlannerAgent`（`create_backend` + `ToolRegistry`+`register_default_tools` + context_factory 产出 ToolContext；backend 不进 ToolContext）+ 渲染助手 `render_planner_result(result, fmt) -> str`（md: narrative + findings 摘要表 + 遥测行；json: `result.model_dump_json()`）。验收：mypy --strict；非 TTY 下 Rich 自动降级不报错；单测构造抛异常的渲染输入断言 on_event 不向外抛。
- [x] 4.2 `cli/inspect.py`：`--inspector` 默认改 `None`（去掉 `...` 必填），新增 `--intent: str | None`。命令体最前互斥校验：both None / both set → exit 3 + stderr 一行；仅 inspector → 原路径；仅 intent → 分流到 intent 路径。`--timeout` 与 `--intent` 组合：忽略 timeout 并 stderr 提示一行（design D-6）。验收：`--json` 输出 schema 稳定（json 模式 dump PlannerResult）；非交互无歧义。
- [x] 4.3 intent 路径主体：装配 PlannerAgent → `asyncio.run(planner.run(intent, observer=RichLiveObserver(...)))` → 退出码映射（design D-6：ok+无critical→0 / ok+critical→1 / 降级失败→2 / 配置usage→3）→ `render_planner_result` 写 stdout 或 `--output`。`ConfigError`（backend 未配置）→ exit 3 一行提示指向 doctor。KeyboardInterrupt/CancelledError → `internal: cancelled` exit 2（复用现有处理）。CLI 边界 `except Exception` 包 `internal: <kind>: <msg>` 不泄露 traceback。复用 `_with_restored_structlog_config` 保护。验收：见 §5。

## 5. 测试

- [x] 5.1 `tests/agent/test_loop_observer.py`：用 `RecordingObserver`（append 事件、不抛）+ FakeBackend：① **单工具**「tool_use→end_turn」断言全序 `TurnStarted→ModelResponded→ToolStarted→ToolCompleted→TurnStarted→ModelResponded→RunFinalized` 且 `RunFinalized.terminal_status==LoopResult.terminal_status`；② `ToolCompleted.invocation` 与 `LoopResult.tool_invocations` 同一条；③ **同轮并行多工具**：断言偏序——每个 tool_use_id 各有一对 Started/Completed 且同块 Started 先于 Completed，按 tool_use_id 关联（**不**假设两块间全序）；④ **幻觉工具名**块：断言仍发出该块的 ToolStarted+ToolCompleted（invocation.error 非空）；⑤ observer=None → 与不传时 LoopResult 完全一致（无回归）；⑥ **loop fail-loud（observer 抛）**：传一个 on_event 会抛的 observer，断言异常**向上传播**（loop 不吞）——验证 loop 不写防御性 try/except；⑦ **fail-loud 工具路径不发 ToolCompleted**：构造一个 dispatch 抛 `ToolPolicyViolation` 的工具（如注册一个 side_effects="write" 的 stub，M2 policy gate 会拒），断言该块发了 ToolStarted 但**无**配对 ToolCompleted、无 RunFinalized，且 `run` 抛出异常（loop 既有 fail-loud 路由不被 observer 改变）。
- [x] 5.2 `tests/agent/test_planner.py` 增用例：`PlannerAgent.run(intent, observer=rec)` → rec 收到内部 loop 的完整事件序列；不传 observer 行为不变。
- [x] 5.3 `tests/cli/test_inspect_intent.py`（驱动 `hostlens.cli.main` + patched `sys.argv`，复用 test_inspect.py 风格，走通入口 usage-exit 改写）：① both None → exit 3 + stderr「必须提供 --inspector 或 --intent 之一」；② both set → exit 3 + stderr「互斥」；③ 仅 inspector → 原路径未回归（加一条 smoke 或引用既有）；④ backend 未配置 → exit 3 指向 doctor；⑤ **playback 端到端**：配 `backend.type=playback` + record-then-replay 生成的 cassette（M2.4 模式，保证不 miss），`--intent` 跑通 → stdout 含 narrative + findings 摘要、stderr 含实时进度行、terminal_status=ok → exit 0；⑥ json 模式 → stdout 是合法 PlannerResult JSON；⑦ 降级（cassette/fake 制造 max_tokens 或 unavailable）→ exit 2 且仍输出部分结果。
- [x] 5.3b **既有 inspect 测试回归修正**（Codex review #6）：`--inspector` 由必填改可选会破坏 archived `inspect-cli-command` spec 的「缺 --inspector 报错」场景与 `tests/cli/test_inspect.py` 对应用例。更新该用例：`hostlens inspect <t>`（缺 inspector **且**缺 intent）→ exit 3 + 新提示文案（不再是 `Missing option '--inspector'`，而是命令体互斥校验的「必须提供其一」）；确认其余既有 inspect 用例（含 --help 列 6→7 选项、--timeout 系列）不回归。
- [x] 5.4 Anthropic API 降级 vs fixture 失败两种路径分别验收（CLI 层）：
  - **降级路径**：用一个持续 raise `BackendUnavailable` 的 fake backend（**不是** cassette miss）→ loop 重试耗尽 finalize `failed_api_unavailable` → `--intent` exit 2、stderr 标降级、CLI 未在 loop 之上重试、不泄露 traceback。
  - **fixture 失败路径**：`CassetteMiss` 是非可重试 backend 错误，loop **不**降级而是上抛 → CLI `except Exception` 包成一行 `internal: CassetteMiss: ...` → exit 2，不泄露 traceback。断言这是 `internal:` 包装而**非** terminal_status 降级（与 proposal FM5 一致；正式 playback 用例 5.3⑤ 用 record-then-replay 保证不 miss）。
- [x] 5.5 密钥脱敏：构造 inspector/工具失败 envelope 含敏感串场景，断言实时进度(stderr) 与最终报告(stdout) 都不出现未脱敏的密钥/路径（复用 scrub_exception_message 已脱敏的 envelope，CLI 不二次反脱敏）。

## 6. 质量门

- [x] 6.1 `mypy --strict` 通过新增/修改文件（events.py / loop.py / planner.py / cli/_intent.py / cli/inspect.py），无未注释 `Any`。
- [x] 6.2 `ruff check` + `ruff format --check` 通过；新增代码注释只写 WHY（CLAUDE.md §6）；loop 的 emit 点用单行助手保持核心控制流可读（面试官能看懂）。
- [x] 6.3 全量 `pytest -m "not live"` 绿，无回归（重点确认既有 test_loop.py / test_planner.py / test_inspect*.py 不受 observer 与 --inspector 可选化影响）。
