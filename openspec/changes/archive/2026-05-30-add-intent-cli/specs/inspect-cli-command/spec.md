## MODIFIED Requirements

### 需求:`hostlens inspect` 命令必须支持 6 个选项与 1 个位置参数

`hostlens inspect <target>` 必须接受以下参数（Typer 定义；标题沿用 M1.7 名称作为稳定标识符，M2.7 新增 `--intent` 后选项总数实为 7，下列为权威清单）：

- 位置参数 `target: str`（必填）：target 名；从 `TargetRegistry` 查询
- 选项 `--inspector / -i <name>: str | None = None`（可选）：inspector 名；从 `InspectorRegistry` 查询。与 `--intent` **互斥**
- 选项 `--intent <自然语言>: str | None = None`（可选）：自然语言巡检意图，触发 `PlannerAgent` 自主规划。与 `--inspector` **互斥**
- 选项 `--output / -o <FILE>: Path | None = None`：输出文件路径；缺省 stdout
- 选项 `--format / -f <md|json>: Literal["md", "json"] = "md"`：输出格式
- 选项 `--parameters / -p <JSON>: str | None = None`：JSON 字符串或 `@<path>` 文件引用（如 `@./params.json`）；缺省传 `{}` 给 InspectorRunner（仅 `--inspector` 路径有意义）
- 选项 `--allow-privileged: bool = False`：opt-in 允许跑 `privilege != "none"` 的 Inspector（仅 `--inspector` 路径有意义）
- 选项 `--timeout <SECONDS>: int | None = None`：单 Inspector 超时（整数秒；不接受 float）；缺省 None = 不覆盖 manifest `collect.timeout_seconds`；**值校验**：`1 <= value <= 300`（与 archived `inspector-plugin-system` spec 中 `CollectSpec.timeout_seconds = Field(ge=1, le=300)` 严格一致），不在范围 → exit 3 + stderr `invalid --timeout: must be in [1, 300]`；**实现路径**：`InspectorRunner.run()` 不接受 timeout 覆盖参数（保持其 inspector-plugin-system spec 中的契约不变），CLI 必须在 dispatch 前**重构 CollectSpec 让 Pydantic validation 生效**：`from hostlens.inspectors.schema import CollectSpec; new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": cli_timeout}); manifest_for_run = manifest.model_copy(update={"collect": new_collect})` 再传给 runner —— **禁止**直接用 `manifest.collect.model_copy(update={"timeout_seconds": cli_timeout})` （Pydantic v2 `model_copy(update=...)` **不**触发字段 validation，会让 [1, 300] 外的值静默写入）；CollectSpec 的 `Field(ge=1, le=300)` 在构造时会触发 validation 作为防御纵深第二道；**禁止** 改 `InspectorRunner.run()` 签名或修改 archived `inspector-plugin-system` spec 的 runner 契约；`--timeout` 仅对 `--inspector` 路径生效，`--intent` 路径忽略它并在 stderr 提示一行

**`--inspector` 与 `--intent` 互斥校验**：二者必须**恰好提供其一**。两者都缺、或同时提供，都必须以 usage error（exit 3）失败，stderr 给出一行说明，不泄露 Python traceback。仅 `--inspector` 走 M1 单 Inspector 管线（行为不变）；仅 `--intent` 走 Planner Agent 路径。

**`--help` 输出必须** 列出全部参数 + 简要描述 + 退出码语义（4 行：`0: healthy / 1: critical finding / 2: runner failure / 3: usage error`）。

**Typer 默认 usage exit 转换**：Typer 自身对 `Missing argument` / `Missing option` / `Invalid value for` 等 usage 错误默认 exit 2 —— 这与本提案 exit 2 = runner failure 定义冲突。**CLI 入口必须** 包裹 Typer app 调用，**仅**针对 usage error（`click.exceptions.UsageError` 及其子类 `BadParameter` / `MissingParameter`，或 `SystemExit(code=2)`）**改写 exit code 为 3**；**禁止** 改写其他 exit code（如 `--help` 的 `SystemExit(code=0)`、`--version` 的 `SystemExit(code=0)`、runner 失败的 `SystemExit(code=2)` —— 区分方式：本提案 CLI 中只有 Click usage exception 会触发 code=2，runner 失败走 `typer.Exit(2)` 显式构造且发生在 try 内部业务路径之外）。互斥校验失败由命令体显式 `typer.Exit(code=3)` 抛出（不依赖 Click usage 改写）。

**只读命令；允许 EUID==0**：与 `hostlens inspectors list/show` / `hostlens target list` 一致；**禁止** 加 EUID==0 检查。`--intent` 路径同为只读巡检入口，同样允许 EUID==0。

#### 场景:`--help` 输出含全部参数

- **当** 执行 `hostlens inspect --help`
- **那么** 输出必须含 `--inspector` / `--intent` / `--output` / `--format` / `--parameters` / `--allow-privileged` / `--timeout` 7 个选项名

#### 场景:`--help` 退出码必须为 0（不被 usage 改写误伤）

- **当** 执行 `hostlens inspect --help`
- **那么** exit code 必须为 0；**禁止** 被 Typer usage exit 改写逻辑误判为 usage error 改成 3

#### 场景:缺位置参数 target 报错

- **当** 执行 `hostlens inspect`（无 target）
- **那么** stderr 必须含 `Missing argument 'TARGET'` 且 exit code 必须为 3（CLI 入口包裹 Typer usage 错误并改写为 3，与全局退出码方案对齐）

#### 场景:缺 --inspector 且缺 --intent 报错

- **当** 执行 `hostlens inspect local-host`（既无 --inspector 也无 --intent）
- **那么** exit code 必须为 3 且 stderr 含一行说明必须提供 `--inspector` 或 `--intent` 之一

#### 场景:--inspector 与 --intent 同时提供报错

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --intent "检查健康"`
- **那么** exit code 必须为 3 且 stderr 含一行说明 `--inspector` 与 `--intent` 互斥

#### 场景:--format 不在 md/json 报错

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --format html`
- **那么** stderr 必须含 `Invalid value for '--format' / '-f'` 且 exit code 必须为 3

#### 场景:允许 EUID==0 运行

- **当** 以 root 用户（EUID==0）执行 `hostlens inspect local-host --inspector hello.echo`
- **那么** 命令**不** 因 root 而 refuse；正常流程继续

#### 场景:--timeout 0 或负数被拒绝

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 0`
- **那么** exit code 必须为 3 + stderr 含 `invalid --timeout:` 前缀（提示 `must be in [1, 300]`）

#### 场景:--timeout 必须经 CollectSpec 重构注入触发 validation

- **当** CLI 收到合法 `--timeout 5` 调度 inspector 时，**禁止**直接 `manifest.collect.model_copy(update={"timeout_seconds": 5})`（绕过 validation）；**必须**用 `CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": 5})` 构造新 CollectSpec
- **那么** 注入路径触发 Field(ge=1, le=300) validation；测试用 monkeypatch 假设 CLI 上限校验绕过（manually patch CLI 的 [1, 300] 校验），传 `--timeout 9999`，期望下游 CollectSpec 构造 raise `pydantic.ValidationError`

#### 场景:--timeout 超过上限被拒绝

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 301`
- **那么** exit code 必须为 3 + stderr 含 `invalid --timeout:` 前缀（提示 `must be in [1, 300]`）

#### 场景:--timeout 上限 300 边界值接受

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 300`
- **那么** CLI 接受参数（exit 0 / 1 / 2 取决于 runner 结果，**不**为 3 拒绝）

#### 场景:--timeout 通过 CollectSpec 重构注入到 runner

- **当** 执行 `hostlens inspect local-host --inspector hello.echo --timeout 5`，CLI 在内部用 `from hostlens.inspectors.schema import CollectSpec; new_collect = CollectSpec(**{**manifest.collect.model_dump(), "timeout_seconds": 5}); manifest_for_run = manifest.model_copy(update={"collect": new_collect})` 构造新 manifest 实例（**不**用 `manifest.collect.model_copy(update=...)`——后者会绕过 Pydantic validation）
- **那么** runner 收到的 `manifest_for_run.collect.timeout_seconds == 5`；InspectorRegistry 中的原始 manifest 实例**未被修改**（CLI 只做临时拷贝；原 manifest 仍走 InspectorRegistry 提供的引用）；CollectSpec 构造过程触发 `Field(ge=1, le=300)` validation 作为防御纵深

#### 场景:--timeout 与 --intent 组合被忽略并提示

- **当** 执行 `hostlens inspect local-host --intent "检查健康" --timeout 5`
- **那么** `--timeout` 不影响 Planner 路径（Agent 工具超时由 ToolSpec 固定），CLI 在 stderr 提示一行 `--timeout 对 --intent 模式无效，已忽略`，不报错、不改变退出码逻辑

## ADDED Requirements

### 需求:`hostlens inspect --intent` 必须装配并运行 PlannerAgent，实时进度走 stderr、报告走 stdout

`--intent` 路径必须用 `create_backend(settings)` + 注册了默认工具的 `ToolRegistry` + 产出含 target/inspector registry 的 `ToolContext` 的 context_factory 装配 `PlannerAgent`，并以一个 CLI 端 observer（实现 `LoopObserver` Protocol）调用 `PlannerAgent.run(intent, observer=...)`。backend 禁止进入 context_factory 产出的 `ToolContext`（ADR-008）。

实时进度（Agent 逐轮的工具调用与每轮返回的 assistant 文本，**非** token 级流式）必须渲染到 **stderr**；最终报告必须输出到 **stdout**（或 `--output` 指定文件）。二者必须分离，使脚本消费 stdout 时不被进度输出污染。`--intent` 字符串只能作为模型的 user message，禁止进入任何 shell/命令渲染路径。CLI 边界必须把任何未预期异常（含从 loop 透传上来的非可重试 backend 错误，如 `CassetteMiss`）包成一行 `internal: <kind>: <msg>` → exit 2，不泄露 Python traceback。

#### 场景:实时进度与报告分流

- **当** `--intent` 运行且 Agent 调用了若干工具
- **那么** stderr 必须出现逐轮/逐工具的实时进度，stdout 必须只含最终报告内容

#### 场景:backend 未配置报配置错误

- **当** `--intent` 运行但 backend 未配置（如缺 `ANTHROPIC_API_KEY`，`create_backend` 抛 `ConfigError`）
- **那么** 必须 exit 3 并在 stderr 给出一行配置错误提示（指向 `hostlens doctor`），不泄露 traceback

### 需求:`hostlens inspect --intent` 必须输出 narrative + findings 摘要 + 遥测，支持 md/json

`--intent` 路径必须按 `--format` 渲染：md 模式输出 `PlannerResult.narrative`（markdown）+ findings 摘要（severity / message / tags）+ 一行 loop 遥测（turns / terminal_status / token usage）；json 模式输出 `PlannerResult` 的 JSON 序列化（含 narrative / findings / loop_result / intent）。**禁止**组装 `reporting.models.Report`（沿用 M2.4 决策）。findings 为空时 md 模式只输出 narrative + 遥测，不报错。

#### 场景:md 模式输出综述与 findings 摘要

- **当** `--intent --format md` 且 Agent 收集到若干 finding
- **那么** stdout 必须含 narrative 文本与 findings 摘要表，并附 terminal_status / token usage 遥测行

#### 场景:json 模式输出可解析的 PlannerResult

- **当** `--intent --format json`
- **那么** stdout 必须是 `PlannerResult` 的合法 JSON（含 narrative / findings / loop_result / intent），可被下游解析

### 需求:`hostlens inspect --intent` 退出码沿用 4 值语义并由 PlannerResult 映射

`--intent` 路径必须按 `PlannerResult` 映射退出码（与 `--inspector` 路径同一 4 值语义，优先级 3>2>1>0）：terminal_status=`ok` 且无 critical finding → `0`；terminal_status=`ok` 且 ≥1 `severity=="critical"` finding → `1`；terminal_status ∈ 降级/失败集合（`degraded_max_turns` / `degraded_token_budget` / `degraded_no_planner` / `degraded_rate_limited` / `failed_api_unavailable` / `empty_response`）→ `2`；参数互斥违规 / backend 配置错误 / `--output` 写失败 / `--format` 非法 → `3`。`PlannerAgent` 降级时 CLI 禁止重试（重试单一收口在 loop），仍输出已收集的 findings 与 narrative。

#### 场景:健康巡检退出 0

- **当** `--intent` 运行结果 terminal_status=`ok` 且无 critical finding
- **那么** 必须 exit 0

#### 场景:critical finding 退出 1

- **当** terminal_status=`ok` 且收集到至少一条 `severity=="critical"` 的 finding
- **那么** 必须 exit 1

#### 场景:降级退出 2 且仍输出部分结果

- **当** terminal_status 为 `degraded_max_turns` / `degraded_token_budget` / `failed_api_unavailable` 等
- **那么** 必须 exit 2，stderr 标注降级原因，stdout 仍输出已收集的 findings 与（可能为空的）narrative，CLI 未重试
