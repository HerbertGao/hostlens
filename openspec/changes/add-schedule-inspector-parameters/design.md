# 设计:add-schedule-inspector-parameters

## 决策 1:`inspector_parameters` 是 `dict[str, dict[str, Any]]`,不是按 inspector 的强类型联合

manifest 字段定为 `inspector_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)`。外层 key 是 inspector canonical name（如 `net.listening_ports`），value 是参数对象（任意 JSON dict）。

**为什么不强类型**：每个 inspector 的参数 schema 是它自己 manifest 里的 `parameters` JSON schema（运行时数据，不是 Python 类型）。schedule 层若想强类型就得在 `ScheduleManifest` 里枚举所有 inspector 的参数模型——既无法对外部 / 社区 inspector 成立，也把 inspector SOT（YAML manifest）泄进调度 schema。所以调度层只承诺「透传」,合法性校验交给 inspector 自身的 `parameters` schema（见决策 2 的双重把关）。

`Any` 在本项目默认禁（`mypy --strict`），此处是**有意豁免**:参数对象的 schema 在另一层（inspector YAML）、不在 Python 类型系统里,这正是 CLAUDE.md §6 允许的「有清晰注释说明为什么」的 `Any`。字段处加注释指明裁判在 inspector schema。Pydantic 对 `dict[str, dict[str, Any]]` 会拒绝**标量 value**（`{net.listening_ports: "derper"}` 中 value 不是 mapping → `ValidationError`）;**内层** dict 故意不在调度层校验（由 inspector schema 在决策 2 裁定）。

## 决策 2（已按 review 修订）:loader 注入 `InspectorRegistry`,在加载期对每个 `inspector_parameters` 条目做完整 fail-loud 校验

**修订动机（review 抓的 blocker）**:8 个内置默认健康 inspector 里**只有** `net.listening_ports` 声明了 `parameters:` block,其余 7 个（`linux.cpu.top_processes` / `linux.memory.pressure` / `linux.disk.usage` / `linux.fs.inode_pressure` / `linux.system.load_avg` / `linux.systemd.failed_units` / `log.tail.error_burst`，名取自 `DEFAULT_HEALTH_INSPECTORS`）**无** `parameters:`。`InspectorRunner.run` 的参数校验**整段 gated 在 `if manifest.parameters is not None:`（runner.py:243）**——给一个**无参 inspector** 传参,runner 既不校验也不报错,参数被**静默丢弃**（且原始 params 会并入 finding DSL 上下文、可能影子 output 字段）。若 loader 只验「key 在解析集内」,`{linux.disk.usage: {...}}` 会过校验却静默无效——正是 loader 本要拦的「静默无效」footgun。故 loader **必须**拿到 inspector 的 `parameters` schema 才能正确把关 → 注入 `InspectorRegistry`。

`load_schedules` 当前签名只注入 `TargetRegistry`。本提案**新增** `inspector_registry: InspectorRegistry` 参数（与 `TargetRegistry` 对称:target 校验靠 target registry,inspector 参数校验靠 inspector registry）。**接线比「3 个调用点」复杂**（review 抓的 wiring undercount,须如实落进 tasks）:

- **`cli/schedule.py`**:`load_schedules` 经 helper `_load_manifests_or_exit(settings, target_registry)` 调用,实际 call site 有 **4 处**:`list_cmd`(:328) / `trigger_cmd`(:362) / `_serve`(:434，**run 与 daemon 共用**——`run_cmd`/`daemon_cmd` 不直接调 loader、都委托 `_serve`) / `status_cmd`(:526)。要改的是**helper 签名** + 这 4 处传 inspector registry:`_serve`(:433) 与 `trigger_cmd`(:361) **已构建** inspector registry——只需把现有的穿进调用;`list_cmd` / `status_cmd` 当前只建 target registry——须新增 build。`_build_inspector_registry` 已存在。
- **`cli/doctor.py`**:`_check_schedules` **当前不为 schedule 检查建 inspector registry**(它给 target 检查用「缺 targets.yaml 时空 registry」的设计)。新增 inspector registry build 后,其 except 元组 `(ConfigError, TargetError, ValidationError, yaml.YAMLError)` **不含 `InspectorError`**——若 inspector registry build 自身 raise（坏 builtin → 致命 `InspectorError`),doctor 会崩,违反「doctor 不得因坏 manifest 崩」。故须把 inspector-registry-build 的失败纳入 doctor 的 except 处理（转成 `CheckResult(status="error")`,不外抛)。
- **`cli/mcp.py`**:`load_schedules` 在 `_build_management_deps` 的 `load_manifests=lambda: ...` 闭包里;`serve` 已在 `mcp.py:202` 建了 inspector registry,把它穿进闭包即可。

3 个文件、但实际改 helper 签名 + 4 schedule call sites（`list_cmd`/`status_cmd` 新建 registry、`_serve`/`trigger_cmd` 穿现有）+ doctor 的 build & except + mcp 闭包。

loader 对每个 `(key, params)` 条目按序 fail-loud 校验（消息含**文件名 + key + 原因**,与既有 target / name 校验同基调,**全部走 `ConfigError`**——不得泄露 `InspectorError` 等其它异常类型）:

1. **mode 适用性**:`mode != "deterministic"`（agent 或省略）且 `inspector_parameters` 非空 → `ConfigError`（agent 模式不消费,静默接受会误导）。
2. **key 在运行集内**:`mode == "deterministic"` 时 `key ∈ resolve_inspector_set(manifest.inspectors)`（无 `inspectors:` → 默认健康集,有则该权威集,**不与默认集取并**）。越界 → `ConfigError` 列 key。
3. **inspector 已注册**:`registry.get(key)` 取 manifest。**注意**:step 2 对**显式 `inspectors:`** 集只验「key 在用户写的集里」、`resolve_inspector_set` 原样返回（不验注册）。故显式集成员可能是 typo / 未注册名（如 `inspectors: [net.listening_prots]`),`registry.get` 会 raise `InspectorError(inspector_not_found)`。loader **必须** `try/except InspectorError` 把它**翻译成 `ConfigError`**(含文件名 + key)——否则裸 `InspectorError` 逃出 loader 契约、`schedule` CLI / `doctor` 的 except 都不接它 → 崩。
4. **inspector 须声明 parameters（loader 独有生产门）**:`manifest.parameters is None` 且 `params` 非空 → `ConfigError`（「inspector X 不接受参数」)。空 dict `{}` 不触发（无副作用,等价于不传）。**这条只在 loader**——见下「无参 inspector 为何只在 loader 拦」。
5. **参数值合法（双门同 helper）**:对声明了 `parameters` 的 inspector,调用**与 runner 完全相同**的 helper（见下）→ 失败翻译成 `ConfigError`。typo（`additionalProperties:false` → 未知 key）、`allowed_processes:[""]`（被 items pattern 拒,见决策 4）、类型错、以及 inspector 自身 `parameters` schema 畸形（`SchemaError`）都挡在**加载期**——不拖到下次触发。

**双门同 helper（消除 raw-validate 分歧 + 对齐异常类型）**:

- *为何复用 helper*:review 证明「loader raw-validate ⊆ runner coerce-validate」**一般情形假**——runner 是 `_apply_schema_defaults → _coerce_parameters → jsonschema.validate`(先注默认后校验),若 loader 只 raw `validate`(不注默认),对**有 `required` 且带 `default`** 的未来 inspector,loader 会拒掉 runner 本会接受的配置(方向反了)。故 loader 与 runner 复用同一公共 helper:从 `inspectors.runner` 抽 `coerce_and_validate_parameters(params, manifest) -> dict`,其**第一行** `params = dict(params or {})`(归一化 `None`/falsy 入参,两个 caller 都传原始 params),随后 defaults + coerce + `jsonschema.validate`。对**有参** inspector,两门走同一 helper → 接受集**相等**(非 ⊆)。
- *异常类型必须对齐（review 抓的新泄漏)*:`jsonschema.validate` 对**畸形的 inspector 自身 schema** raise 的是 `SchemaError`、非 `ValidationError`;`InspectorRunner.run` 现就两者都接(runner.py:248 `ValidationError`→`parameter_validation_failed`、:265 `SchemaError`→`parameter_schema_invalid`)。故 helper 异常契约是 `(ValidationError, SchemaError)`,**两个 caller 都必须接这两类**:runner 包 `status="exception"`、loader 包 `ConfigError`。只接 `ValidationError` 会让 `SchemaError` 裸泄漏出 loader(`schedule`/`doctor` 的 except 都不接 → 崩),与刚修的 `registry.get`→`InspectorError` 泄漏同类。
- *无参 inspector 为何只在 loader 拦（精确 ≡ 边界、不扩 runner 全局行为)*:runner 对无参 inspector 保持现状(`manifest.parameters is None` → 跳过校验、不报错),**本提案不改 runner 这条全局语义**(改它会让 `hostlens inspect <无参> --param ...` 等**所有**面从「静默忽略」变「报错」——超出本特性范围、属 scope creep)。故「接受集相等」**精确指有参 inspector**;无参 inspector 的「传参即拒」是 **loader 独有生产门**(step 4)。生产路径(经 loader)永不让无参+非空参数到达 runner;唯一例外是**绕过 loader 直接构造 `ScheduleManifest`**(测试 / 未来 MCP propose,非生产)——该窄路径下无参 inspector 的非空参数仍被 runner 静默丢弃(可能影子 output)。这是**有意接受的窄、非生产可达边界**,如实记入 spec,不靠扩 runner 全局行为去消(保范围最小)。

**loader 拿到的 registry 须诚实**:`build_registry_from_search_paths(...).registry` 会**丢弃** `.errors`（user-path 自定义 inspector 解析失败被收集+跳过、不进 registry)。若某 schedule 的 `inspector_parameters` 引用了一个**加载失败的 user inspector**,step 3 会当它「未注册」→ `ConfigError`。这是正确的 fail-loud（参数指向不可用 inspector → 拒），但消息应能区分「未注册」与「加载失败」。loader 用的 registry build **不应静默吞** 与被引用 inspector 相关的 errors;tasks 记一条让消息清晰、且至少不误导。

**`resolve_inspector_set` 迁址**:它现居 `orchestration/deterministic.py`,而该模块 top-level import 了 agent / backend 重依赖。让 `loader`（被 `doctor` / `schedule` CLI 用）import 它会把 agent 栈拖进这些轻量路径。但 `resolve_inspector_set` 本身是**纯**逻辑（`None → DEFAULT_HEALTH_INSPECTORS`,否则 verbatim,无 orchestration 依赖)。故**把它迁到 `inspectors/health.py`**（与 `DEFAULT_HEALTH_INSPECTORS` 同住,语义自洽:它就是「健康集解析」),`deterministic.py` 改从新址 import（保持 SOT 单点,不复制逻辑、不漂移)。这同时消除「scheduler→orchestration 分层倒置」与「重依赖污染」两个隐患。loader 从 `inspectors.health` import,纯净轻量。

## 决策 3:参数透传发生在 `_bounded` 内,key 是 `manifest.name`;runner 是 defense-in-depth 二道门

`run_deterministic_inspection` 内层 `_bounded(manifest: InspectorManifest, target)` 是唯一调 `runner.run` 处:

```python
params = (inspector_parameters or {}).get(manifest.name)  # 缺 key → None;present-but-{} → {}
return await runner.run(..., parameters=params)
```

`manifest.name` 与 `inspector_parameters` 的 key 同命名空间——直接 `.get`。**`.get` 仅在 key 缺失时返 `None`**;一个**显式存在但值为 `{}`** 的条目是「命中」返 `{}`（非 `None`）。对 `net.listening_ports` 这类有参 inspector,`{}` 经 runner 的 `_apply_schema_defaults` 注入 `allowed_processes: []`——与 `None` 等价结果;但「未命中→`None`」「命中空 dict→`{}`」语义须分清（spec 各加一条场景),避免未来对「有必填无默认参数」的 inspector 误判。

链路 `runner._run_job → run_deterministic_pipeline → run_deterministic_inspection → InspectorRunner.run` 逐跳直传,不改写参数对象。新增 keyword 类型用 **`dict[str, dict[str, Any]] | None`**（不是 `Mapping`)——`InspectorRunner.run` 的 `parameters` 形参是 `dict[str, Any] | None`,`.get(name)` 须返回可直接赋值的 `dict[str, Any] | None`,`Mapping` 在 `mypy --strict` 下不可赋值给 `dict`。

**runner 仍在运行期校验（defense-in-depth）**:loader 是主门,但 `ScheduleManifest` 可被**绕过 loader 直接 Pydantic 构造**(测试 / 未来 MCP propose)——既有 target-cardinality / name-uniqueness 校验同样只在 loader（schema.py docstring 已明示这一既定模式)。绕过 loader 的脏 key 进 `_bounded`,`.get` 未命中即 inert（never read);若命中且参数非法,runner 的 `jsonschema.validate`(runner.py:247)兜底 → 返 `status="exception"`、`error="parameter_validation_failed"`,在 fleet 报告里**可见**(exception 计入 severity、非 requires_unmet)。即:loader 在加载期 fail-loud(最佳 UX),runner 在运行期兜住绕过路径(不静默)。

**output-field 影子**:`_evaluate_findings`(runner.py:677)的 DSL 上下文是 `{**output, **parameters, ...}`,参数键覆盖 output 键。对**有参** inspector,`additionalProperties:false` 使任何不在 `properties` 的参数键在加载期(决策 2-step5)及 runner 运行期被拒,故无法注入影子键(如 `net.listening_ports` 的 `results`)。对**无参** inspector,**生产路径**经决策 2-**step4** 直接拒非空参数 → 不进 DSL → 无影子风险;**唯一例外**是上文「无参 inspector 为何只在 loader 拦」记的**非生产 bypass 路径**(绕过 loader 直接构造 manifest)——该窄路径下无参 inspector 的非空参数 runtime 不拒、仍并入 DSL、可能影子,是有意接受的非生产可达边界。

## 决策 4（已按 review 修订）:`net.listening_ports.allowed_processes` 须带 items `pattern`,与 `allowed_ports` 同构

新参数:

```yaml
allowed_processes:
  type: array
  items: { type: string, pattern: "^[A-Za-z0-9._@-]+$" }
  default: []
```

**`pattern` 是硬约束（review 抓的 blocker）**:inspector-authoring-contract 要求**每个会被用户值填充的 string 字段**(含 `type: array` 且 `items.type == "string"`)**必须**声明 `pattern` 或 `enum`（loader.py:236 强制,内置 manifest 违反 → registry build **崩**、启动失败)。`allowed_ports` 是 `items.type == integer`、整数豁免该约束;`allowed_processes` 是 string array、**必须**带 pattern。`^[A-Za-z0-9._@-]+$` 覆盖真实进程名(`easytier-core` / `snell-server` / `sing-box` / `bark-server` / `hbbr` / `derper` / `rustdesk` / `hysteria-server` …:字母数字 + `.` `_` `@` `-`),且 `+`(≥1 字符)**天然拒空串** → 同时关掉 `allowed_processes: [""]` 会静默放过所有未归因监听口的洞（review 的 B2）。

finding `when`:`p.wildcard == True and p.port not in allowed_ports and p.process not in allowed_processes`。三条件 AND:通配地址 **且** 端口未豁免 **且** 进程未豁免才 warn。

**进程名空串保守边界**:`process` 由 awk 从 `ss -tlnp` 的 `users:(("name",...))` 抽出;非特权用户对**他人** socket 拿不到进程名 → `process == ""`。`pattern` 拒空串只约束**用户写的 allowlist 成员**,**不**约束 inspector **采集到的** `process` 值（那是 output、走 output_schema)。故采集到 `process == ""` 时,`"" not in <非空 allowlist>` 为真 → **仍 warn**(拿不到身份的通配监听口必须可见,有意保守)。`pattern` 保证 allowlist 里不可能有 `""`,所以「`"" 被豁免`」这条路径不存在。两件事(allowlist 成员约束 vs 采集值)分清。

**精确 membership 不子串**:`snell-server` 不放 `snell-server-evil`;逐字精确最不易误豁免。子串 / 正则 / glob 列非目标。

## 决策 5:net.listening_ports 行为以 manifest + 测试为契约,不建 per-inspector spec

`allowed_processes` 的语义(精确 membership、空进程仍 warn、pattern 拒空串)**不**单独立 per-inspector spec——与**所有**既有 builtin inspector 一致(它们都无逐 inspector spec,manifest 是 SOT,参数约定已在 inspector-authoring-contract 以 `allowed_ports` 为例固化)。本提案**不新增** authoring 约定(`allowed_processes` 是既有「pattern 约束 string array + DSL membership」约定的**新实例**、非新规则),故守住 proposal 的「不补 inspector-authoring-contract」非目标。

但**安全相关语义不靠自觉**:三条(`process ∈ allowed_processes → 不报`、`process ∉ → 报`、`process == "" → 报`)作为 **tasks.md 4.3 的显式验收锚**(可执行测试)钉死,比 prose 场景更强——offline fixture 直接断言 finding 产/不产。review 担心的「保守语义静默回归」由这组测试兜住。

## 决策 6:版本与兼容

`net.listening_ports` `1.0.0 → 1.1.0`(minor:新增可选参数、默认值保旧行为)。`inspector_parameters={}`、`allowed_processes=[]` 默认使既有 manifest / `inspect` 调用零行为变化,无迁移。`run_deterministic_inspection` 对未在 `inspector_parameters` 的 inspector 传 `None`,与改前**逐字节一致**(runner 对 `None` + 无参 = 原路径;对 `None` + 有参 = `_apply_schema_defaults` 注入 `[]`,旧 `when` 等价)。

**默认集耦合(review 的 forward-fragility 披露)**:用默认健康集(无 `inspectors:`)的 manifest 若给某默认 inspector 配 `inspector_parameters`,该 key 的有效性**耦合**到该 inspector 持续在 `DEFAULT_HEALTH_INSPECTORS` 内;未来若把它移出默认集,该 manifest 会在**加载期** fail-loud（决策 2-step2 越界拒)——**可见、非静默**,可接受。`test_health_default_set.py` 只验 registry 成员、不验此耦合,故该回归只在集成层现身;此披露入 spec 备注、不另设门槛。

## 测试策略

- **schema**:`inspector_parameters` 解析 dict-of-dict;**标量** value（string）被 Pydantic 拒（精确锚,不过度声称内层校验);省略 → `{}`;top-level `extra="forbid"` 不破。
- **loader**(注入 fake/real inspector registry):agent + 非空 → fail;deterministic + 越界 key → fail(含 key);deterministic + 无参 inspector key + 非空 params → fail("不接受参数");deterministic + typo 参数键(net.listening_ports)→ fail(加载期,含 jsonschema 消息);deterministic + 显式 `inspectors:[disk]` + key 不在显式集 → fail;deterministic + key 在显式集且参数合法 → pass;deterministic + 空 → pass;无参 inspector + `{}`(空)→ pass(no-op)。
- **deterministic**:命中 → `runner.run` 收声明 dict;未命中 → `None`;present-but-`{}` → `{}`;pipeline→inspection 直链不丢;空 → 全 `None`。
- **resolve_inspector_set 迁址回归**:`inspectors.health.resolve_inspector_set` 与 `deterministic` 重导出同一对象,行为不变。
- **net.listening_ports finding**(offline fixture):process ∈ allowed_processes → 不产;process ∉ → warning;port ∈ allowed_ports(原路径)→ 仍不产(`test_os_net.py::test_listening_ports_unexpected_detected` / `::test_listening_ports_ok_no_findings` 不变绿,采集 shell 未改、cassette 稳定);process == "" + 非空 allowed_processes → warning(保守边界锚)。
- **manifest 加载**:`allowed_processes` 带 pattern → registry build `errors == []`(防 Codex#1 类崩)。
- **mypy --strict + ruff** 全绿(含 `Any` 豁免注释)。
