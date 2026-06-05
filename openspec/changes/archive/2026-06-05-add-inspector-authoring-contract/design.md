## 上下文

M5 收口后进入 M6（builtin inspector 库扩到 ≥40，见 `TODO.md` §M6）。现有 13 个 inspector 集中在 Linux 基础域，最复杂的 `net/tls_cert_expiry.yaml` 已经证明：**openssl 握手 + 日期算术 + JSON 拼装全在 shell 里做、DSL 只做整数比较**这条路可行，且自带 shell 注入防护（`| sh` shlex.quote + 参数 regex）。

M6 的硬域（DB / 容器 / 服务运行时）是否也能沿用这条路、不碰 `hook.py` 与 `sql_result`，是 bulk 铺量前必须回答的架构问题。本设计先把模式与裁决固化，再让 wave-1/2/3 机械落地。

承重墙（代码核实，约束本设计）：

| # | 约束 | 出处 | 推论 |
|---|---|---|---|
| 1 | DSL 白名单仅 `len/sum/min/max/any/all/now/float/int`，禁 string/split/regex/comprehension | `inspectors/dsl.py` | 抽取与数值派生**只能在 collector** |
| 2 | `for_each` 单绑定 `"<expr> as <var>"` | `inspectors/dsl.py` | 跨命令/跨行关联**只能在 collector** |
| 3 | finding 上下文 `output` 键被同名 `parameter` 遮蔽 | `net/tls_cert_expiry.yaml` 注释 | 强制输出键命名约定 |
| 4 | `ReplayTarget` 已存在、命令字节级匹配、capability 投影 | `targets/replay.py` | 录制器可在其上建，无需新回放层 |
| 5 | manifest `extra="forbid"`，无 `min_binary_version` / `hook` / `sql_result` 字段 | `inspectors/schema.py` | 版本门只能文档式声明；新字段会被拒 |

## 目标 / 非目标

**目标：**

- 用 3 个跨数据形态的硬 inspector（SQL / 容器 JSON / 版本敏感 CLI；redis 可能以附证据的 defer 收尾）证明 shell+json+窄scope 模式无需新 infra。
- 产出可复用的《Inspector 作者契约》与 fixture 录制器，把 wave-1+ 变成机械工作。
- 用证据钉死「`hook.py` / `sql_result` 本期是否必要」的裁决，定 wave-2 形状。

**非目标：** 见 `proposal.md` §非目标（不 enable hook.py / 不加 sql_result / 不铺 wave-1 / 不扩 capability enum / 不加 min_binary_version / 不做 list_inspectors 过滤 / 不广义改文档 / 不支持 broad 版本无关 redis·jvm）。

## 决策

### D-1 切法选 infra-risk-layered 而非 domain-slicing
TODO 的默认是「按故障域一个 sub-proposal」。但域切法**最大化 checklist 位移、隐藏架构风险**——直到写到第 N 个硬 inspector 才发现要补 infra。spike-first 把不确定性前置：先在最硬用例证明模式，再分波铺量。简历叙事上「我设计了扩展模式并在最硬条件下证明它」也强于「我写了 28 个 YAML」。

### D-2 模式 = 「复杂度压进 collector，吐 JSON，DSL 只判」
由承重墙 1/2 强制。每个 collector 命令必须输出**已关联、已派生**的 JSON（SQL 计算列 / `jq` / shell 算术），finding 规则只对标量做阈值比较。这不是临时技巧，是契约的一等规则。

### D-3 窄 scope 用文档声明、不用 schema 机器门
承重墙 5：manifest 无 `min_binary_version`，加了会被 `extra="forbid"` 拒。故 redis 6+ / MySQL 8.0+ / 指定 PID / `--json` 客户端这些前提**只在 description·tags·契约里声明**。代价：不满足时在 preflight 以 `command not found` 之类失败，而非结构化「版本不匹配」。spike 接受此代价；机器式版本门是未来工作（见风险）。

### D-4 输出键命名约定：`results` / `items` / `records`
承重墙 3。契约强制 collector 顶层输出键用这三者之一，杜绝与 parameter 同名导致的静默遮蔽（`tls_cert_expiry` 已用 `results` 规避）。

### D-5 fixture 录制器是 bulk 作者的真前置
手写 fixture 必然与 runner 实际发送的命令漂移（漏 preflight 探测、时窗未冻结、secret 泄漏）。录制器对真实 target 渲染并执行**完整 preflight 探测序列 + 主命令**，冻结时窗采样，脱敏 secret，一步写出 `ReplayTarget` 兼容 JSON（含 binary-probe 结果 + capability 声明，承重墙 4）。1 天工具省 40 天 fixture 漂移调试。它是 dev-tool，非 Agent ToolSpec。

### D-6 `redis.slowlog` 是故意的边界探针
spike 的价值是主动找墙，故纳入最可能撞墙的用例。`SLOWLOG GET` 是嵌套 RESP，`redis-cli --json`（Redis 6+）能否干净渲染二进制 command-args 须对真容器实证。三种合法结局，均算 spike 成功：
1. `--json` 干净 → 纯 YAML，完整还原。
2. `--json` 对二进制 args 不可靠 → **退到只报时长+计数**（丢原始命令文本）仍纯 YAML。
3. 必须忠实还原老 redis 任意 args → **明确 defer**，记为首个真正的 `hook.py` 候选。
「找到边界」比「挑软柿子证明」诚实且更可信。

### D-7 裁决（已实证定稿，task 5.2）

redis 真容器实证（task 4.1）已完成,预期裁决**确认成立**（非推翻）：**M6 可零新 infra 推进——本提案不 enable `hook.py`、不加 `sql_result`**。三个硬 inspector 全部落地为纯 YAML（**Mode A**），逐个佐证：

- `postgres.bloat_tables`：`psql -tAc "SELECT json_build_object('results', coalesce(json_agg(t),'[]'::json)) FROM (… 派生 bloat 列 …) t"` 吐**顶层对象**（`parse.format: json` 拒绝顶层数组、要求 dict，见 `parsers/json.py`；`results` 键满足输出键命名），`json_build_object`/`json_agg` 绕开 `sql_result`，数值派生在 SQL 计算列。**已落地 + 真 postgres:16 录 fixture + snapshot 测试**。→ 纯 YAML。
- `docker.containers.restart_loop`：`docker ps -aq | xargs docker inspect | jq` 包成顶层 `{results:[...]}`，`RestartCount`/`State`/`Health` 一等字段，`docker_cli` 能力已在 enum。**已落地 + 真 docker 录 3 fixture（loop/unhealthy/empty）+ snapshot**。→ 纯 YAML。
- `redis.slowlog`（**边界探针，落 Mode A 分支 b = metrics-only**）：实证见下方证据——`redis-cli --json SLOWLOG GET` 对二进制 command-args 产**非法 UTF-8**、`json.loads` 崩；故落地为 **metrics-only**（`SLOWLOG LEN` + server-side `EVAL` 折叠 `max_micros`，绝不回显 command-args），即使 slowlog 含 binary 慢查询 stdout 仍是干净 JSON。**已落地 + 真 redis:7 录 fixture + snapshot**。→ 纯 YAML。
- `mysql.replication_lag`（未实现，仅佐证）：`performance_schema.replication_*` + `JSON_OBJECT()`；墙是语义，非 infra。→ 窄 scope 纯 YAML。

**首个 `hook.py` 触发证据（task 4.1 真容器实测）**：忠实还原二进制 command-args 这一**特定子场景**纯 YAML 不可行——
```
# Redis 7.4.9: SET binkey $'\x00\x01\xff\xfe binary'  写入 slowlog 后
$ redis-cli --json SLOWLOG GET 5
[[2,...,["SET","binkey","\xff\xfe binary"],...],...]   # \xff\xfe 为裸字节
$ ... | python -c 'json.loads(...)'
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 121: invalid start byte
```
即 `--json` 对干净 args 正确转义（`\"` `\t` ``），但对原始非 UTF-8 字节直接吐裸字节、破坏 JSON。metrics-only 路径（丢 args 数组）实测在同一含 binary 慢查询的 slowlog 上仍产干净 `{"count":N,"max_micros":M}`。

**`hook.py` / `sql_result` 的未来触发条件**（写明以免被当永久弃用）：当某 inspector**必须**支持 broad 版本无关解析、或**忠实还原任意二进制/多格式文本**（↑ 老 redis/任意 redis 的 command-args 全文、自动发现并归类所有 JVM、跨 JDK jstat 列漂移归一）时，由**独立提案** enable `hook.py`；`sql_result` 仅在出现「DB 输出无法经 client flag 转 JSON/CSV」的真实场景时才考虑——本期三个 DB/容器/CLI inspector 均未触发。

### D-8 最小文档补丁，不广义重写
按**内容类别**（非死行号）修矛盾、不广义重写：(i) 把 `sql_result` 当**已存在 / 将由 M6 提供**的 parse format（实际只有 raw/table/json/kv）；(ii) 声称「M6 PostgreSQL bloat 需 `hook.py`/`sql_result`」（postgres 半已证不需）；(iii) 把 `hook.py` loader 加载当**已实现**（本类仅命中 `TODO.md` L144 那条已勾选 M1 项，实际 loader 零 hook 支持）。class (i)/(ii) 覆盖面 = `docs/ARCHITECTURE.md` + `docs/operations/inspectors.md` + `TODO.md`，删除或加「superseded by 作者契约」注记。**混合行**（如 ARCHITECTURE L329 / inspectors.md L71「`hook.py` 留给 M6 复杂场景（PostgreSQL bloat / TLS expiry）」，把 hook.py 留作复杂场景未来选项、并以 postgres bloat 为例之一）须**外科式**只删 `PostgreSQL bloat` 例子（class ii）、保留「hook.py 留作复杂场景未来选项」框架。**保留** `hook.py` 作未来逃生舱的表述（ARCHITECTURE L451「raw 时由可选 hook.py 自定义解析」、L1333、L1412-1413 等仍成立、不动）——发契约说「本提案无 hook.py/sql_result」与「hook.py 留作未来逃生舱」并不矛盾。广义文档刷新另起 chore。

### D-9 不给单个 inspector 立 spec
与现有 builtin（`tls_cert_expiry` 等无 per-inspector spec）一致：这几个 inspector 是**实现**，由 snapshot 测试验收；规范层只有 `inspector-authoring-contract`（编写规则）与 `inspector-fixture-recorder`（工具契约）两个新 capability。`inspector-plugin-system` 不改（它已声明 sql_result/hook 延后）。

## 风险 / 权衡

- **窄 scope 靠文档而非 schema**（D-3）：不满足前提时 preflight 报 `command not found` 而非结构化版本不匹配，运维体验差。**缓解**：契约要求 description 明写版本下限；`min_binary_version` + 结构化 capability mismatch 列为 wave-2 前的独立基础设施提案。
- **`redis.slowlog` 可能 defer**（D-6）：使「3 个全证」的故事变成「2 证 + 1 找到边界」。**取舍**：诚实优先，已在 proposal/contract 把 defer 定义为成功结局。
- **契约是 prose 纪律、非机器强制**：作者可能违反输出键命名 / 在 DSL 里试图派生。**缓解**：契约配可执行反例 + snapshot 测试兜底；未来可加 manifest lint（如校验 finding 表达式不含禁用构造、输出键命名）——列为后续，不在本 spike。
- **录制器需真容器/真 DB 一次性采集**：CI 不能每次 spin 全部。**缓解**：thin integration lane（docker-compose，push-to-main / nightly）只在 manifest 变化时重录 fixture；日常 CI 全走 `ReplayTarget` 回放。
- **文档补丁与广义漂移的边界**（D-8）：只动直接矛盾行，可能仍留间接过时表述。**取舍**：spike 不背广义文档债，保 scope 干净。
