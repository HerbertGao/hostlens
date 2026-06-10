# inspector-plugin-system 规范

## 目的

定义 Hostlens Inspector 插件体系（M1 落地范围）：`InspectorManifest` Pydantic schema、loader（含 Jinja2 AST 静态校验 / shell 注入防御三件套 / ReDoS 6 类静态拒绝）、4 种 parse format（raw / table / json / kv，不含 sql_result）、Finding DSL 引擎（simpleeval + AST 静态 gate + 26 项 builtin deny-list）、`InspectorRegistry` 与 `RegistryBuildResult` 双值装配工厂、`InspectorRunner` 11 步固定求值顺序（含 runtime parameter validation + per-call-site precise exception scoping + 协作 cancellation 边界）、`InspectorError` 结构化 kind enum、CLI `hostlens inspectors list/show`、`hostlens doctor` `inspectors` section、`Settings.inspectors_search_paths`、2 个 builtin inspector（hello.echo / system.uptime）。本规范不含 hook.py / sampling_window / artifacts / sql_result（留给后续 milestone）。
## 需求
### 需求:`InspectorManifest` Pydantic 模型必须严格 conform M1 字段集

`hostlens.inspectors.schema.InspectorManifest` 必须是 Pydantic v2 模型，含**恰好**以下顶层字段（不多不少；`model_config = ConfigDict(extra="forbid", frozen=True)`）：

- 标识：
  - `name: str`：全局唯一；正则 `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$`（点分命名空间，如 `linux.cpu.top_processes`；**强制最少两段**——`.` 之前与之后都必须有内容；以小写字母开头）
  - `version: str`：SemVer 字符串；正则 `^\d+\.\d+\.\d+$`（M1 阶段强制 SemVer，与 ToolSpec.version 的 opaque 语义不同——manifest 是用户写的，强 SemVer 帮助 schema 兼容判定）
  - `description: str`：min_length=1
- 兼容性：
  - `tags: list[str] = []`：用于 `inspectors list --tag` 筛选；每个 tag 匹配 `^[a-z][a-z0-9_-]*$`
  - `targets: list[Literal["local", "ssh", "docker", "k8s"]]`：取值域**恰好** `local` / `ssh` / `docker` / `k8s`；至少 1 个、可任意组合；与 `ExecutionTarget.type` Literal 锁定全集对齐（取值域至此收口）。`docker` 由「放开 Inspector 的 Docker target 支持」提案放开（DockerTarget 已落地，见 `docker-execution-target` spec）；`k8s` 由「放开 Inspector 的 K8s target 支持」提案放开（KubernetesTarget 已落地，见 `kubernetes-execution-target` spec）；任意其他字符串（`kubernetes` / `replay` 等）仍必须被拒。声明 `docker` / `k8s` 的 inspector 必须满足 `inspector-authoring-contract` spec §需求:容器适用性 的容器语义判据（含 docker⇔k8s 奇偶约束）。
  - `requires_capabilities: list[str] = []`：值必须在 `Capability` Enum 的小写 value 集合（M1 = `{"shell", "file_read", "ssh", "systemd", "docker_cli"}`）内；loader 校验未知值 raise
  - `requires_binaries: list[str] = []`：每个 binary 名匹配 `^[a-zA-Z0-9._-]+$`（防注入）
  - `requires_files: list[str] = []`：每个路径必须匹配严格正则 `^/[A-Za-z0-9._/-]+$`（POSIX 绝对路径 + 严格 ASCII 字符集，**禁止** shell 元字符 `; $ \` ( ) | & < > \n \0` 等）**且**在 Pydantic `field_validator` 中做 path-component 级二次校验：拆分 `path.split("/")` 后任何 component 等于 `"."` 或 `".."` → raise（防止父目录穿越，保证路径是规范化的绝对路径）；理由：runner preflight 探测时会把 path 拼到 `[ -r <path> ]` shell 求值，宽松字符集会构成命令注入向量；防御纵深由 (a) 此字段级正则在 manifest 加载时拒绝 + (b) component 级 `..` 拒绝 + (c) runner 在拼 shell 命令前**仍**用 `shlex.quote(path)` 包路径**三重保证**。**已知接受风险**（manifest 作者责任，不在 loader 范围）：路径仍可能指向 `/proc/self/mem` 等伪文件系统、`/dev/...` 字符设备等敏感位置——M1 不做白名单 prefix 检查（无业务必要、增加误判面）；docs/operations/inspectors.md 中记为「已知接受风险」
  - `privilege: Literal["none", "sudo", "root"] = "none"`：M1 runner 对 `privilege != "none"` 在未 `allow_privileged` opt-in 时返回 `requires_unmet`；M1 范围**不**实现 sudo 调用集成
- 参数化：
  - `parameters: dict[str, Any] | None = None`：JSON Schema dict；如非 None 必须 conform JSON Schema draft 2020-12；`type: object` 顶层（其他顶层类型 reject）
  - `secrets: list[str] = []`：每个 secret 名匹配 `^[A-Z_][A-Z0-9_]*$`（POSIX env var 命名）
- 采集：
  - `collect: CollectSpec`：嵌套模型；详见下一需求块
- 解析：
  - `parse: ParseSpec`：嵌套模型；详见下一需求块
- 输出与判定：
  - `output_schema: dict[str, Any]`：JSON Schema dict；`type: object` 顶层；非 None
  - `findings: list[FindingRule]`：可空（空列表表示该 Inspector 只采集不判定，仅返回 output）

**M1 范围禁用字段（出现 → loader raise `manifest_validation_error`）**：`hook` / `sampling_window` / `artifacts` / 任何 manifest 顶层未列出的字段。

#### 场景:Manifest 字段集严格

- **当** 用 `extra` 字段（如 `priority: high`）的 yaml 加载 manifest
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 `extra fields not permitted` + 字段名

#### 场景:name 正则强制点分命名

- **当** 试图加载 `name: simple_name` 的 manifest（缺少点分）
- **那么** 必须 raise `pydantic.ValidationError`，错误信息含 name 正则

#### 场景:name 接受多级点分

- **当** 加载 `name: linux.cpu.top_processes` 的 manifest
- **那么** 必须成功

#### 场景:version 强制 SemVer

- **当** 试图加载 `version: latest` 或 `version: 1` 或 `version: v1.0` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:targets 必须非空且仅含允许值

- **当** 试图加载 `targets: []`（空）或 `targets: [kubernetes]` 或 `targets: [replay]` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`（空列表违反 `min_length=1`；`kubernetes` / `replay` 不在 Literal 取值域内——取值域恰好为 `ExecutionTarget.type` 全集 `local` / `ssh` / `docker` / `k8s`）

#### 场景:targets 接受 docker

- **当** 加载 `targets: [docker]` 或 `targets: [local, docker]` 或 `targets: [local, ssh, docker]` 的 manifest
- **那么** 必须成功（`docker` 已在 Literal 取值域内；DockerTarget 已实现）

#### 场景:targets 接受 k8s

- **当** 加载 `targets: [k8s]` 或 `targets: [local, ssh, docker, k8s]` 的 manifest
- **那么** 必须成功（`k8s` 已在 Literal 取值域内；KubernetesTarget 已实现，见 `kubernetes-execution-target` spec）

#### 场景:requires_files 含 shell 元字符被拒绝

- **当** 试图加载 `requires_files: ["/tmp/x; curl evil.com"]` 或 `requires_files: ["/etc/$(whoami)"]` 或 `requires_files: ["/path with space"]` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`（字段级正则 `^/[A-Za-z0-9._/-]+$` 在 manifest 加载阶段拒绝；防御纵深的第一道闸）

#### 场景:requires_files 含 NUL 字节被拒绝

- **当** 试图加载 `requires_files: ["/tmp/x  "]`（含 NUL 字节）
- **那么** 必须 raise `pydantic.ValidationError`（正则字符集已限定 ASCII alphanumeric + `._/-`，NUL 不在其中）

#### 场景:requires_files 含 .. 父目录穿越被拒绝

- **当** 试图加载 `requires_files: ["/etc/../passwd"]` 或 `requires_files: ["/a/b/../c"]`
- **那么** 必须 raise `pydantic.ValidationError`（component 级 `..` 校验拒绝；防穿越是 manifest 安全契约的一部分）

#### 场景:requires_files 含 . 单点 component 被拒绝

- **当** 试图加载 `requires_files: ["/etc/./passwd"]`
- **那么** 必须 raise `pydantic.ValidationError`（要求路径已规范化）

#### 场景:M1 禁用字段被拒绝

- **当** 试图加载含顶层 `hook: hook.py` 或 `artifacts: [...]` 或 `collect.sampling_window: ...` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`（与 extra="forbid" 行为一致）

#### 场景:Manifest 实例不可变

- **当** 已加载的 `manifest` 试图赋值 `manifest.name = "x"`
- **那么** 必须 raise `pydantic.ValidationError`（frozen=True）

### 需求:`CollectSpec` 与 `ParseSpec` 嵌套模型字段集

`hostlens.inspectors.schema.CollectSpec` 必须含**恰好**以下字段：

- `command: str`：min_length=1；shell-evaluated；Jinja2 模板（参数/secrets 渲染规则见下方需求）
- `timeout_seconds: int = 60`：默认 60；下限 1，上限 = `Settings.concurrency.inspector_timeout_seconds_max`（默认 300）；超出 loader raise

`hostlens.inspectors.schema.ParseSpec` 必须含**恰好**以下字段：

- `format: Literal["raw", "table", "json", "kv"]`：M1 **恰好**这 4 种；写 `sql_result` 或其他值 loader raise
- `columns: list[str] = []`：`format == "table"` 时必填且非空；`format == "raw"` 且 `raw_extract_regex` 非 None 时必填（用于命名捕获组映射）；其他 format 必须为空
- `delimiter: str = "="`：`format == "kv"` 时使用；其他 format 必须保持默认（loader 校验）
- `skip_header_rows: int = 1`：`format == "table"` 时使用；下限 0；其他 format 必须保持默认（loader 校验）
- `raw_extract_regex: str | None = None`：仅 `format == "raw"` 时允许非 None；loader 必须执行**四层静态闸**（任一失败时，**在 Pydantic v2 `model_validator` 内 raise `ValueError(f"<tag>: <detail>")`**——Pydantic v2 会自动捕获并构造最终的 `pydantic.ValidationError`，测试断言 `ValidationError.errors()[0]["msg"]` 含命中的 tag 字符串；**禁止**在 validator 内直接 raise `ValidationError`，那不是 Pydantic v2 文档推荐路径，可能在未来 minor version 失效；**所有四层都在加载时**完成，runner 不依赖任何运行时 timeout 作为主防御）：
  1. 字符串长度 ≤ 200（防止用户写超长正则增加 ReDoS 攻击面）
  2. `re.compile()` 必须成功
  3. 所有捕获组都是**命名**组（无匿名 `()` 组）；命名组数 == `len(columns)`
  4. **ReDoS 静态拒绝**：用 `sre_parse.parse(regex)` 拿 AST 并 walk 所有节点，拒绝以下 known-bad 模式（一个不漏；M1 范围严格名单）：
     - 嵌套量词：外层 `MAX_REPEAT` / `MIN_REPEAT` 节点的子树中含**任何**另一个 `MAX_REPEAT` / `MIN_REPEAT` / `POSSESSIVE_REPEAT`（Python 3.11+ 新增）节点——覆盖 `(<sub>+)+` / `(<sub>*)*` / `(?:a+)+` 等所有等价形式
     - 量词作用于 `ASSERT` / `ASSERT_NOT`（lookahead / lookbehind）节点：拒绝 `(?=...)+` / `(?!...)+` 等
     - **任何 GROUPREF 节点**（命名或编号 backreference）：拒绝 `(.+)\1+` / `(?P<x>.+)(?P=x)+` 等——backreference 是 ReDoS 高危且 M1 范围无业务需要，直接整体禁用
     - **任何 ATOMIC_GROUP 节点**（Python 3.11+ `(?>...)`）：拒绝
     - alternation 分支重叠 / 一支是另一支的前缀：扫所有 `BRANCH` 节点，比较每对分支的字面量前缀（`LITERAL` 序列）；若任一对存在 `branch_a` 的字面量前缀完全等于 `branch_b` 的字面量序列（或反之）→ 拒绝。覆盖 `(a\|a)+` / `(a\|aa)+` / `(a\|ab)+` 等。**精确算法**：对每个 BRANCH 节点 `b` 抽出其前缀 LITERAL 序列 `prefix(b)`；若 `∃ b1, b2 ∈ b.branches, b1 != b2: prefix(b1).startswith(prefix(b2)) or prefix(b2).startswith(prefix(b1))` → 拒绝
     - 量词作用于"可匹配空串的子模式"：扫 `MAX_REPEAT` / `MIN_REPEAT` 的子树，若子树是 `BRANCH` 含空分支、或 `MAX_REPEAT(min=0)` 的子树、或纯 `ASSERT` → 拒绝（覆盖 `(a?)*` / `(a*)+` / `((?=x))*` 等）
   - 实现：M1 实现统一用 `sre_parse.parse` + 自定义 `_walk_redos(ast)` 函数；**禁止**用 regex 字面扫描（漏判风险高）；命中 raise `pydantic.ValidationError` 含 known-bad 模式标签
   - **runner 仍可调用 `asyncio.wait_for(asyncio.to_thread(re.search), timeout=1.0)`，但仅作为"软兜底"日志事件；ReDoS 防御不依赖此 timeout 生效**（Python `re` 在 C 层回溯无法被 asyncio / signal 在非主线程可靠中断；这是诚实的限制，写入 design.md 风险/权衡）

两个嵌套模型都设置 `model_config = ConfigDict(extra="forbid", frozen=True)`。

#### 场景:CollectSpec timeout 上限

- **当** 试图加载 `collect: { command: "echo", timeout_seconds: 9999 }` 的 manifest
- **那么** 必须 raise `pydantic.ValidationError`（上限超出）

#### 场景:ParseSpec table 必须声明 columns

- **当** 试图加载 `parse: { format: table }`（缺 columns）的 manifest
- **那么** 必须 raise `pydantic.ValidationError`，错误指明 columns 必填

#### 场景:ParseSpec raw 模式不允许 columns（除非用 raw_extract_regex）

- **当** 加载 `parse: { format: raw, columns: [x, y] }` **且** `raw_extract_regex` 为 None
- **那么** 必须 raise `pydantic.ValidationError`，错误指明 columns 与 raw_extract_regex 必须同时声明

#### 场景:ParseSpec raw_extract_regex 必须可编译

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x" }`（语法错）
- **那么** 必须 raise `pydantic.ValidationError`，错误含 `re.error` 消息

#### 场景:ParseSpec raw_extract_regex 必须全部命名组

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(\\d+) (?P<y>\\d+)", columns: [y] }`（含匿名捕获组）
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:ParseSpec raw_extract_regex 命名组数必须等于 columns 数

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<a>\\d+)", columns: [a, b] }`（捕获组少于 columns）
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:ParseSpec raw_extract_regex 超过 200 字符被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "<201 个字符的合法正则>" }`
- **那么** 必须 raise `pydantic.ValidationError`（长度上限闸）

**ReDoS 场景测试约定**：所有 ReDoS payload 必须**包成"合法命名组 + 配套 columns"结构**，确保 ReDoS 检测分支独立于 column/named-group 数量校验生效（否则 bare 模式会先 fail 其他校验）；每个场景的 `pydantic.ValidationError` 必须含命中的 ReDoS 模式标签（如 `nested_quantifier` / `groupref_forbidden` / `prefix_subset_alternation` 等）。

#### 场景:ParseSpec raw_extract_regex 嵌套量词被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>(a+)+)", columns: [x] }`（经典 ReDoS 模式）
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `nested_quantifier`

#### 场景:ParseSpec raw_extract_regex 量词作用于可匹配空串子模式被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>(a?)*)", columns: [x] }`
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `quantifier_on_empty_matchable`

#### 场景:ParseSpec raw_extract_regex 非捕获组嵌套量词被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>(?:a+)+)", columns: [x] }`
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `nested_quantifier`（外层 MAX_REPEAT 子树含内层 MAX_REPEAT）

#### 场景:ParseSpec raw_extract_regex 命名 backreference 被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>.+)(?P=x)+", columns: [x] }`
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `groupref_forbidden`（任何 GROUPREF 节点都拒绝）

#### 场景:ParseSpec raw_extract_regex 编号 backreference 被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>.+)\\1+", columns: [x] }`
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `groupref_forbidden`

#### 场景:ParseSpec raw_extract_regex lookahead 加量词被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>(?=a+)+a)", columns: [x] }`
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `quantifier_on_assert`（量词作用于 ASSERT 节点）

#### 场景:ParseSpec raw_extract_regex atomic group 被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>(?>a+))", columns: [x] }`（Python 3.11+ ATOMIC_GROUP）
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `atomic_group_forbidden`

#### 场景:ParseSpec raw_extract_regex 前缀子集 alternation 被拒绝

- **当** 加载 `parse: { format: raw, raw_extract_regex: "(?P<x>(a\|aa)+)", columns: [x] }`（`aa` 以 `a` 为前缀；NFA 状态可重叠）
- **那么** 必须 raise `pydantic.ValidationError`，errors[0].msg 含 `prefix_subset_alternation`

#### 场景:ParseSpec 非 table 不允许 skip_header_rows 非默认

- **当** 加载 `parse: { format: json, skip_header_rows: 2 }`
- **那么** 必须 raise `pydantic.ValidationError`

### 需求:`FindingRule` 四字段 DSL 字段集与静态校验

`hostlens.inspectors.schema.FindingRule` 必须含**恰好**以下字段：

- `for_each: str | None = None`：形如 `<iterable_expr> as <var_name>`；若声明，loader 必须用 regex `^(.+?)\s+as\s+([a-z_][a-z_0-9]*)$` 解析；`<iterable_expr>` 部分必须能被 `simpleeval` 编译（`simpleeval.SimpleEval().eval(expr)` 在空 context 下不抛 SyntaxError）；`<var_name>` 部分必须匹配 `^[a-z_][a-z_0-9]*$`
- `when: str`：必填；min_length=1；必须能被 simpleeval 编译为只读表达式（loader 静态校验）
- `severity: Literal["info", "warning", "critical"]`：必填；M1 三值集合
- `message: str`：必填；min_length=1；Python `.format()` 风格模板（`{var_name}` 或 `{var_name.attr}`）

`model_config = ConfigDict(extra="forbid", frozen=True)`。

**静态校验规则（loader 必须执行）**：

1. `when` 表达式静态编译失败 → raise `InspectorError(kind="finding_when_invalid", index=i, error=...)`
2. 聚合模式（`for_each is None`）下 `message` 引用 `<var_name>.<attr>` 形式的变量（无法在聚合上下文解析） → raise `InspectorError(kind="finding_message_invalid_aggregate_ref", index=i, var=...)`
3. 遍历模式下 `message` 引用了 `for_each` 声明之外的变量名 → 允许（可能引用 output 顶层 / parameters），runner 在求值时报 `simpleeval.NameNotDefined` 并 skip 该 finding rule

#### 场景:for_each 缺少 `as` 分隔符

- **当** 加载 `findings: [{ for_each: "processes p", when: "p.cpu > 70", severity: warning, message: "..." }]`
- **那么** 必须 raise `pydantic.ValidationError`，错误指明 for_each 必须含 `as <var>`

#### 场景:聚合模式 message 引用 for_each 变量被拒绝

- **当** 加载 `findings: [{ when: "len(processes) > 5", severity: info, message: "Found {p.command}" }]`（聚合模式但引用 `p`）
- **那么** 必须 raise `InspectorError(kind="finding_message_invalid_aggregate_ref")`

#### 场景:severity 必须在 3 值集合内

- **当** 加载 `severity: high` 或 `severity: error` 的 finding
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:when 语法错误

- **当** 加载 `when: "p.cpu > >"` 的 finding
- **那么** 必须 raise `InspectorError(kind="finding_when_invalid")`（loader 静态校验阶段）

### 需求:Manifest loader 必须 reject 未走 `sh` filter 的 string / array(string-items) parameter 引用

`hostlens.inspectors.loader.load_manifest(path)` 必须用 `jinja2.Environment().parse(source)` 解析 `collect.command` 模板的 AST，对其中每个 `jinja2.nodes.Name` 节点：

- 若 name 匹配 `parameters` schema 中**类型为 `string`** 的字段名 → 必须紧随 `| sh` filter（即 AST 上紧邻的 `nodes.Filter(name="sh")`）；否则 raise `InspectorError(kind="unquoted_parameter_in_command", path=path, parameter=name)`
- 若 name 匹配 `parameters` schema 中**类型为 `array`** 的字段名 → loader 必须分情况处理：
  - `items.type == "string"`：必须紧随 `| map('sh')` filter **接** `| join(<delim>)` filter（filter chain 顺序必须为 `map('sh')` 在 `join` 之前）；否则 raise `InspectorError(kind="unquoted_array_parameter_in_command", path=path, parameter=name)`
  - `items.type` 是 `integer` / `number` / `boolean`：不强制 filter chain（非 string 元素不构成 shell 注入向量）
  - **`items` 字段缺失 / `items.type` 缺失 / `items.type` 是 `object` 或 `array` / `items` 用 `oneOf/anyOf/allOf` 而无单一 `type`**：loader 必须**拒绝该 manifest** raise `InspectorError(kind="array_parameter_items_type_undetermined", path=path, parameter=name)`——这堵住"用户故意省略 items 让 loader 无法判定"的绕过路径；array parameter 必须显式声明 items.type，没有"默认放行"
- 例外：若 array 字段通过 `nodes.Getitem`（subscript 形式 `endpoints[0]`）拿到**单个 string 元素**后继续走 `| sh` → 接受（被 subscript 解引用后是 string 类型，按 string 规则处理）
- 例外：manifest 顶层声明 `unsafe_raw: true` **且** 该模板紧邻含 `{# unsafe_raw: <理由> #}` 注释 → 接受（M1 范围**不**支持，loader 直接 raise `InspectorError(kind="unsafe_raw_not_supported_in_m1")`；留给未来提案）
- 对**非 string / 非 array(string-items)** 类型（integer / number / boolean / array(non-string items)）的 parameter 引用：不强制 `| sh` filter（数值类型与非 string 元素的 array 不构成 shell 注入向量）

**Loader 必须用 AST 遍历（`jinja2.visitor.NodeVisitor`），不用 regex**（regex 漏判风险高，AST 是唯一可靠路径）；遍历必须穿透 `nodes.Filter` chain、`nodes.CondExpr`（三元）、`nodes.Concat`（字符串拼接）、`nodes.If` block 内引用、macro 调用——任何位置出现的 Name 节点都必须按上述规则校验。

**Jinja2 解析错误 wrap**：loader 调用 `jinja2.Environment().parse(source)` 时如果模板本身语法错误抛 `jinja2.TemplateSyntaxError` → loader 必须捕获并 wrap 成 `InspectorError(kind="command_template_invalid", path=path, line=err.lineno, message=err.message)`，**不**让 Jinja2 异常直接 propagate（保持 loader 的"统一抛 InspectorError"契约）。

#### 场景:string parameter 未走 sh filter 被拒绝

- **当** 加载 manifest，`parameters.host: { type: string, pattern: ... }` 且 `collect.command: "ping {{ host }}"`（未带 sh filter）
- **那么** 必须 raise `InspectorError(kind="unquoted_parameter_in_command", parameter="host")`

#### 场景:string parameter 走 sh filter 被接受

- **当** 加载 manifest，`parameters.host: { type: string, pattern: ... }` 且 `collect.command: "ping {{ host | sh }}"`
- **那么** 必须成功加载

#### 场景:整型 parameter 不强制 sh filter

- **当** 加载 manifest，`parameters.port: { type: integer }` 且 `collect.command: "psql -p {{ port }}"`（未带 sh filter）
- **那么** 必须成功加载

#### 场景:M1 不支持 unsafe_raw

- **当** 加载 manifest 顶层声明 `unsafe_raw: true`
- **那么** 必须 raise `InspectorError(kind="unsafe_raw_not_supported_in_m1")`

#### 场景:AST 遍历必须捕获 default filter 边角

- **当** 加载 `collect.command: "ping {{ host | default('') }}"`（用 default filter 但缺 sh filter）且 host 是 string 类型 parameter
- **那么** 必须 raise `InspectorError(kind="unquoted_parameter_in_command")`（AST 遍历必须穿透 filter chain）

#### 场景:array(string-items) parameter 未走 map('sh')|join 被拒绝

- **当** 加载 manifest，`parameters.endpoints: { type: array, items: { type: string, pattern: ... } }` 且 `collect.command: "ping {{ endpoints | join(' ') }}"`（缺 `map('sh')`）
- **那么** 必须 raise `InspectorError(kind="unquoted_array_parameter_in_command", parameter="endpoints")`

#### 场景:array(string-items) parameter 走 map('sh')|join 被接受

- **当** 加载 manifest，`parameters.endpoints: { type: array, items: { type: string, pattern: ... } }` 且 `collect.command: "ping {{ endpoints | map('sh') | join(' ') }}"`
- **那么** 必须成功加载

#### 场景:array(string-items) subscript 后单元素走 sh 被接受

- **当** 加载 manifest，`parameters.endpoints: { type: array, items: { type: string, pattern: ... } }` 且 `collect.command: "ping {{ endpoints[0] | sh }}"`
- **那么** 必须成功加载（subscript 解引用后元素是 string 类型，按 string 规则处理）

#### 场景:array(non-string items) 不强制 map('sh')

- **当** 加载 manifest，`parameters.ports: { type: array, items: { type: integer } }` 且 `collect.command: "ports={{ ports | join(',') }}"`（缺 `map('sh')`）
- **那么** 必须成功加载（integer 元素不构成 shell 注入向量）

#### 场景:array 缺失 items 声明被拒绝

- **当** 加载 manifest `parameters: { endpoints: { type: array } }`（无 items 声明）且 `collect.command: "ping {{ endpoints | join(' ') }}"`
- **那么** 必须 raise `InspectorError(kind="array_parameter_items_type_undetermined", parameter="endpoints")`（堵住"省略 items 绕过 sh-filter"路径）

#### 场景:array items.type 是 object 被拒绝

- **当** 加载 manifest `parameters: { endpoints: { type: array, items: { type: object, properties: {...} } } }`
- **那么** 必须 raise `InspectorError(kind="array_parameter_items_type_undetermined")`（object 元素插入 shell 后无确定字符集，必须显式拆分）

#### 场景:array items 用 oneOf/anyOf 多类型被拒绝

- **当** 加载 manifest `parameters: { x: { type: array, items: { oneOf: [{type: string}, {type: integer}] } } }`
- **那么** 必须 raise `InspectorError(kind="array_parameter_items_type_undetermined")`

#### 场景:array(string-items) filter chain 顺序错误被拒绝

- **当** 加载 `collect.command: "ping {{ endpoints | join(' ') | map('sh') }}"`（map 在 join 之后，相当于对整个 joined string 跑 map 而不是对元素跑）
- **那么** 必须 raise `InspectorError(kind="unquoted_array_parameter_in_command")`（filter chain 顺序要求 `map('sh')` 在 `join` 之前）

#### 场景:CondExpr 内引用必须穿透校验

- **当** 加载 `collect.command: "ping {{ host if host else 'localhost' }}"`（三元表达式内引用 host，无 sh filter）
- **那么** 必须 raise `InspectorError(kind="unquoted_parameter_in_command", parameter="host")`（AST 遍历必须穿透 CondExpr 节点）

### 需求:Manifest loader 必须 reject string parameter 缺少 `pattern` 或 `enum` 约束

`hostlens.inspectors.loader.load_manifest(path)` 在加载 `parameters` JSON Schema 时，对每个**会被用户值实际填充的 string 类型字段**必须校验包含 `pattern: <regex>` **或** `enum: [<allowed_values>]` 至少一个；包括：

- 顶层 `type: string` 字段（直接）
- `type: array` 且 `items.type == "string"` 字段（**items schema 同样必须含 pattern/enum**，因为数组元素也会进 shell 命令）
- nested object 内的 string 字段（递归校验）

二者都缺失 → raise `InspectorError(kind="parameter_missing_charset_constraint", path=path, parameter=name)`（数组元素失败时 parameter 字段值含路径，如 `endpoints.items`）。

理由：缺约束的 string（含 array items）字段是 shell 注入向量；强制约束让"manifest 写作阶段"就堵住注入。

#### 场景:string 字段缺 pattern/enum 被拒绝

- **当** 加载 manifest `parameters: { host: { type: string } }`（无 pattern / 无 enum）
- **那么** 必须 raise `InspectorError(kind="parameter_missing_charset_constraint", parameter="host")`

#### 场景:string 字段含 pattern 被接受

- **当** 加载 manifest `parameters: { host: { type: string, pattern: "^[a-zA-Z0-9.-]+$" } }`
- **那么** 必须成功

#### 场景:string 字段含 enum 被接受

- **当** 加载 manifest `parameters: { mode: { type: string, enum: [fast, slow] } }`
- **那么** 必须成功

#### 场景:非 string 字段不要求约束

- **当** 加载 manifest `parameters: { port: { type: integer } }`（无 minimum / maximum）
- **那么** 必须成功（仅 string / array(string-items) 类型强制约束）

#### 场景:array(string-items) items schema 缺 pattern/enum 被拒绝

- **当** 加载 manifest `parameters: { endpoints: { type: array, items: { type: string } } }`（items 无 pattern / 无 enum）
- **那么** 必须 raise `InspectorError(kind="parameter_missing_charset_constraint", parameter="endpoints.items")`

#### 场景:array(string-items) items schema 含 pattern 被接受

- **当** 加载 manifest `parameters: { endpoints: { type: array, items: { type: string, pattern: "^[a-zA-Z0-9.-]+:\\d+$" } } }`
- **那么** 必须成功

### 需求:Manifest loader 必须 reject secret 出现在 `collect.command` 模板插值位置

`hostlens.inspectors.loader.load_manifest(path)` 在 AST 遍历阶段，必须检查 `collect.command` 模板的所有 `jinja2.nodes.Name` 节点：

- 若 name 出现在 `secrets:` 声明列表 → raise `InspectorError(kind="secret_inlined_in_command", path=path, secret=name)`
- 若 secret name 出现在 `nodes.Getitem`（subscript 形式，如 `vars[PGPASSWORD]` 或 `env["PGPASSWORD"]`） → raise 同上
- 命令中通过 `$PGPASSWORD` 字面量引用 secret（shell 求值时由 sshd / shell 替换）→ 接受（**不**触发 loader 校验，因为这不是 Jinja2 插值）

理由：Jinja2 插值会让 secret 进入 cmd string，落入 process list / shell history / 错误日志栈帧。

#### 场景:secret 出现在 Jinja2 直接引用被拒绝

- **当** 加载 manifest `secrets: [PGPASSWORD]` 且 `collect.command: "psql -W {{ PGPASSWORD }}"`
- **那么** 必须 raise `InspectorError(kind="secret_inlined_in_command", secret="PGPASSWORD")`

#### 场景:secret 通过 shell `$VAR` 引用被接受

- **当** 加载 manifest `secrets: [PGPASSWORD]` 且 `collect.command: "PGPASSWORD=$PGPASSWORD psql ..."`（**无** Jinja2 插值）
- **那么** 必须成功加载（runner 后续通过 env 注入 PGPASSWORD）

#### 场景:secret 通过 subscript 形式引用被拒绝

- **当** 加载 manifest `secrets: [PGPASSWORD]` 且 `collect.command: "psql {{ env['PGPASSWORD'] }}"`
- **那么** 必须 raise `InspectorError(kind="secret_inlined_in_command")`

### 需求:Manifest loader 必须用 `yaml.safe_load` 且 reject 超大文件

`hostlens.inspectors.loader.load_manifest(path)` 实现必须：

- 调用 `path.stat().st_size > 262144`（256 KB）时 raise `InspectorError(kind="manifest_too_large", path=path, size=size)`，**不**进入解析
- 用 `yaml.safe_load(path.read_text())` 解析；**禁止**用 `yaml.load(...)` 走 default Loader（可构造任意 Python 对象 → RCE）
- 解析后立即 `InspectorManifest.model_validate(data)`；Pydantic 错误统一包成 `InspectorError(kind="manifest_validation_error", path=path, errors=...)`
- **任何 `yaml.YAMLError` 子类**（含 `ConstructorError` / `ScannerError` / `ParserError` / `ComposerError` 等）→ 统一 catch 并 wrap 成 `InspectorError(kind="manifest_parse_error", path=path, line=line, column=column, original=yaml_err)`；errors 中 line/column 字段来自 `yaml.MarkedYAMLError.problem_mark`（若 `original` 是 MarkedYAMLError 子类）；**禁止**让任何 `yaml.YAMLError` 子类直接 propagate 出 loader（保持 loader "统一抛 InspectorError" 契约）

#### 场景:文件超过 256KB 被拒

- **当** 加载 280 KB 的 manifest 文件
- **那么** 必须 raise `InspectorError(kind="manifest_too_large")`，不进入 yaml 解析

#### 场景:必须用 safe_load 而非 load

- **当** 加载含 `!!python/object/apply:os.system [whoami]` 的 yaml 文件
- **那么** 必须 raise `InspectorError(kind="manifest_parse_error", original=<yaml.constructor.ConstructorError instance>)`；safe_load 内部抛 `ConstructorError`，loader 必须 wrap 而非直接 propagate（与本需求块"任何 yaml.YAMLError 子类统一 wrap"契约一致）

#### 场景:YAML 语法错误含 line/column

- **当** 加载 yaml 文件含 `name: [unclosed`
- **那么** 必须 raise `InspectorError(kind="manifest_parse_error")`，extra 中含 `line` 与 `column`

### 需求:`InspectorRegistry` API 必须支持注册 / 查询 / 列表 / summary 投影

`hostlens.inspectors.registry.InspectorRegistry` 必须是普通 class（**不**是 Pydantic 模型；状态可变才能 register），提供以下 API：

- `register(manifest: InspectorManifest) -> None`：注册一个 manifest；name 冲突 raise `InspectorError(kind="duplicate_inspector", inspector=name, existing_path=..., new_path=...)`
- `get(name: str) -> InspectorManifest`：按 name 查；未找到 raise `InspectorError(kind="inspector_not_found", inspector=name)`
- `names() -> list[str]`：返回所有已注册 inspector name 列表，**按字典序**排序
- `list() -> list[InspectorManifest]`：返回所有已注册 manifest，**按 name 字典序**
- `list_summaries() -> list[InspectorSummary]`：返回 `hostlens.tools.schemas.list_inspectors.InspectorSummary` 列表（M2 已锁定 schema），按 name 字典序；投影规则：
  - `name` ← `manifest.name`
  - `version` ← `manifest.version`
  - `description` ← `manifest.description`
  - `tags` ← `sorted(manifest.tags)`（字典序，保证 prompt cache key 稳定）
  - `compatible_target_kinds` ← `sorted(manifest.targets)`（字典序）

#### 场景:duplicate_inspector 冲突 raise

- **当** 注册同名 manifest 两次
- **那么** 第二次 register 必须 raise `InspectorError(kind="duplicate_inspector")`

#### 场景:names 与 list 返回字典序

- **当** 按 `b`, `a`, `c` 顺序 register 3 个 manifest
- **那么** `names()` 必须返回 `["a", "b", "c"]`；`list()[i].name` 必须按相同顺序

#### 场景:list_summaries 投影完整

- **当** registry 含 1 个 manifest `name=linux.cpu, version=1.0.0, description="CPU check", tags=[cpu, linux], targets=[ssh, local]`
- **那么** `list_summaries()[0]` 必须等于 `InspectorSummary(name="linux.cpu", version="1.0.0", description="CPU check", tags=["cpu", "linux"], compatible_target_kinds=["local", "ssh"])`（tags 与 targets 都按字典序）

#### 场景:get 未找到 raise

- **当** 调用 `registry.get("does.not.exist")`
- **那么** 必须 raise `InspectorError(kind="inspector_not_found")`

### 需求:`build_registry_from_search_paths` 必须返回 `(registry, errors)` 双值、按规则区分 fatal vs collectable 错误

`hostlens.inspectors.registry.RegistryBuildResult` 必须是 `@dataclass(frozen=True)` 含字段：

- `registry: InspectorRegistry`：已成功加载的 inspector 装配后的 registry
- `errors: list[RegistryLoadError]`：所有文件级加载失败的清单（**不**抛出，让调用方决定 exit code）

`hostlens.inspectors.registry.RegistryLoadError` 必须是 `@dataclass(frozen=True)` 含字段：

- `path: Path`：失败文件路径
- `kind: str`：错误 kind（来自 `InspectorError.kind`）
- `detail: str`：错误简短描述（来自 `str(InspectorError)`）

`hostlens.inspectors.registry.build_registry_from_search_paths(user_paths: list[Path], *, settings: Settings) -> RegistryBuildResult` 必须：

1. 先扫 builtin 路径（**hardcode** `Path(hostlens.inspectors.__file__).parent / "builtin"`，**禁止**从 settings 读 builtin 路径）；按 `**/*.yaml` 递归找所有 manifest
2. 再扫 `user_paths`（每个 path `**/*.yaml` 递归）
3. 加载顺序确定：builtin 优先 alphabetical；用户路径按传入顺序遍历，每个路径内部 alphabetical
4. **错误分级**：
   - **Fatal 错误**（必须 raise，不进 errors 列表）：duplicate_inspector（builtin vs builtin / 用户 vs builtin / 用户 vs 用户）—— 这是 SECURITY 关键，silent collect 会让攻击者放同名 manifest 后无感知；**禁止** silent skip / collect
   - **Collectable 错误**（catch + 累积到 `errors` 列表，**不** raise）：单文件的 `manifest_parse_error` / `manifest_validation_error` / `manifest_too_large` / `unquoted_parameter_in_command` / `unquoted_array_parameter_in_command` / `array_parameter_items_type_undetermined` / `parameter_missing_charset_constraint` / `secret_inlined_in_command` / `unsafe_raw_not_supported_in_m1` / `command_template_invalid` / `finding_when_invalid` / `finding_message_invalid_aggregate_ref` / `parse_json_not_object` —— 这些是单文件失败，**不应**阻塞其他正常 inspector 加载；调用方（CLI / doctor）从 `errors` 决定是否 exit 1
5. **builtin 路径的文件级错误**：**仍然 raise** 而非 collect——builtin 是仓库自带，文件级错误意味着发布出 bug，必须立即暴露；只有**用户**路径的文件级错误才 collect

**禁止**任何"用户路径覆盖 builtin"的开关（如 `allow_builtin_override=True`）；攻击场景见 design.md 决策 6。

#### 场景:builtin 路径 hardcode

- **当** 调用 `result = build_registry_from_search_paths([], settings=Settings())`（空用户路径列表）
- **那么** `result.registry` 必须含 builtin 目录下所有 manifest（M1 至少含 `hello.echo` 与 `system.uptime`）；`result.errors == []`

#### 场景:用户 manifest 同名 builtin 被拒绝 (fatal raise)

- **当** 用户路径下存在 `system.uptime.yaml`（与 builtin `system.uptime` 同名）
- **那么** `build_registry_from_search_paths([user_path], settings=Settings())` 必须 raise `InspectorError(kind="duplicate_inspector")`（**fatal**，不进 errors 列表；理由：security-critical，silent collect 会让攻击者放注入 manifest 后用户无感知）

#### 场景:用户路径间冲突也被 raise

- **当** 用户路径 A 与 B 各含同名 manifest
- **那么** 必须 raise `InspectorError(kind="duplicate_inspector")`

#### 场景:用户路径单文件 parse 错收集到 errors 不阻塞其他

- **当** 用户路径含 1 个语法错的 yaml（如 `name: [unclosed`）+ 2 个正常 manifest
- **那么** `result.registry` 必须含 2 个正常 manifest + builtin；`result.errors` 必须含 1 项 `RegistryLoadError(path=<bad yaml>, kind="manifest_parse_error", detail=...)`；**不** raise

#### 场景:builtin 路径文件级错误必须 raise

- **当** builtin 路径下含 1 个语法错的 yaml（仓库 bug 场景）
- **那么** 必须 raise `InspectorError(kind="manifest_parse_error", path=<builtin yaml>)`（builtin 错误是 fatal，必须立即暴露，**不** collect）

### 需求:`InspectorRunner.run` 必须永远返回 `InspectorResult` 不抛业务异常

`hostlens.inspectors.runner.InspectorRunner` 必须提供：

- `__init__(self, target_registry: TargetRegistry, *, settings: Settings, logger: BoundLogger)`：依赖注入
- `async def run(self, manifest: InspectorManifest, target: ExecutionTarget, parameters: dict[str, Any] | None = None, *, allow_privileged: bool = False, cancel: asyncio.Event | None = None) -> InspectorResult`

**契约（按调用层 scope，不按异常类全局分类）**：

runner 在每个**业务调用点**用**精确的 except 列表**只捕获该调用点合法可能抛的异常类型，并就地转 status / skip finding；runner 自身代码路径上的异常自然 propagate 暴露 bug。**禁止**在 runner 顶层写 bare `except Exception` 或全局 catch `AttributeError` / `KeyError` / `TypeError`——这些异常的处理必须在引发它们的具体调用点决定。

具体映射（每个调用点的合法 except 列表）：

| 调用点 | 合法 except 列表 | 命中后行为 |
|---|---|---|
| `target.exec(...)` | `TargetError` | 整 inspector status → `target_unreachable`，error=err.kind |
| `target.exec` 返回 `ExecResult(timed_out=True)` | — （通过返回值判断，不是异常）| status → `timeout`，duration_seconds 来自 ExecResult |
| Jinja2 `template.render(...)` | `jinja2.UndefinedError` / `jinja2.TemplateError` | status → `exception`，error="render_failed: ..." |
| `_parse_<format>(stdout, spec)` | `json.JSONDecodeError` / `InspectorError(kind="parse_json_not_object")` | status → `exception`，error="parse_failed: ..." |
| `jsonschema.validate(output, schema)` | `jsonschema.ValidationError` | status → `exception`，error="output_schema_mismatch: ..." |
| `dsl.evaluate(<expr>, ctx)`（runner-internal） | `simpleeval.InvalidExpression` / `simpleeval.FeatureNotAvailable` / `simpleeval.NameNotDefined` / `simpleeval.NumberTooHigh` / `simpleeval.WrongType` / `simpleeval.IterableTooLong` / `asyncio.TimeoutError` | 当前 finding rule skip + warning log（**不**影响其他 rule，整体 status 仍可能 `ok`） |
| `format_message(template, ctx)`（`template.format(**ctx)` 调用） | `KeyError` / `IndexError` / `AttributeError` | 当前 finding rule skip + warning log（**这是唯一允许 catch `KeyError`/`AttributeError` 的调用点**，因为它们是用户 manifest 写错变量名的合法表达） |
| preflight：`target.exec("command -v <bin>", ...)` 等探测 | `TargetError` | status → `target_unreachable` |
| 调用方传非法参数（`manifest is None` / `target is None`） | — | runner **重新 raise `ValueError`**（这是编程错误，让调用方修） |

**任何不在上述任一调用点 except 列表中的异常 propagate** —— 包括 runner 自身内部 Pydantic 模型访问的 `AttributeError`、registry 查询的 `KeyError`、类型转换的 `TypeError`、`asyncio.CancelledError` 等；这些**必须**作为 runner 自身 bug 暴露在 stack trace 中。

#### 场景:TargetError 转 target_unreachable

- **当** 调用 `runner.run(manifest, target, ...)` 其中 `target.exec` 抛 `TargetError(kind="ssh_connection_lost")`
- **那么** 返回的 `InspectorResult.status == "target_unreachable"`、`error == "ssh_connection_lost"`、**不**抛异常

#### 场景:DSL 求值异常 skip 当前 finding rule

- **当** manifest 有 2 个 finding rule，rule[0] 的 `when` 表达式求值时抛 `simpleeval.NameNotDefined`，rule[1] 正常
- **那么** 返回的 `InspectorResult.status == "ok"`、`findings` 仅含 rule[1] 产生的 finding（rule[0] skip + warning log）

#### 场景:runner 自身 bug 重新 raise

- **当** runner 内部访问 `manifest.collect.command` 时（runner 编程错误访问了不存在的属性如 `manifest.nonexistent.attr`）触发 `AttributeError`
- **那么** 必须**重新 raise `AttributeError`**，**不**被任何 except 子句吞掉转 `status="exception"`（理由：该 `AttributeError` 不在任何业务调用点的 except 列表中）

#### 场景:format_message 的 KeyError 触发 finding rule skip 而非整体失败

- **当** finding rule 的 message template 是 `"hello {nonexistent_var}"`，runner 在 `format_message(template, ctx)` 调用处触发 `KeyError("nonexistent_var")`
- **那么** 该 finding rule **skip + warning log**，整体 status 仍可能为 `ok`；**不**抛 `KeyError`（业务调用点 `format_message` 允许 catch `KeyError`）

#### 场景:registry 查询的 KeyError 不被吞

- **当** runner 内部因为 bug 写出 `ctx.target_registry._internal_dict["does_not_exist"]` 触发 `KeyError`
- **那么** 必须**重新 raise `KeyError`**（这是 runner 自身访问 registry 私有结构的编程错误，不在任何业务调用点的 except 列表中）

#### 场景:非法参数 raise ValueError

- **当** 调用 `runner.run(manifest, target=None, ...)`
- **那么** 必须 raise `ValueError`

### 需求:`InspectorRunner` 求值顺序必须固定为 preflight → render → exec → parse → schema → findings

`InspectorRunner.run(...)` 内部步骤必须按以下顺序执行；任一步骤命中下表中的失败条件 → 立即返回对应 `InspectorStatus` + error 信息，**不**进入后续步骤：

| # | 步骤 | 失败条件 | 返回 status |
|---|---|---|---|
| 1 | preflight: target type 兼容 | `target.type not in manifest.targets` | `requires_unmet`，missing=`["target_type"]` |
| 2 | preflight: capabilities | `set(manifest.requires_capabilities) - set(c.value for c in target.capabilities)` 非空 | `requires_unmet`，missing=`[cap1, cap2, ...]` |
| 3 | preflight: privilege | `manifest.privilege != "none"` 且 `allow_privileged is False` | `requires_unmet`，missing=`["privilege_opt_in"]` |
| 4 | preflight: env secrets | `manifest.secrets` 中任一 env var 不在 `os.environ` | `requires_unmet`，missing=`["env:VAR_NAME"]` |
| 5 | preflight: binaries | 任一 `requires_binaries` 在 target 上探测失败（`command -v <bin>` 退码非 0） | `requires_unmet`，missing=`["bin:nginx"]` |
| 6 | preflight: files | 任一 `requires_files` 在 target 上 `[ -r $(printf %s <quoted_path>) ]` 退码非 0；**runner 必须用 `shlex.quote(path)` 包路径再拼 shell 命令**（虽然字段层正则已限定字符集为防御纵深仍要做） | `requires_unmet`，missing=`["file:/etc/nginx/nginx.conf"]` |
| 7a | **runtime parameter validation**: `jsonschema.validate(parameters or {}, manifest.parameters)`（当 `manifest.parameters` 非 None） | `jsonschema.ValidationError`（值不符合声明类型/约束——防御 type confusion 注入：caller 传 string `"5432; rm -rf /"` 而 manifest 声明 integer 时被拦） | `exception`，error="parameter_validation_failed: ..." |
| 7a | 同上 | `jsonschema.SchemaError`（manifest schema 本身在 loader 已 check，但 `model_construct` 可绕过 loader；本步骤是 defense-in-depth） | `exception`，error="parameter_schema_invalid: ..." |
| 7 | render: Jinja2 渲染 `collect.command`（含 sh filter / secrets env 准备） | `jinja2.UndefinedError` 等渲染错 | `exception`，error="render_failed: ..." |
| 8 | exec: `target.exec(rendered_cmd, timeout=..., env=secrets_env)` | `ExecResult.timed_out is True` | `timeout` |
| 8 | exec | `TargetError` 抛出 | `target_unreachable`，error=err.kind |
| 9 | parse: 按 `parse.format` 调对应 parser | parser 抛异常 | `exception`，error="parse_failed: ..." |
| 10 | schema 校验: `jsonschema.validate(parsed_output, manifest.output_schema)` | `jsonschema.ValidationError` | `exception`，error="output_schema_mismatch: ..." |
| 11 | findings: 遍历 manifest.findings，按 DSL 引擎求值 | 单 rule 求值异常 | rule skip + warning log（**不**让整体失败） |
| 终 | 所有步骤成功 | — | `ok`，含 output + findings |

**Cancellation propagation（与 InspectorStatus 正交）**：runner 必须在 step 1 之前**以及**每个 step 边界（preflight 后 / render 前 / exec 前 / parse 前 / findings 前）检查 `cancel.is_set()`；命中时直接 raise `asyncio.CancelledError`——这是 async 协作取消信号，**不**进入 `InspectorStatus` 枚举（status 闭集保持 5 值）。由 ToolRegistry / Agent loop dispatch 层负责传播。**已知 cooperative cancellation 限制**：每两个 cancel-check 之间的 `await target.exec(...)` 一旦 fire 就**不可**中断（target 子进程已起飞）；这是 cooperative cancellation 的本质，不视为缺陷；spec 不要求 runner 中断已 fire 的 target.exec。

#### 场景:target type 不兼容直接 requires_unmet

- **当** manifest.targets=[ssh]，target.type="local"
- **那么** 返回 `InspectorResult.status == "requires_unmet"`、`missing == ["target_type"]`；**未**进入 render / exec

#### 场景:capability 缺失返回 requires_unmet

- **当** manifest.requires_capabilities=[systemd]，target.capabilities={SHELL, FILE_READ}
- **那么** 返回 `status == "requires_unmet"`、`missing == ["systemd"]`

#### 场景:env secret 缺失返回 requires_unmet

- **当** manifest.secrets=[PGPASSWORD]，`os.environ` 不含 `PGPASSWORD`
- **那么** 返回 `status == "requires_unmet"`、`missing == ["env:PGPASSWORD"]`；**未**触发 target.exec

#### 场景:超时返回 timeout

- **当** target.exec 返回 `ExecResult(timed_out=True, ...)`
- **那么** 返回 `status == "timeout"`、`duration_seconds == ExecResult.duration_seconds`

#### 场景:output_schema 不匹配返回 exception

- **当** parse 返回 `{"foo": 1}`，manifest.output_schema 要求 `processes: array`
- **那么** 返回 `status == "exception"`、`error` 含 jsonschema `ValidationError.message`

#### 场景:findings 求值异常 skip 不影响 status

- **当** 6 个 finding rule 中 rule[3] 抛 `simpleeval.NameNotDefined`
- **那么** 返回 `status == "ok"`、`findings` 含 5 个 rule 的产物（rule[3] skip）

### 需求:4 种 parse format 解析行为

`hostlens.inspectors.parsers` 模块必须提供 4 个 parser 函数，签名统一为 `def parse_<format>(stdout: str, spec: ParseSpec) -> dict[str, Any]`：

**`parse_raw(stdout, spec)`**：

- `spec.raw_extract_regex is None`：返回 `{"raw": stdout}`
- `spec.raw_extract_regex` 非 None：用 `re.compile(regex).search(stdout)` 匹配（**只匹配一次**，匹配失败 → 返回 `{<col>: None for col in spec.columns}`）；成功 → 按命名组映射到 columns 字段；返回 `{col1: value1, col2: value2, ...}`

**`parse_table(stdout, spec)`**：

- 按行 split，前 `spec.skip_header_rows` 行 skip
- 每行 `re.split(r"\s+", line.strip(), maxsplit=len(spec.columns)-1)` 拆列
- 列数不足 → 该行被 skip + warning log；列数过多 → 多余部分合并到最后一列
- 返回 `{spec.columns[0] + "s" if pluralizable else spec.columns[0]: [{col: val, ...}, ...]}` —— **修订**：M1 简化为统一返回 `{"rows": [{col: val, ...}, ...]}`，让 finding DSL 写 `for_each: "rows as r"`

**`parse_json(stdout, spec)`**：

- `json.loads(stdout)`；失败 raise `json.JSONDecodeError`（由 runner 转 `status="exception"`）
- 返回结果必须是 dict（不是 list / scalar）；否则 raise `InspectorError(kind="parse_json_not_object")`（runner 转 `exception`）

**`parse_kv(stdout, spec)`**：

- 按行 split；每行用 `line.split(spec.delimiter, maxsplit=1)` 拆 key/value
- 拆分后长度 < 2 的行 skip + warning log
- key 与 value 都 strip 空白
- 返回 `{<key>: <value>, ...}`；同 key 重复 → 后者覆盖前者 + warning log

#### 场景:parse_raw 无 regex 返回完整 stdout

- **当** `parse_raw("hello world\n", ParseSpec(format="raw"))`
- **那么** 返回 `{"raw": "hello world\n"}`

#### 场景:parse_raw 含 regex 提取命名组

- **当** `parse_raw("load: 0.42, 0.50", ParseSpec(format="raw", raw_extract_regex=r"load: (?P<l1>[\d.]+), (?P<l5>[\d.]+)", columns=["l1", "l5"]))`
- **那么** 返回 `{"l1": "0.42", "l5": "0.50"}`

#### 场景:parse_raw regex 不匹配返回 None 字典

- **当** stdout="garbage"，regex 不匹配
- **那么** 返回 `{"l1": None, "l5": None}`（columns 中每个字段值为 None）

#### 场景:parse_table 跳过 header

- **当** `parse_table("PID USER\n1 root\n2 admin\n", ParseSpec(format="table", columns=["pid", "user"], skip_header_rows=1))`
- **那么** 返回 `{"rows": [{"pid": "1", "user": "root"}, {"pid": "2", "user": "admin"}]}`

#### 场景:parse_json 顶层非 dict raise

- **当** `parse_json("[1,2,3]", ParseSpec(format="json"))`
- **那么** 必须 raise `InspectorError(kind="parse_json_not_object")`

#### 场景:parse_kv 使用自定义 delimiter

- **当** `parse_kv("MemTotal:        1024 kB\nMemFree:         512 kB\n", ParseSpec(format="kv", delimiter=":"))`
- **那么** 返回 `{"MemTotal": "1024 kB", "MemFree": "512 kB"}`

### 需求:Finding DSL 引擎必须用 simpleeval 且禁用危险节点

`hostlens.inspectors.dsl.evaluate(expr: str, context: dict[str, Any], *, timeout_seconds: float = 1.0) -> Any` 必须：

- 用 `simpleeval.SimpleEval()` 实例，注入：
  - `functions = {"len": len, "sum": sum, "min": min, "max": max, "any": any, "all": all, "now": _utc_now, "float": float, "int": int}`（其中 `_utc_now()` 返回 `datetime.now(timezone.utc)`，tz-aware；`float` / `int` 是只读类型转换函数，无副作用；内置 Inspector 与用户 manifest 均可在 `when` 表达式中调用 `float(raw_extract_regex 提取的字符串)` 等转换）
  - `names = context`（runner 注入 `output` 字段 + `parameters` + `window_start` / `window_end`）
- `eval_class = simpleeval.SimpleEval`（默认配置；**不**用 `EvalWithCompoundTypes`，因为不需要 dict/set literal）
- 求值前 strict 校验：表达式 AST 中**禁止**出现 `ast.Lambda` / `ast.ListComp` / `ast.SetComp` / `ast.DictComp` / `ast.GeneratorExp` / `ast.Import` / `ast.ImportFrom` / `ast.Subscript` 中的非 slice 用法 / 任何 `__dunder__` attribute（除 `.id` / `.attr` 简单访问绑定变量的 `<name>.<simple_attr>`） → 否则 raise `simpleeval.FeatureNotAvailable`
- 求值在 `asyncio.to_thread` 中执行 + `asyncio.wait_for(timeout=timeout_seconds)`；超时 raise `asyncio.TimeoutError` 由 runner 转成 finding rule skip + warning log
- 返回值类型由表达式决定（bool / int / float / str / list / dict）

#### 场景:简单算术表达式求值

- **当** `evaluate("len(processes)", {"processes": [1, 2, 3]})`
- **那么** 返回 `3`

#### 场景:绑定变量属性访问

- **当** `evaluate("p.cpu_pct > 70", {"p": SimpleNamespace(cpu_pct=85)})`
- **那么** 返回 `True`

#### 场景:lambda 被拒绝

- **当** `evaluate("(lambda: 1)()", {})`
- **那么** 必须 raise `simpleeval.FeatureNotAvailable`

#### 场景:list comprehension 被拒绝

- **当** `evaluate("[x for x in range(10)]", {})`
- **那么** 必须 raise `simpleeval.FeatureNotAvailable`

#### 场景:dunder attribute 被拒绝

- **当** `evaluate("''.__class__.__bases__", {})`
- **那么** 必须 raise `simpleeval.FeatureNotAvailable`

#### 场景:经典逃逸路径被堵

- **当** `evaluate("().__class__.__base__.__subclasses__()", {})`
- **那么** 必须 raise `simpleeval.FeatureNotAvailable`

#### 场景:超时 raise

- **当** `evaluate("sum(range(10**8))", {}, timeout_seconds=0.1)`
- **那么** 必须 raise `asyncio.TimeoutError`（实际上 simpleeval 已禁止 `range()` 这类调用，但 timeout 是兜底）

#### 场景:now 返回 tz-aware UTC

- **当** `evaluate("now()", {})`
- **那么** 返回 `datetime` 实例且 `tzinfo == timezone.utc`

### 需求:`InspectorResult` Pydantic 模型字段集

`hostlens.inspectors.result.InspectorResult` 必须是 Pydantic v2 模型（`extra="forbid"`, `frozen=True`），含**恰好**以下字段：

- `name: str`：inspector name
- `version: str`：inspector version（manifest.version）
- `status: Literal["ok", "timeout", "target_unreachable", "requires_unmet", "exception"]`
- `target_name: str`
- `duration_seconds: float`
- `output: dict[str, Any] = {}`：parser 返回的结构化数据；`status != "ok"` 时可能为空 dict
- `findings: list[Finding] = []`：DSL 求值产生的 finding 列表；按 manifest.findings 顺序输出
- `error: str | None = None`：仅 `status ∈ {timeout, target_unreachable, exception}` 时必须含错误简述（非空非空白字符串）；`status == "ok"` 必须为 None；`status == "requires_unmet"` 允许 None（原因由 `missing` 字段承载，避免冗余）
- `missing: list[str] = []`：仅 `status == "requires_unmet"` 时有意义；其他 status 必须为空

**Finding 的 SOT（变更点）**：`hostlens.inspectors.result.Finding` 不再是独立 Pydantic 定义，而是 `hostlens.reporting.models.Finding` 的 **type alias re-export**：

```python
# src/hostlens/inspectors/result.py
from hostlens.reporting.models import Finding as Finding
```

`hostlens.reporting.models.Finding` 是项目级 Finding SOT，其字段集与字段约束由 `report-data-model` capability 的 §需求:`Finding` Pydantic 模型必须严格四字段且是 Finding SOT 规范；本 spec **不重复定义** Finding 字段集，仅说明：

- `InspectorResult.findings: list[Finding]` 中的 Finding 即 `hostlens.reporting.models.Finding`
- 与 archived `inspector-plugin-system` 旧版本相比，**`Finding.evidence` 字段类型从 `dict[str, str]` 变为 `list[Evidence]`**（BREAKING）；M1 阶段 finding DSL 在 collect 之后求值产生的 finding 默认 `evidence=[]`（DSL 当前**不**构造 evidence；evidence 构造能力留给 M6+ hook.py 或 M3 finding DSL 扩展提案）

**对 `hostlens.tools.schemas.run_inspector.FindingSummary` 的连带影响**：

```python
# src/hostlens/tools/schemas/run_inspector.py
from hostlens.reporting.models import Finding
FindingSummary = Finding  # type alias，零行为变更
```

M2 已落地的 `register_default_tools` 与 `RunInspectorOutput.findings: list[FindingSummary]` schema 声明**零修改**（跟进字段集变更通过 type alias 自动传导）；但 `default_tools.py` 中 `_run_inspector_handler` 的投影逻辑**必须**修改（约第 148-159 行）—— 当前实现用 `evidence={k: str(v) for k, v in finding.evidence.items()}` dict comprehension，BREAKING 后 `evidence` 是 `list[Evidence]` 不再有 `.items()`，必须改为 `findings=list(result.findings)` 直接复用 InspectorResult.findings（因 `FindingSummary = Finding` 同类型）。该修改属于本提案 add-report-data-model 范围内，**不**修改 archived `inspector-plugin-system` spec 的 ToolRegistry handler 行为契约（output schema 字段集不变）。

#### 场景:status=ok 时 error 必须 None

- **当** 实例化 `InspectorResult(name="x", version="1.0.0", status="ok", target_name="t", duration_seconds=1.0, error="some error")`
- **那么** 必须 raise `pydantic.ValidationError`（model_validator 强制 ok ⇒ error is None）

#### 场景:status=requires_unmet 时 missing 必须非空

- **当** 实例化 `InspectorResult(status="requires_unmet", missing=[], ...)`
- **那么** 必须 raise `pydantic.ValidationError`（model_validator 强制 requires_unmet ⇒ missing 非空）

#### 场景:status=ok 时 missing 必须为空

- **当** 实例化 `InspectorResult(status="ok", missing=["x"], ...)`
- **那么** 必须 raise `pydantic.ValidationError`

#### 场景:findings 顺序与 manifest 顺序一致

- **当** manifest.findings 索引 0/1/2 各触发 1 个 finding
- **那么** `InspectorResult.findings` 按 0,1,2 顺序排列

#### 场景:findings 中 Finding.evidence 默认为空 list

- **当** finding DSL 求值产生一个 Finding（M1 DSL 不构造 evidence）
- **那么** 该 `finding.evidence == []` 必须为 True（list 而非 dict——BREAKING 后的新字段类型）

#### 场景:Finding 是 type alias 而非独立定义

- **当** 执行 `from hostlens.inspectors.result import Finding as F_inspectors; from hostlens.reporting.models import Finding as F_reporting`
- **那么** `F_inspectors is F_reporting` 必须为 True（type alias 等价于同一类对象）

#### 场景:tools/schemas/run_inspector.FindingSummary 也是 type alias

- **当** 执行 `from hostlens.tools.schemas.run_inspector import FindingSummary; from hostlens.reporting.models import Finding`
- **那么** `FindingSummary is Finding` 必须为 True

#### 场景:旧 dict 形式 evidence 不再接受

- **当** 试图 `Finding(severity="info", message="x", evidence={"key": "value"})`（dict 而非 list）
- **那么** 必须 raise `pydantic.ValidationError`（BREAKING：M1 旧版 dict 形式不再兼容）

### 需求:`Settings.inspectors_search_paths` 字段必须可配且支持 env override

`hostlens.core.config.Settings` 必须新增字段：

- `inspectors_search_paths: list[Path] = Field(default_factory=lambda: [Path("~/.config/hostlens/inspectors").expanduser()])`
- 支持环境变量 `HOSTLENS_INSPECTORS_SEARCH_PATHS` override：值是 `:` 分隔的路径列表（Unix PATH 风格）；空字符串视为空 list

**禁止**通过 settings 配置 builtin 路径（hardcode 在 `build_registry_from_search_paths` 实现里，理由见决策 6）。

#### 场景:默认路径

- **当** 实例化 `Settings()`（无 env override）
- **那么** `settings.inspectors_search_paths == [Path.home() / ".config" / "hostlens" / "inspectors"]`

#### 场景:env override

- **当** 设置 `HOSTLENS_INSPECTORS_SEARCH_PATHS=/etc/hostlens/inspectors:/opt/team-inspectors` 后实例化 `Settings()`
- **那么** `settings.inspectors_search_paths == [Path("/etc/hostlens/inspectors"), Path("/opt/team-inspectors")]`

#### 场景:env 空字符串

- **当** 设置 `HOSTLENS_INSPECTORS_SEARCH_PATHS=""` 后实例化 `Settings()`
- **那么** `settings.inspectors_search_paths == []`

### 需求:`InspectorError` 必须扩展支持结构化字段

`hostlens.core.exceptions.InspectorError`（M0 已定义为 `HostlensError` 子类）必须支持以下 keyword-only 结构化字段（M1 落地需扩展）：

- `kind: str`（必填；M1 已知值集合见下方枚举）
- `path: Path | None = None`：manifest 文件路径
- `inspector: str | None = None`：inspector name
- `parameter: str | None = None`：parameter 名（注入校验失败时）
- `secret: str | None = None`：secret 名（secret 校验失败时）
- `field: str | None = None`：字段名（schema 校验失败时）
- `index: int | None = None`：findings 数组索引
- `existing_path: Path | None = None` / `new_path: Path | None = None`：duplicate_inspector 时两个路径
- `errors: list[dict[str, Any]] | None = None`：Pydantic 错误列表
- `extra: dict[str, Any] = {}`：兜底字段

**M1 已知 kind 取值集合（**恰好**这些；其他值 raise `ValueError`）**：

- `manifest_parse_error` / `manifest_validation_error` / `manifest_too_large`
- `unquoted_parameter_in_command` / `unquoted_array_parameter_in_command` / `array_parameter_items_type_undetermined` / `parameter_missing_charset_constraint` / `secret_inlined_in_command` / `unsafe_raw_not_supported_in_m1` / `command_template_invalid`
- `finding_when_invalid` / `finding_message_invalid_aggregate_ref`
- `duplicate_inspector` / `inspector_not_found`
- `parse_json_not_object`

`__init__` 必须用 keyword-only：`def __init__(self, *, kind, **structured_fields)`；非 keyword-only 调用 raise `TypeError`。

#### 场景:InspectorError 结构化字段可访问

- **当** raise `InspectorError(kind="duplicate_inspector", inspector="x.y", existing_path=Path("a"), new_path=Path("b"))`
- **那么** 必须能通过 `err.kind` / `err.inspector` / `err.existing_path` / `err.new_path` 访问；`str(err)` 含全部 4 字段值

#### 场景:非法 kind 被拒绝

- **当** raise `InspectorError(kind="custom_kind_not_in_enum")`
- **那么** 必须 raise `ValueError`（kind 必须在枚举集合内）

#### 场景:positional 调用被拒绝

- **当** `InspectorError("manifest_parse_error", path=...)`（位置参数）
- **那么** 必须 raise `TypeError`

### 需求:CLI `hostlens inspectors list` 必须支持过滤与 JSON 输出

`hostlens inspectors list [--tag <tag>] [--target-kind <kind>] [--json]` 必须：

- 默认输出：Rich Table 含 `name` / `version` / `description` / `tags` / `compatible_target_kinds` 列；按 name 字典序
- `--tag <tag>`：仅显示 `tag in manifest.tags` 的 inspector
- `--target-kind <kind>`：仅显示 `kind in manifest.targets` 的 inspector
- 两个过滤可同时使用，AND 语义
- `--json`：输出 JSON 数组（每条 = `InspectorSummary.model_dump()`），按 name 字典序；保证 schema 稳定（snapshot 测试覆盖）
- 命令是只读，**允许** root 执行（与 `hostlens target list` / `inspectors show` 行为一致）
- 加载错误处理：单个 manifest 加载失败 → 该 inspector 不出现在 list 输出，但 CLI **必须**在 stderr 打印每个失败文件的 path + error kind + 简短 detail，并以 **exit 1** 退出（与 proposal Failure Modes 表"防止 silent 加载失败"原则严格对齐；**禁止** silent skip，避免攻击者放注入 manifest 后用户感知不到）

#### 场景:无过滤显示全部

- **当** 在 registry 含 `hello.echo` + `system.uptime` 时跑 `hostlens inspectors list`
- **那么** 输出 2 行（按字典序：`hello.echo` 在 `system.uptime` 之前）

#### 场景:--tag 过滤

- **当** `hello.echo.tags=[demo]`、`system.uptime.tags=[system, linux]`；跑 `hostlens inspectors list --tag linux`
- **那么** 仅输出 `system.uptime`

#### 场景:--json schema 稳定

- **当** 跑 `hostlens inspectors list --json`
- **那么** 输出必须 conform `list[InspectorSummary.model_json_schema()]`；字段顺序按 Pydantic 默认（与 snapshot 一致）

#### 场景:root 不被拒绝

- **当** `sudo hostlens inspectors list`
- **那么** 必须正常返回（exit 0）

#### 场景:加载错误 exit 1 且 stderr 显示每个失败文件

- **当** 用户目录下含 1 个语法错的 yaml 文件，正常 manifest 2 个；跑 `hostlens inspectors list`
- **那么** stdout 输出正常 2 个 inspector 的表格；stderr 输出 1 行错误（含失败文件 path + error kind `manifest_parse_error` + 简短 detail）；exit code 1（**禁止** silent skip）

### 需求:CLI `hostlens inspectors show <name>` 必须脱敏 secrets

`hostlens inspectors show <name> [--json]` 必须：

- 找不到 inspector → exit 1 + 错误信息
- 默认输出：Rich 渲染 manifest 字段；`secrets:` 字段**只**显示名字列表（如 `[HOSTLENS_POSTGRES_PASSWORD]`），**禁止**从 `os.environ` 读 secret 值显示
- `parameters` 字段含 `default: "${ENV_VAR}"` 的 default 值时，只显示占位符，**不**展开 env var
- `--json`：输出 `InspectorManifest.model_dump()`（含 secrets 字段名列表，**不**含值）；schema 稳定（snapshot 测试覆盖）
- 命令是只读，允许 root

#### 场景:secrets 字段只显示名字

- **当** manifest.secrets=[HOSTLENS_POSTGRES_PASSWORD]，env `HOSTLENS_POSTGRES_PASSWORD=literal-secret-do-not-leak`，跑 `hostlens inspectors show postgres.bloat_tables`
- **那么** 输出含 `HOSTLENS_POSTGRES_PASSWORD` 但**不**含 `literal-secret-do-not-leak`

#### 场景:--json schema 稳定

- **当** 跑 `hostlens inspectors show hello.echo --json`
- **那么** 输出 conform `InspectorManifest.model_json_schema()`

#### 场景:不存在的 name exit 1

- **当** 跑 `hostlens inspectors show does.not.exist`
- **那么** exit code 1，stderr 含 `inspector_not_found`

### 需求:`hostlens doctor` 必须新增 `inspectors` section

`hostlens doctor [--json]` 必须扩展 `_check_inspectors()` 函数，输出含：

- `loaded: int`：成功加载的 inspector 数
- `errors: list[{path: str, kind: str, detail: str}]`：加载失败的 manifest 列表（含文件路径 + 错误 kind + 详情）
- `missing_secrets: list[{inspector: str, secret: str}]`：所有 inspector 声明的 secrets 在 `os.environ` 中缺失的清单（不显示 env 值）
- `status: Literal["ok", "warn", "fail"]`：`errors 非空 → fail`；`missing_secrets 非空且 errors 空 → warn`；都空 → `ok`

doctor 整体 exit code 规则保持现状：任一 section status=fail → exit 1；warn 不影响 exit code（但 stdout 显示）。

#### 场景:全部加载成功 status=ok

- **当** registry 含 2 个 builtin manifest，0 个用户 manifest，无加载错误，无 secret 缺失
- **那么** `doctor --json` 的 `inspectors.status == "ok"`、`loaded == 2`、`errors == []`、`missing_secrets == []`

#### 场景:加载错误 status=fail

- **当** 用户路径下含 1 个语法错的 yaml
- **那么** `inspectors.status == "fail"`、`errors[0].path` 含该文件、`errors[0].kind == "manifest_parse_error"`；doctor exit 1

#### 场景:secret 缺失 status=warn

- **当** 某 inspector 声明 `secrets: [PGPASSWORD]` 且 `os.environ` 不含 `PGPASSWORD`
- **那么** `inspectors.status == "warn"`、`missing_secrets[0] == {"inspector": "...", "secret": "PGPASSWORD"}`；doctor exit 0（warn 不阻塞）

#### 场景:M0 doctor 兼容

- **当** 跑 `hostlens doctor --json`
- **那么** 输出必须**同时**含 M0 落地的 `python_version` / `anthropic_api_key` / `config_dir` section，与本提案新增的 `inspectors` section 并存（**不**破坏 M0 已有契约）

### 需求:内置 Inspector `hello.echo` 与 `system.uptime` 必须满足验收契约

`src/hostlens/inspectors/builtin/hello/echo.yaml` 必须：

- `name: hello.echo`
- `version: 1.0.0`
- `description`: 简短说明用于验证 inspector 管线
- `tags: [demo, hello]`
- `targets: [local, ssh]`
- `requires_capabilities: []`
- `requires_binaries: [echo]`
- `privilege: none`
- 无 `parameters` / 无 `secrets`
- `collect.command: "echo hello"`、`collect.timeout_seconds: 5`
- `parse.format: raw`、无 `raw_extract_regex`
- `output_schema`: `{type: object, properties: {raw: {type: string}}, required: [raw]}`
- `findings`: 1 个聚合 rule `{when: "len(raw) > 0", severity: info, message: "hello received: {raw}"}`（由于 simpleeval 上下文中无 `target_name` 变量，message 直接引用 output 字段 raw）

`src/hostlens/inspectors/builtin/system/uptime.yaml` 必须：

- `name: system.uptime`
- `version: 1.0.0`
- `description`: 提取负载平均值
- `tags: [system, linux, performance]`
- `targets: [local, ssh]`
- `requires_capabilities: [shell]`
- `requires_binaries: [uptime]`
- `privilege: none`
- 无 `parameters` / 无 `secrets`
- `collect.command: "uptime"`、`collect.timeout_seconds: 5`
- `parse.format: raw`、`raw_extract_regex: "load average:\\s+(?P<load1>[\\d.]+),\\s+(?P<load5>[\\d.]+),\\s+(?P<load15>[\\d.]+)"`、`columns: [load1, load5, load15]`
- `output_schema`: `{type: object, properties: {load1: {type: [string, "null"]}, load5: ..., load15: ...}}`
- `findings`: 2 个聚合 rule（M1 范围；load 阈值是固定字面量，未来 M6 时再 parameterize）：
  - `{when: "load1 and float(load1) > 4.0", severity: warning, message: "1-min load average is {load1}"}`
  - `{when: "load1 and float(load1) > 8.0", severity: critical, message: "1-min load average critically high: {load1}"}`

（`float` / `int` 由 §需求:Finding DSL 引擎... 中的 functions 集合显式注册；本需求块**不需要**额外修订该函数集——上方已涵盖。）

**本提案 MODIFIED 的变更点**：仅 `hello.echo demo path 跑通` 场景的 finding 字段类型——`evidence` 从 archived 的 `{...}` dict 字面量改为 `[]` 空 list（兑现 BREAKING `evidence: list[Evidence]`）；新增 `tags=[]`（兑现 Finding 新字段）。其他字段集、manifest yaml、`hello.echo 加载成功` / `system.uptime 在 linux/macos 上跑通` / `simpleeval 必须支持 float/int 内置` 三个场景**保持不变**。

#### 场景:hello.echo 加载成功

- **当** `result = build_registry_from_search_paths([], settings=Settings())` 装配
- **那么** `result.registry.get("hello.echo")` 必须返回完整 `InspectorManifest`；`result.errors == []`

#### 场景:hello.echo demo path 跑通

- **当** 用 `LocalTarget("local-host")` + `runner.run(manifest_hello, target, ...)` 完整跑通
- **那么** 返回 `InspectorResult.status == "ok"`、`findings == [Finding(severity="info", message="hello received: hello\n", evidence=[], tags=[])]`（**BREAKING 后**：evidence 是空 list 而非 dict 字面量；tags 是空 list 新增字段默认值）

#### 场景:system.uptime 在 linux/macos 上跑通

- **当** 在 macOS 或 Linux 上用 `LocalTarget` 跑 `system.uptime`
- **那么** 返回 `InspectorResult.status == "ok"`、`output.load1` 是数值字符串（非 None）

#### 场景:simpleeval 必须支持 float/int 内置

- **当** `evaluate("float('4.5') > 4.0", {})`
- **那么** 返回 `True`（float/int 已注册到 functions）

#### 场景:hello.echo 不再接受 dict evidence 旧形式

- **当** 试图把 archived spec 中 `evidence={"key": "value"}` dict 形式的 finding 构造写进 hello.echo 测试
- **那么** Pydantic 校验失败 + 测试 fail（BREAKING 兜底；保证旧测试不会偶然通过）

#### 场景:hello.echo manifest yaml 保持不变

- **当** 加载 `inspectors/builtin/hello/echo.yaml`
- **那么** manifest 顶层字段集与上方列出的 14 项契约一致；本 MODIFIED 仅影响 finding 构造时 Pydantic 字段类型，**不**改 builtin manifest yaml

### 需求:collect.sampling_window 时窗采集

The system SHALL 支持 manifest 可选字段 `collect.sampling_window.duration_seconds`；当声明时，runner MUST 基于可注入时钟计算 `window_end = now`、`window_start = now - duration_seconds`，并把 `window_start` / `window_end`（`YYYY-MM-DD HH:MM:SS` UTC 字符串，journalctl `--since/--until` 友好，非带 `T`/时区偏移的 ISO 形式）与 `window_seconds`（int）注入到 Jinja2 命令渲染上下文与 Finding DSL 求值上下文。省略该字段时行为与既有 Inspector 完全一致（向后兼容）。

#### 场景:注入窗口变量到命令渲染

- **当** Inspector 声明 `collect.sampling_window.duration_seconds: 300` 且 `collect.command` 引用 `{{ window_start }}` / `{{ window_end }}`
- **那么** runner 用 `[now-300s, now]` 的 `YYYY-MM-DD HH:MM:SS` UTC 字符串渲染命令，`window_start` 早于 `window_end` 恰好 300 秒

#### 场景:窗口变量可用于 Finding DSL

- **当** 某 finding 的 `when` 表达式引用 `window_seconds`
- **那么** DSL 求值上下文中 `window_seconds` 等于声明的 `duration_seconds`

#### 场景:省略 sampling_window 保持旧行为

- **当** Inspector manifest 未声明 `collect.sampling_window`
- **那么** 渲染与 DSL 上下文中不出现 `window_start` / `window_end` / `window_seconds`，加载与执行行为与本 delta 之前完全一致

### 需求:可注入时钟保证回放确定性

The system SHALL 允许向 runner 注入时钟（默认真实 UTC 时钟）；测试与回放场景 MUST 能注入固定时钟，使含窗口变量的渲染命令在重复运行间逐字节稳定（从而可被 `ReplayTarget` 精确匹配，并使 snapshot 稳定）。

#### 场景:冻结时钟产出稳定命令

- **当** 注入固定时钟并对同一 `sampling_window` Inspector 渲染命令两次
- **那么** 两次渲染出的命令字符串完全相同

### 需求:窗口注入变量名为保留名

The system SHALL 把 `window_start` / `window_end` / `window_seconds` 视为运行时注入的保留名；当 manifest `parameters` 声明了与之同名的字段时，loader MUST 拒绝加载并给出字段级错误，避免 parameter 覆盖注入变量造成求值歧义。

#### 场景:parameter 撞保留名被拒

- **当** 某 manifest 的 `parameters` 声明名为 `window_start` 的字段
- **那么** loader 拒绝加载该 manifest 并指出该名为保留注入变量名

### 需求:Agent 表面结构化参数的 JSON 解码 coercion

The system SHALL 在 runner 参数 coercion 阶段，对 manifest 声明类型为 `array` / `object` 的 string 参数值尝试 `json.loads`；当解码成功且结果与声明的容器类型一致时采用解码值，否则保留原字符串交由后续 `jsonschema.validate` 拒绝（与既有 `integer` / `number` / `boolean` coercion 同属 permissive-coerce-then-validate 不变式）。这使数组 / 对象参数可经 Agent 表面 `RunInspectorInput.parameters: dict[str, str]` 以 JSON 编码字符串传入；shell 注入防御仍由 manifest 的 `items.pattern` 与命令模板的 `| sh`（`shlex.quote`）保证，解码分支不会让未校验值到达 `target.exec`。`RunInspectorInput.parameters` 的 `dict[str, str]` tool schema 不变。

#### 场景:JSON 数组字符串解码后通过校验

- **当** Agent 对声明 `endpoints: {type: array}` 的 Inspector 传入 `parameters={"endpoints": "[\"database:5432\"]"}`
- **那么** runner 将其解码为 `["database:5432"]`，`jsonschema.validate` 按 `items.pattern` 通过，命令渲染得到展开后的端点

#### 场景:非 JSON 或类型不符的字符串被拒

- **当** array 类型参数收到非 JSON 字符串（如 `"database:5432"`）或解码结果不是数组
- **那么** 保留原字符串，`jsonschema.validate` 以类型不符拒绝，Inspector status=exception，从不到达 `target.exec`

#### 场景:解码出的坏 item 被 items.pattern 拒

- **当** array 参数解码为 `["database:5432;whoami"]`（含非法字符）
- **那么** `items.pattern`（`^[a-zA-Z0-9.-]+:[0-9]+$`）拒绝该 item，命令不渲染、不执行

### 需求:默认工具装配支持注入时钟

The system SHALL 让默认工具装配函数 `register_default_tools` 接受可选时钟参数；当传入时，所注册的 `run_inspector` 工具 MUST 把该时钟透传给 `InspectorRunner`，使 Agent → `run_inspector` → runner 路径上的 `sampling_window` Inspector 渲染确定性命令。`ToolContext` 字段集保持不变（时钟经工具装配边界注入，不进 DI 容器，遵守 ADR-008）。

#### 场景:经 Agent 路径的窗口命令确定性

- **当** 以 `register_default_tools(registry, clock=<固定时钟>)` 装配，并经 Agent 路径运行声明 `sampling_window` 的 Inspector
- **那么** 渲染命令使用注入的固定时钟（与不传 clock 时的真实 UTC 默认行为隔离），使 `ReplayTarget` 可精确匹配
