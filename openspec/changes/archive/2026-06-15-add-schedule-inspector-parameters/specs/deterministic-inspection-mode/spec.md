## 新增需求

### 需求:deterministic 模式必须把 `manifest.inspector_parameters` 透传给匹配的 inspector

`mode=deterministic` 的 job body 对每个 target 跑固定 inspector 集时,对每个 inspector **必须**查 `inspector_parameters`（来自 `ScheduleManifest.inspector_parameters`,键为 inspector canonical name）:

- key **缺失**（该 inspector 不在 `inspector_parameters` 里）→ 传 `parameters=None`（跑默认参数）。
- key **存在**（含值为空 dict `{}`）→ 传该值。`{}` 是「命中、空参数」,**不**等同缺失的 `None`;对有默认参数的 inspector 二者结果等价（`_apply_schema_defaults` 注入默认),但语义须分清以免未来对「有必填无默认参数」inspector 误判。

透传链 `SchedulerRunner._run_job → run_deterministic_pipeline → run_deterministic_inspection → InspectorRunner.run`（注意首尾两个 `runner` 是不同类:前者 `SchedulerRunner`、后者 `InspectorRunner`）**禁止**丢弃或改写参数对象。

- 参数的合法性**主**由加载器在加载期校验（见 schedule-manifest 能力「加载器必须注入 InspectorRegistry」需求:未注册 inspector / 无参 inspector 传参 / typo / 非法值 / 畸形 schema 在加载期 `ConfigError` fail-loud）。`InspectorRunner.run` 用**同一个**参数校验 helper（`dict(params or {})` + defaults + coerce + validate,接 `ValidationError` 与 `SchemaError` 两类）作**运行期 defense-in-depth 二道门**:覆盖**绕过 loader 直接构造 `ScheduleManifest`** 的路径(测试 / 未来 MCP propose)——此路径下**有参 inspector** 的非法参数 → `InspectorResult(status="exception", error="parameter_validation_failed: ...")`,在 fleet 报告里**可见**（exception 计入 severity、非 requires_unmet）,**不**静默成功。
- **接受集相等的精确边界**:两门接受集相等**仅对有参 inspector**(同 helper、同异常)。**无参 inspector + 非空参数**只在**加载器**拒（生产门);runtime 对无参 inspector 保持「`manifest.parameters is None` → 跳过校验」的既有全局语义(本能力**不**改它,避免波及 `hostlens inspect` 等所有面)。故绕过 loader 直接构造 manifest 给无参 inspector 传非空参数,runtime **不**报错、参数被静默丢弃(可能影子 finding DSL 的 output 字段)。这是**有意接受的窄、仅经非生产 bypass 可达**的边界——生产路径经 loader step 4 永不到此。
- 本能力**不**改变 inspector 集解析、capability 过滤、status 派生与 fleet severity 聚合:`inspector_parameters` 只影响某 inspector 在给定参数下「报不报某条 finding」。
- **参数键与 output 字段影子**:`InspectorRunner` 求值 finding 时参数键覆盖 output 键;对**有参** inspector,`additionalProperties:false` 使非声明键在加载期(step 5)及运行期被拒,无法注入影子键;对**无参** inspector,**生产路径**经加载器 step 4 拒非空参数、不进 DSL → 无影子;**仅非生产 bypass 路径**(绕过 loader)下无参 inspector 的非空参数 runtime 不拒、可能影子(上一条记的窄边界)。
- `inspector_parameters` 为空（默认）时,所有 inspector 收到 `parameters=None`,采集行为与本提案前**逐字节一致**。

#### 场景:命中 inspector 收到声明的参数
- **当** `mode=deterministic`、`inspector_parameters={"net.listening_ports": {"allowed_processes": ["derper"]}}`,且 `net.listening_ports` 在解析出的 inspector 集内,对某 target 采集
- **那么** `net.listening_ports` 的 `InspectorRunner.run` 必须收到 `parameters={"allowed_processes": ["derper"]}`

#### 场景:未命中 inspector 收到 None
- **当** 同一次采集里运行集内的另一个 inspector(如 `linux.disk.usage`)未出现在 `inspector_parameters`
- **那么** 它的 `InspectorRunner.run` 必须收到 `parameters=None`(跑默认参数)

#### 场景:命中但值为空 dict 收到空 dict
- **当** `inspector_parameters={"net.listening_ports": {}}`,对某 target 采集
- **那么** `net.listening_ports` 的 `InspectorRunner.run` 必须收到 `parameters={}`（命中、空参数;经 `_apply_schema_defaults` 与 `None` 结果等价但语义为「命中」）

#### 场景:绕过 loader 的有参 inspector 非法参数运行期被兜住
- **当** 绕过加载器直接构造 `ScheduleManifest`（如测试 / 未来 MCP propose），给一个**有参** inspector 传了不合法参数（typo 键 / 类型错），deterministic 采集该 inspector
- **那么** `InspectorRunner.run` 经同 helper 校验失败 → 产 `InspectorResult(status="exception", error="parameter_validation_failed: ...")`，在 fleet 报告里**可见**、不静默成功

#### 场景:命中无参 inspector 且值为空 dict 无副作用
- **当** `inspector_parameters={"linux.disk.usage": {}}`（`linux.disk.usage` 无 `parameters:` block,值为空 dict 通过加载器 step 4）,对某 target 采集
- **那么** `InspectorRunner.run` 收到 `parameters={}`;因 `manifest.parameters is None`,校验 / 默认 / coerce 整段跳过,`{}` 不进 finding DSL 上下文、不改变任何 finding 逻辑（无害 no-op）

#### 场景:空 inspector_parameters 行为不变
- **当** `mode=deterministic` 且 `inspector_parameters={}`(默认),对各 target 采集
- **那么** 所有 inspector 必须收到 `parameters=None`,采集结果与本提案前一致
