# Inspector 作者契约（Authoring Contract）

> 这是写 Hostlens builtin / 社区 Inspector 的**规范层**（normative）文档：codify
> 五条承重墙派生的硬规则。每条规则配「为什么」+ 正/反例。
> 想要 step-by-step 上手教程见
> [inspector-authoring.md](inspector-authoring.md)（tutorial）；
> 完整 manifest 字段参考见 [inspectors.md](inspectors.md)。
>
> 本契约由 OpenSpec 变更
> [`add-inspector-authoring-contract`](../../openspec/changes/add-inspector-authoring-contract/)
> 落地，用本期三个跨数据形态硬 inspector 作活例证明：
>
> | 活例 | 数据形态 | 证明的规则 |
> |---|---|---|
> | `postgres.bloat_tables` | SQL → JSON | 全派生在 SQL 计算列 / `json_build_object` 吐顶层对象 |
> | `docker.containers.restart_loop` | 容器原生 JSON | 单 `for_each` = 容器 / 跨容器 join 在命令内 |
> | `redis.slowlog` | 版本敏感 CLI | 窄 scope 文档式声明 / metrics-only 退路 |

---

## 五条承重墙（代码事实源）

契约的每条规则都从一条**已在代码核实**的约束派生。改 Inspector 前先理解这五堵墙：

| # | 承重墙 | 代码出处 | 推论 |
|---|---|---|---|
| 1 | Finding DSL 白名单仅 `len/sum/min/max/any/all/now/float/int`，禁 string / split / regex / 推导式 / lambda / dunder | `inspectors/dsl.py`（`_DSL_FUNCTIONS` + `validate_ast`） | 一切抽取与数值派生**只能在 collector** |
| 2 | `for_each` 仅单绑定 `"<expr> as <var>"` | `inspectors/dsl.py`（`_FOR_EACH_PATTERN`） | 跨命令 / 跨行关联**只能在 collector** |
| 3 | finding 求值上下文把 `output` 与 `parameters` 合并，**同名 parameter 遮蔽 output 键** | `net/tls_cert_expiry.yaml` 注释 + runner | 强制输出键命名约定 |
| 4 | `parse.format: json` 要求顶层为 dict，**拒绝顶层数组 / 标量** | `inspectors/parsers/json.py`（`parse_json_not_object`） | SQL/CLI 必须包成顶层对象 |
| 5 | manifest `extra="forbid"`，**无** `min_binary_version` / `hook` / `sql_result` 字段；capability enum 固定；tag 正则不含 `+` | `inspectors/schema.py` | 版本门只能文档式声明；新字段会被拒 |

---

## 规则 1 — 一切抽取与数值派生必须在 collector 命令内完成

**承重墙 1。** Finding DSL 只有这九个函数，没有任何字符串 / 正则 / split / 推导式
能力：

```python
# inspectors/dsl.py
_DSL_FUNCTIONS = {"len", "sum", "min", "max", "any", "all", "now", "float", "int"}
```

所以**字段抽取、解析、数值派生（比率 / 百分比 / 延迟秒）必须在 `collect.command`
里算好**（shell 算术 / `jq` / SQL 计算列），finding 规则**只对已就绪的标量 / 集合**做
阈值与成员比较。

**为什么**：在 finding 表达式里试图做派生会撞 DSL 白名单（`split` / 正则不存在）、被
`validate_ast` 在 manifest 加载期拒掉（报 `finding_when_invalid`），或在运行期静默失败。

### 反例（在 finding 里派生 — 会被拒）

```yaml
findings:
  # ❌ DSL 无字符串 split；'used'.split('%') 在 validate_ast / 运行期都过不了
  - when: "float(disk_line.split()[4].rstrip('%')) > 90"
    severity: warning
    message: "disk full"
```

### 正例（派生压进 collector，DSL 只判标量）

```yaml
collect:
  # 百分比在 shell 算好，吐 JSON
  command: |
    used=$(df --output=pcent / | tail -1 | tr -dc 0-9)
    printf '{"used_pct":%d}' "$used"
parse: { format: json }
findings:
  - when: "used_pct > 90"        # ✅ 只对标量做阈值比较
    severity: warning
    message: "root fs {used_pct}% full"
```

活例 `postgres.bloat_tables`：bloat 比率全在 SQL 计算列算出，finding 只比较数值；
`tls_cert_expiry`：`days_until_expiry` 在 shell（GNU `date -d`）算出整数，DSL 只做
`<= critical_days` 比较。

---

## 规则 2 — 跨命令 / 跨行关联必须在 collector 内完成（单 `for_each`）

**承重墙 2。** `for_each` 只接受**单绑定**形式：

```python
# inspectors/dsl.py
_FOR_EACH_PATTERN = re.compile(r"^(.+?)\s+as\s+([a-z_][a-z_0-9]*)$")   # "<expr> as <var>"
```

没有第二个绑定、没有 nested loop、没有 join 语法。**任何跨命令输出或跨行结果的关联
（如容器列表 join inspect 详情）必须在 collector 命令里完成**（单条命令 / 管道 /
`for_each $(...)` 展开 / SQL JOIN）后吐**单一 JSON**，finding 层只 `for_each` 遍历这个
已关联好的集合。

**为什么**：`for_each` 单绑定无法表达 join；试图在 finding 层关联两个集合无路可走。

### 反例 / 正例

```yaml
# ❌ 想在 finding 层 join 两个集合 —— for_each 单绑定做不到
# ✅ 在 collector 里 join 后吐一个 results 数组：
collect:
  command: |
    docker ps --format '{{ "{{.ID}}" }}' | while read id; do
      docker inspect "$id" --format '...'   # 关联 ps 列表 + inspect 详情
    done | jq -s '{results: .}'
findings:
  - for_each: "results as c"      # ✅ 单绑定遍历已关联好的集合
    when: "c.restart_count > 5"
    severity: critical
    message: "container {c[name]} in restart loop"
```

活例 `docker.containers.restart_loop`：容器列表与 inspect 详情在命令内关联，`for_each`
单绑定 = 容器。

---

## 规则 3 — 输出顶层键必须用约定命名（`results` / `items` / `records`）防 parameter 遮蔽

**承重墙 3。** finding 求值上下文把 `output`（collector 输出）与 `parameters` 合并到
同一个命名空间，**同名 `parameter` 会遮蔽 `output` 键**。所以 collector 输出的顶层结果
键**必须**取自 `results` / `items` / `records` 之一，**禁止**与任一已声明 parameter 同名。

**为什么**：若输出键叫 `endpoints` 而你又有个 `endpoints` parameter（一个 host:port
字符串数组），finding 上下文里 `endpoints` 解析成参数值，`for_each: "endpoints as e"`
会遍历字符串而非结果 dict —— 静默错误，不报错。`tls_cert_expiry` 已踩过这个坑，注释里
写明了规避方式（用 `results`）。

### 反例 / 正例

```yaml
parameters:
  properties:
    endpoints: { type: array, items: { type: string, pattern: "^[a-zA-Z0-9.-]+:[0-9]+$" } }
collect:
  # ❌ printf '{"endpoints":[...]}'   —— 与 parameter `endpoints` 撞名，被遮蔽
  command: |
    printf '{"results":[...]}'        # ✅ 顶层键 results，不与任何 parameter 同名
findings:
  - for_each: "results as e"          # ✅ 遍历结果 dict，不是参数字符串
    when: "e.days_until_expiry <= critical_days"
    severity: critical
    message: "..."
```

---

## 规则 4 — `parse.format: json` 的顶层必须是 JSON 对象

**承重墙 4。** JSON 解析器强制顶层为 dict：

```python
# inspectors/parsers/json.py
data = json.loads(stdout)
if not isinstance(data, dict):
    raise InspectorError(kind="parse_json_not_object")   # 拒绝顶层数组 / 标量
```

裸 `json_agg(...)` / `docker ps --format json` 的逐行 JSON / 顶层数组都会被拒。**必须把
结果包进一个顶层对象**（其中一个键就是规则 3 约定的 `results` / `items` / `records`）。

### 正例

```sql
-- ✅ json_build_object 吐顶层对象，results 键满足规则 3
SELECT json_build_object(
  'total_tables', (SELECT count(*) FROM pg_stat_user_tables),
  'results', coalesce(json_agg(t), '[]'::json)
) FROM ( /* 派生 bloat 列 */ ORDER BY n_dead_tup DESC LIMIT {{ max_results }} ) t
```

```bash
# ✅ docker ps --format json 是逐行 JSON（非顶层数组），用 jq -s 包成对象
docker ps --format json | jq -s '{results: .}'
```

活例 `postgres.bloat_tables`：用 `json_build_object('results', json_agg(...))` 而非裸
`json_agg`（后者吐顶层数组、被 `parse_json_not_object` 拒）。它同时是列表形态截断的活例
——subquery 经 `ORDER BY n_dead_tup DESC LIMIT {{ max_results }}` 截 top-N、外层 `total_tables`
标量给截断前总数（完整截断后形态见 `bloat_tables.yaml`）。

---

## 规则 5 — 运行前提必须文档式声明（schema 无机器门）

**承重墙 5。** manifest `extra="forbid"` 且**没有** `min_binary_version` 字段：

```python
# inspectors/schema.py
class InspectorManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)   # 未知字段在加载期被拒
    tags: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]*$")]]   # 注意：不含 `+`
```

所以**版本下限**（Redis 6+ / MySQL 8.0+）、**要求调用方提供的标识**（如 JVM PID）、
**所需 `--json` 能力客户端**这类前提**只能在 `description` 与 `tags` 里声明**，由本契约
统一约定措辞。

**为什么**：新增 manifest 字段会被 `extra="forbid"` 在加载期拒掉。不满足前提时由
preflight 的 `requires_binaries` 探测或主命令失败兜底（报 `command not found` 之类，而非
结构化「版本不匹配」—— 这是文档式声明的已知代价，结构化版本门是未来独立提案）。

### tag 不能含 `+`

tag 正则 `^[a-z][a-z0-9_-]*$` **不含 `+`**。版本下限用 `redis6` / `mysql8` /
`json-client` 这类 tag 配合 `description` 自由文本表达：

```yaml
# ❌ tags: [redis6+]            —— `+` 不在 tag 正则里，loader 拒（manifest_validation_error）
# ✅
tags: [redis, redis6, json-client]
description: "Redis 慢日志巡检。需 Redis 6+ 且 redis-cli 支持 --json。"
```

活例 `redis.slowlog`：`description` 写明「需 Redis 6+ 且 `redis-cli --json`」，tag 用
`redis6` / `json-client`。

### Linux-only 依赖必须声明

依赖 GNU 工具特性（如 GNU `date -d` 做日期算术、`df --output=`、`/proc` 路径）的
inspector **必须**在 `description` 注明 Linux-only（BSD / macOS 的 `date` 无 `-d`）。

```yaml
# ✅
description: "TLS 证书到期检查。days_until_expiry 用 GNU `date -d` 计算，Linux-only。"
```

活例 `tls_cert_expiry`：manifest 注释明写「GNU `date -d`, Linux-only」。

---

## 规则 6 — 命令注入安全三件套

凡把调用方参数（尤其数组 / 字符串）插入 `collect.command` 的 inspector，**必须**同时做到
下面三件，缺一不可。loader 在加载期做 Jinja2 AST walk 强制（`inspectors/loader.py`）。

### (a) 参数经 `| sh`（shlex.quote）后再进 shell

- **字符串参数**：`{{ param | sh }}` —— 缺 `| sh` 报 `unquoted_parameter_in_command`。
- **字符串数组参数**：`{{ arr | map('sh') | join(' ') }}` —— 缺这个精确链报
  `unquoted_array_parameter_in_command`。
- 数值 / 布尔参数无需过滤（不可被 shell 当文本求值）。

### (b) 参数 schema 用 `pattern`（或 `enum`）收紧取值域

任何 `type: string`（含数组的 `items: {type: string}`）参数**必须**声明 `pattern` 或
`enum`，否则 loader 报 `parameter_missing_charset_constraint`。数组 `items` 缺 `type` 报
`array_parameter_items_type_undetermined`。

### (c) 不得裸 `{{ param }}` 进可执行位置

不允许未引用直接拼进 shell 可执行位。secret **不走** `{{ }}` —— 在命令里用 shell 变量
`$PGPASSWORD` 引用（runner 经 env 注入），把 secret 名放进插值位报
`secret_inlined_in_command`。

### 活例（`tls_cert_expiry`，三件套全做到）

```yaml
parameters:
  properties:
    endpoints:
      type: array
      items: { type: string, pattern: "^[a-zA-Z0-9.-]+:[0-9]+$" }   # (b) pattern 收紧
collect:
  command: |
    for ep in {{ endpoints | map('sh') | join(' ') }}; do          # (a) map('sh') | join
      host=${ep%%:*}
      end=$(echo | openssl s_client -servername "$host" -connect "$ep" ...)
      ...
    done
```

---

## 规则 7 — `requires_binaries` / `requires_capabilities` 约定

preflight 探测靠这两个字段；它们是**结构化前置门**（与规则 5 的文档式声明互补）。

### `requires_capabilities`

只能取 capability enum 中的值（`inspectors/schema.py`）：

```
{shell, file_read, ssh, systemd, docker_cli}
```

未知值报 `unknown_capability`。**本契约不扩 enum**。但**注意**：`docker_cli` / `systemd`
是**惰性探测**的 capability（首次 `exec` 后才加入 `target.capabilities`），而 preflight
在 exec 前校验 `requires_capabilities`——故**禁止**把它们放进 `requires_capabilities` 当门
（见规则 9）；容器 / systemd 巡检改用 `requires_binaries: [docker]` / `[systemctl]` 门控。
`requires_capabilities` 只放**静态** capability（`shell` / `file_read` / `ssh`，target 构造时即在）。

```yaml
requires_capabilities: [shell]        # tls_cert_expiry / postgres.bloat_tables / redis.slowlog（静态 cap，安全）
# 容器 / systemd 巡检：用 requires_binaries 门控，不放惰性的 docker_cli / systemd（规则 9）
requires_binaries: [docker, jq, xargs]   # docker.containers.restart_loop
```

### `requires_binaries`

声明 inspector 依赖的可执行文件（runner 在 preflight 探测其存在）。字段正则
`^[a-zA-Z0-9._-]+$`：

```yaml
requires_binaries: [openssl]   # tls_cert_expiry
requires_binaries: [psql]      # postgres.bloat_tables
requires_binaries: [docker]    # docker.containers.restart_loop
```

不满足时 preflight 失败 —— 这正是规则 5「文档式版本门」的兜底机制（缺 binary 或版本太老
→ `command not found` / 主命令失败）。

### `requires_files`

须是**规范绝对路径**（正则 `^/[A-Za-z0-9._/-]+$`，禁 `.` / `..` 组件）。

---

## 规则 8 — collector 必须在 backend 失败时 fail-loud（禁造 fallback 成功对象）

backend（Redis / Docker daemon / DB ...）**不可达 / 认证失败 / 查询出错**时，collector
**必须**非零退出且 stdout 为**空或非-JSON**，**禁止**制造 fallback 成功对象
（如 `{"count":0}` / `{"results":[]}`）。

**为什么**：runner **不校验主命令退出码** —— 它只解析 stdout
（`runner.py` 第 8 步 `target.exec` 后直接进 parse，从不看 `exec_result.exit_code`）。
于是 collector 在 backend 宕机时若兜底吐一个合法成功对象，会被祝福成 `status=ok`，
监控反而把「backend 不可达」误报成「健康」—— 监控自己在撒谎，比没有监控更危险。
正确做法是让失败路径产出空/非-JSON stdout，由 `parse_json` 抛 `JSONDecodeError` /
`InspectorError`，runner 收口成 `status=exception`（诚实）。

**「genuine 空结果」与「backend 宕机」必须可区分**：前者 client 成功返回空集
（如空 slowlog `count=0`、`docker ps` 成功但零容器）→ 合法对象 `status=ok`；后者 client
非零退出 → exit 1 + 空 stdout → `status=exception`。两者**走不同退出码路径**才能区分。

`postgres.bloat_tables` 是正确模板：`psql` 失败 → 空 stdout → parse 抛 `JSONDecodeError`
→ `status=exception`，从不兜底假装健康。

### 反例（造 fallback 成功对象 — 宕机被误报健康）

```bash
# ❌ redis-cli 失败时 ${count:-0} 兜底成 0，吐 {"count":0} → status=ok（撒谎）
count=$(redis-cli ... --json SLOWLOG LEN)
printf '{"count":%d}' "${count:-0}"
# ❌ docker ps 失败也吐 {"results":[]} → 报「无重启循环容器」而 daemon 已死
ids=$(docker ps -aq ...)
if [ -z "$ids" ]; then printf '{"results":[]}'; fi
```

### 正例（fail-loud + 校验，仅 genuine 空集才吐合法对象）

```bash
# ✅ 失败即 exit 1（空 stdout → parse 异常 → status=exception）
count=$(redis-cli ... --json SLOWLOG LEN) || { echo "SLOWLOG LEN failed" >&2; exit 1; }
case "$count" in ''|*[!0-9]*) echo "non-numeric: $count" >&2; exit 1;; esac
printf '{"count":%d}' "$count"     # 仅 count 是真整数（含 0）才吐
```

```bash
# ✅ docker ps 成功且真为空集才吐 {"results":[]}；ps 失败 → exit 1
ids=$(docker ps -aq ...) || { echo "docker ps failed" >&2; exit 1; }
if [ -z "$ids" ]; then printf '{"results":[]}'; exit 0; fi
... | jq -c '{results: [...]}' || { echo "inspect/jq failed" >&2; exit 1; }
```

活例 `redis.slowlog` / `docker.containers.restart_loop`：每个 backend 调用失败即非零退出，
对标量结果校验是整数，只有真成功（含 genuine 空集）才吐合法 JSON。

---

## 规则 9 — 用 `requires_binaries` 门控外部工具，不要门控**惰性探测**的 capability

**为什么**：`InspectorRunner` 的 preflight **先**校验 `requires_capabilities`（step 2）、**后**才做 binary 探测 / 主命令 exec（step 5/8）；而某些 capability（如 `docker_cli`）在 `LocalTarget` / `SSHTarget` 上是**惰性探测**的——只有首次 `exec` 之后才加入 `target.capabilities`。两者叠加 ⇒ 在一台**装了 docker** 的主机上，preflight step 2 检查 `requires_capabilities: [docker_cli]` 时该 capability 还没被探测到 → `requires_unmet` → inspector 永不运行（snapshot 测试会因 fixture 录制时已暖身探测过、误以为正常）。

**静态 vs 惰性**：target 构造时即在的 capability 是**静态、可安全门控**的——`shell` / `file_read`（Local+SSH）、`ssh`（SSH）。其余 enum 值（`docker_cli` / `systemd`）是**惰性探测**的，**禁止**放进 `requires_capabilities`。

**规则**：依赖外部 CLI（docker / psql / redis-cli / systemctl 等）的 inspector **必须**用 `requires_binaries:` 门控（preflight step 5 用 `command -v` 探测、不依赖惰性 capability），**禁止**把惰性 capability（`docker_cli` / `systemd`）放进 `requires_capabilities` 当门。活例 `docker.containers.restart_loop` 只声明 `requires_binaries: [docker, jq, xargs]`（不声明 `docker_cli`）；`linux.systemd.failed_units` 只声明 `requires_binaries: [systemctl, awk]`（不声明 `systemd`）。回归锁：(a) 用 capability 只含 `{shell, file_read}`（冷 target）的 fixture 回放，断言 inspector 仍 `status=ok` 而非 `requires_unmet`；(b) `test_builtin_capability_gate.py` 扫描全部 builtin manifest，断言 `requires_capabilities ⊆ {shell, file_read, ssh}`。

---

## 规则 10 — FindingRule `message` 必须是简短中文标签 + 注入关键数据（禁空指针 / 禁纯英文长句）

FindingRule 的 `message` 是报告里运维**第一眼看到的那一行**。它走 Python `str.format`（**不是** Jinja，与命令模板的 Jinja2 不同引擎；见 [`project_manifest_parameters_must_wrap_type_object`] 邻近约定），用 `{field}` 注入 collector 输出字段。本规则把它从「作者手写的静态英文串」收口成「简短中文标签 + 具体数据」。

> 落地于 OpenSpec 变更 `improve-report-rendering-and-i18n`。旗舰样板是
> `linux.systemd.failed_units`；全量内置 inspector 的 message 改写是**分阶段多 PR 长尾**，
> crosscheck 机审**按已迁移 allowlist 范围**生效（见下「机审边界」）。

每条 `message` **必须**同时满足以下五点：

- **(a) 简短中文标签**：用一句简短中文描述问题类别（如「systemd 失败服务」「磁盘使用率超阈值」「内存不足」），让人一眼知道是哪类问题。叙述性的根因分析归 Diagnostician（中文，见 diagnostician 系统提示），`message` 只做「标签 + 数据」，不写整段推理。
- **(b) 注入关键数据**：对**有可变数据**的发现，用 `{field}` 注入 collector 输出的关键数据（哪个单元 / 什么值 / 什么阈值）。被注入的 `{field}` **必须**是 `output_schema` 保证存在的字段（`required` 或有容错默认），否则 `str.format` 抛 `KeyError`、该规则被静默跳过。
- **(c) 注入干净人读串，禁 repr**：`str.format` 对**数组 / 对象类**输出会吐 **Python repr**（如 array-of-objects 渲染成 `[{'unit': 'foo.service'}]`），严禁直接注入这类字段。collector **必须额外 emit 一个已 join 的串字段**（如在 `failed: [{unit:...}]` 旁配一个 `failed_names: "foo.service, bar.service"`），`message` 注入那个干净串字段、而非 raw 对象数组。
- **(d) 禁空指针**：禁止 `see X for details`（中英皆禁）这类**不含实际数据**的指针式措辞——发现必须自带具体指向，不必跳别处查。
- **(e) 禁纯英文长句**：禁止整段英文叙述。技术术语 / 命令 / 字段名 / 路径可保留英文，但 message 主体是简体中文标签。

**为什么**：本系统面向中文无人值守日报。手写静态英文串既不说**哪个**对象出了问题、又读着费劲；`see X for details` 把运维支到别处反而更慢；raw 数组注入吐 repr 让报告夹一坨 Python 字面量。中文标签 + 干净数据注入让每条发现**自带指向**。

### 反例 / 正例（旗舰样板 `linux.systemd.failed_units`）

`failed` 是 array-of-objects（`[{unit:...}]`）。直接注入 `{failed}` 会吐 repr；写英文静态串又违反 (a)(e)：

```yaml
# ❌ 英文静态串 + 空指针 + 不注入数据（改写前的旧 message）
message: "One or more systemd units are in the failed state (see failed for details)"

# ❌ 中文了但注入 raw 数组 → str.format 吐 "[{'unit': 'nginx.service'}]" repr
message: "systemd 失败服务：{failed}"
```

```yaml
# ✅ collector 在 awk 单遍里同时 emit `failed`（数组）与 `failed_names`（已 join 的干净串）
collect:
  command: |
    systemctl list-units --type=service --state=failed --no-legend --plain 2>/dev/null \
      | awk '
          { unit=$1; if (unit != "") { arr[n++]=unit } }
          END {
            printf "{\"failed\":[";
            for (i=0;i<n;i++) { printf "%s{\"unit\":\"%s\"}", (i?",":""), arr[i] }
            printf "],\"failed_names\":\"";
            for (i=0;i<n;i++) { printf "%s%s", (i?", ":""), arr[i] }
            printf "\"}";
          }'
output_schema:
  type: object
  properties:
    failed:       { type: array, items: { type: object, properties: { unit: { type: string } } } }
    failed_names: { type: string }          # 干净 join 串，供 message 注入
  required: [failed, failed_names]           # 两者都 required，注入不会 KeyError
findings:
  - when: "len(failed) > 0"                   # 计数留在 when（str.format 不能调函数）
    severity: critical
    message: "systemd 失败服务：{failed_names}"   # ✅ 注入 "nginx.service, mysql.service"
```

> `message` 里**不能**写 `{len(failed)}`——`str.format` 不能在 `{}` 里调函数（会抛
> `KeyError` 并跳过该规则），计数判断只留在 `when` 表达式里。

### 凡注入数组 / 对象类字段，都须配套 emit 干净 join 串

这不是 systemd 专属规约：**任何**要在 message 里展示一组对象（容器名 / 表名 / 端点）的 inspector，都须在 collector 里额外 emit 一个已 join 的串字段（`<name>_names` / `<name>_list` 之类），message 注入那个串，而非 raw 数组。

### 机审边界（crosscheck 是静态检查、按 allowlist 范围、不验证运行时正确性）

crosscheck 测试把上面的规约**部分**变机审，但有明确边界——它**不是**「全量必中文」的硬门：

- **范围按已迁移 allowlist**：crosscheck 的「含中文 / 无空指针 / 注入字段已声明」断言**只**施加在「已迁移 allowlist」（初始 = `{linux.systemd.failed_units}`，各域长尾 PR 改写完逐个把 inspector 的 canonical `name` 加入）。**禁**对 allowlist 外未迁移 inspector 施加中文 / 注入断言——否则尚未改写的英文 message 会让 crosscheck 一上线即全红。
- **防漂移断言**：每个内置 inspector **必须**恰好属于「已迁移 allowlist」或「待迁移 backlog」之一，且二者并集 == 全部内置 inspector。新增 inspector 若未分类（既不在 allowlist 也不在 backlog）则 crosscheck **失败**，强制作者把它纳入其一——使「全量 vs 长尾」边界显式可见，不靠「碰巧没人加新 inspector」逃逸。
- **(c) 是 if-inject-then-declared 守卫**：crosscheck **若** message 含 `{field}` 注入**则**断言该字段在 `output_schema` 声明存在；它**不**机械强制「每条 message 必须注入」（「有可变数据必须注入」是作者目标契约，机械判定「哪些字段算可变数据」太模糊，留人审 + review）。genuinely 无可变数据的纯标签 inspector 无需注入、**也不需要任何豁免标记**。
- **能力上限**：crosscheck 是**静态**检查（**不**实例化 collector、**不**跑命令）。它**不能**验证被注入的 `{field}` 运行时是否真实存在、**不能**验证注入值渲染是否干净（无 repr 泄漏）、**不**机械强制「该注入的有没有注入」——这些靠 collector 单测 + 真机 demo + 人审兜，本契约不宣称 crosscheck 覆盖运行时正确性。

### finding id 一次性 churn（改 message 的已知副作用）

`message` 是 `compute_finding_id(inspector_name, inspector_version, message)` 的输入（`severity` 被刻意排除以支持 `changed_severity` 检测）。因此**改写 message 会改变同一问题的 finding id**——批量重写 message 的那次升级，首跑 regression diff 会把旧 id 一次性报成 `resolved`、新 id 报成 `added`（同一真实问题 id 被重置，**非真实状态变化**）。这是 message 改写的已知且认可的一次性副作用，运维侧解读见 [MIGRATION.md](../MIGRATION.md#message-改写与-finding-id-一次性-churn)。

---

## 速查清单（写完 inspector 自查）

- [ ] 所有抽取 / 派生（比率 / 百分比 / 延迟秒）在 `collect.command` 算好，finding 只判标量（规则 1）
- [ ] 跨命令 / 跨行关联在 collector 内完成，`for_each` 单绑定遍历已关联集合（规则 2）
- [ ] 输出顶层键用 `results` / `items` / `records`，不与任何 parameter 同名（规则 3）
- [ ] `parse.format: json` 时顶层是 JSON 对象（`json_build_object` / `jq -s '{...}'`，非裸数组）（规则 4）
- [ ] 版本下限 / PID / `--json` 客户端只在 `description` + `tags` 声明；tag 不含 `+`；Linux-only 依赖已注明（规则 5）
- [ ] 字符串参数 `| sh`、字符串数组 `| map('sh') | join(' ')`；每个字符串参数有 `pattern` / `enum`；secret 走 `$ENV` 不走 `{{ }}`（规则 6）
- [ ] `requires_capabilities` 取自 enum、`requires_binaries` 声明依赖（规则 7）
- [ ] backend 失败时 collector 非零退出 + 空/非-JSON stdout，**不**造 fallback 成功对象；genuine 空集与宕机走不同退出码（规则 8）
- [ ] 外部 CLI 依赖用 `requires_binaries` 门控，**不**用惰性探测的 capability（如 `docker_cli`）当唯一门（规则 9）
- [ ] FindingRule `message` 是简短中文标签 + `{field}` 注入关键数据；注入字段在 `output_schema` 声明（防 `KeyError`）；数组 / 对象类字段先在 collector emit 已 join 的干净串再注入（**禁** raw 数组吐 repr）；无 `see X for details` 空指针、无纯英文长句（规则 10）

---

## 参考

- Tutorial（5 分钟上手）：[inspector-authoring.md](inspector-authoring.md)
- Manifest 字段参考 + 注入防御五件套：[inspectors.md](inspectors.md)
- 设计与裁决（承重墙表 / `hook.py` · `sql_result` 触发条件）：
  [`add-inspector-authoring-contract/design.md`](../../openspec/changes/add-inspector-authoring-contract/design.md)
- 规范（5 条需求）：
  [`inspector-authoring-contract/spec.md`](../../openspec/changes/add-inspector-authoring-contract/specs/inspector-authoring-contract/spec.md)
- message 规约（规则 10）规范：
  [`improve-report-rendering-and-i18n/.../inspector-authoring-contract/spec.md`](../../openspec/changes/improve-report-rendering-and-i18n/specs/inspector-authoring-contract/spec.md)
- 报告渲染新布局示例（抬头 / 覆盖行 / 根因置顶 / 四元组去重 / 多 target 分节 / 健康态）：
  [notify.md §报告渲染示例](notify.md#报告渲染示例新布局)
