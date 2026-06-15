# schedule-manifest 规格变更:add-schedule-inspector-parameters

## 新增需求

### 需求:`ScheduleManifest` 必须支持 `inspector_parameters` 给 deterministic 调度按 inspector 传参

`hostlens.scheduler.schema.ScheduleManifest` 必须新增字段 `inspector_parameters: dict[str, dict[str, Any]]`，默认 `{}`（`Field(default_factory=dict)`）。语义:外层 key 是 inspector canonical name（如 `net.listening_ports`），value 是透传给该 inspector 的参数对象。

- 该字段是 `ScheduleManifest` 已声明字段,**不破坏** `model_config = ConfigDict(extra="forbid")`——顶层未知字段仍 fail-loud。
- value 的**内层** schema **不**在调度层强类型化:每个 inspector 的参数合法性由 inspector 自身 manifest 的 `parameters` JSON schema 裁定（见下「加载器」需求的 step 3/4 与 deterministic-inspection-mode 能力的运行期兜底）。调度层只承诺**透传**,但**外层形状**（value 必须是 dict）由 Pydantic 强制。
- `inspector_parameters` **仅** `mode=deterministic` 生效。agent 模式不消费它（见下「加载器」需求）。

#### 场景:省略 inspector_parameters 默认空 dict
- **当** 一个合法 deterministic manifest 不写 `inspector_parameters`
- **那么** 必须成功解析且 `manifest.inspector_parameters == {}`,巡检行为与本提案前完全一致

#### 场景:inspector_parameters 解析为 dict-of-dict
- **当** manifest 写 `inspector_parameters: {net.listening_ports: {allowed_processes: [derper]}}`
- **那么** 必须成功解析,`manifest.inspector_parameters["net.listening_ports"]["allowed_processes"] == ["derper"]`

#### 场景:inspector_parameters 顶值为标量被拒
- **当** manifest 写 `inspector_parameters: {net.listening_ports: "derper"}`（value 是标量字符串、非对象）
- **那么** 必须 `ValidationError`（外层 dict-of-dict 形状由 Pydantic 校验;内层不在此层校验）

### 需求:加载器必须注入 `InspectorRegistry` 并 fail-loud 校验 `inspector_parameters`

`hostlens.scheduler.loader.load_schedules` 必须新增注入 `InspectorRegistry`（与既有 `TargetRegistry` 对称:target 成员靠 target registry 校验、inspector 参数靠 inspector registry 校验）。在**加载时**对每个 `inspector_parameters` 条目 `(key, params)` 按序执行 fail-loud 校验,任一不过即 raise——错误**必须**是 `ConfigError`、消息含**出错文件名 + 违规 key + 原因**,**禁止**泄露 `InspectorError` 等其它异常类型、**禁止**静默忽略、**禁止**推迟到 job 触发时:

1. **mode 适用性**:`mode != "deterministic"`（agent 或省略 mode）且 `inspector_parameters` 非空 → 拒绝（agent 模式不消费,静默接受会让用户误以为参数生效）。
2. **key 在运行集内**:`mode == "deterministic"` 时,`key` 必须 ∈ `resolve_inspector_set(manifest.inspectors)`（无 `inspectors:` → 内置默认健康集,有则为该权威集,**不与默认集取并**）。越界 key → 拒绝并列出。
3. **inspector 已注册**:`registry.get(key)` 取 inspector manifest。`resolve_inspector_set` 对显式 `inspectors:` 集**原样返回、不验注册**,故显式集成员可能是未注册名(typo)→ `registry.get` raise `InspectorError(inspector_not_found)`。loader **必须**捕获并翻译成 `ConfigError`(含文件名 + key),**禁止**让裸 `InspectorError` 逃出。step 4/5 **仅**对 step 3 成功取回的 manifest 执行(短路:step 3 raise 后不再跑 step 4/5,不得在未取回 manifest 时访问 `manifest.parameters`)。
4. **inspector 须声明 parameters**:若该 manifest **无** `parameters:` block（`manifest.parameters is None`）且 `params` 非空 → 拒绝（给无参 inspector 传参是静默无效,必须拦)。`params` 为空 dict `{}` 不触发本条。**本条是 loader 独有的生产门**(runtime 对无参 inspector 保持「跳过校验」语义,见 deterministic-inspection-mode 能力的边界说明)。
5. **参数值合法**:对声明了 `parameters` 的 inspector,以**与 `InspectorRunner.run` 完全相同**的参数校验 helper（`dict(params or {})` + defaults + coerce + `jsonschema.validate`,二者复用)校验 → 失败即翻译成 `ConfigError`。loader **必须**捕获该 helper 抛出的 **`jsonschema.ValidationError`（非法值 / typo / 被 pattern 拒的空串）与 `jsonschema.exceptions.SchemaError`（inspector 自身 `parameters` schema 畸形）两类**——只接 `ValidationError` 会让 `SchemaError` 裸泄漏出 loader（与 step 3 的 `InspectorError` 泄漏同类、致 CLI / doctor 崩）。**因复用同一 helper 且接同样的异常,loader 接受集与 runner 接受集相等**（非「⊆」)——杜绝「loader raw-validate 拒掉 runner 凭默认值会接受的 `required` 参数」这类方向反转的分歧。

> step 3/4/5 把「未注册 inspector」「给无参 inspector 传参」「typo 参数键」「非法值」「畸形 schema」从「运行期静默无效 / 次日触发才报 / 裸异常崩」提前到**加载期 `ConfigError` fail-loud**;runtime 仍有 `InspectorRunner.run` 的同 helper 校验作 defense-in-depth(覆盖绕过 loader 直接构造 manifest 的路径,见 deterministic-inspection-mode 能力)。

#### 场景:agent 模式带非空 inspector_parameters 被拒
- **当** `mode: agent`（或省略 mode）的 manifest 写了非空 `inspector_parameters`,执行加载
- **那么** 加载必须 fail-loud 拒绝,消息指出 inspector_parameters 仅 deterministic 生效

#### 场景:deterministic 模式 key 不在默认健康集被拒
- **当** `mode: deterministic`、无 `inspectors:`（用默认健康集）的 manifest 写 `inspector_parameters: {mysql.deadlocks: {...}}`（`mysql.deadlocks` 不在默认健康集),执行加载
- **那么** 加载必须 fail-loud 拒绝,消息列出越界 key `mysql.deadlocks`

#### 场景:deterministic 模式显式 inspectors 集权威、key 不在其中被拒
- **当** `mode: deterministic`、`inspectors: [linux.disk.usage]` 的 manifest 写 `inspector_parameters: {net.listening_ports: {...}}`（`net.listening_ports` 不在该显式集,即便它是已注册 inspector),执行加载
- **那么** 加载必须 fail-loud 拒绝(显式集是权威集、不与默认集取并),消息列出越界 key `net.listening_ports`

#### 场景:deterministic 模式显式 inspectors 含未注册名作为 param key 报 ConfigError 而非崩
- **当** `mode: deterministic`、`inspectors: [net.listening_prots]`（拼错、未注册）的 manifest 写 `inspector_parameters: {net.listening_prots: {...}}`,执行加载
- **那么** 加载必须以 **`ConfigError`**（含文件名 + key）fail-loud——**禁止**让 `registry.get` 的 `InspectorError` 裸抛出（`schedule` CLI / `doctor` 的 except 不接它会崩）

#### 场景:deterministic 模式给无参 inspector 传参被拒
- **当** `mode: deterministic` 的 manifest 写 `inspector_parameters: {linux.disk.usage: {x: 1}}`（`linux.disk.usage` 在运行集内但其 manifest 无 `parameters:` block）,执行加载
- **那么** 加载必须 fail-loud 拒绝,消息指出该 inspector 不接受参数

#### 场景:deterministic 模式参数键 typo 在加载期被拒
- **当** `mode: deterministic` 的 manifest 写 `inspector_parameters: {net.listening_ports: {allowed_procesess: [x]}}`（参数键拼错,该 inspector `parameters` 是 `additionalProperties:false`）,执行加载
- **那么** 加载必须 fail-loud 拒绝(jsonschema 校验失败),消息含未知键——不拖到触发时才以 exception 状态暴露

#### 场景:deterministic 模式 key 在运行集且参数合法正常加载
- **当** `mode: deterministic`、无 `inspectors:` 的 manifest 写 `inspector_parameters: {net.listening_ports: {allowed_processes: [derper]}}`（`net.listening_ports` 在默认健康集、参数合法）,执行加载
- **那么** 必须成功加载
