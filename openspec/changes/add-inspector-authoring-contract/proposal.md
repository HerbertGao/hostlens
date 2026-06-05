## 为什么

M6 要把 builtin inspector 从现有 13 个扩到 **≥40 个**、覆盖 ~20 个故障域（见 `TODO.md` §M6）。但在 bulk 铺量之前，有一个**未经证明的架构假设**横在前面：

> 复杂 inspector（数据库 / 容器 / 服务运行时）能否在**不 enable `hook.py`、不新增 `sql_result` parse format** 的前提下，纯靠「把复杂度压进 collector 命令、吐 JSON、用现有 `json` format 解析」写出来？

如果这个假设成立，M6 余下的 ~27 个 inspector 是机械铺量；如果不成立，先得补基础设施。直接开写一堆 YAML 会把这个风险埋进 checklist，写到第 N 个 PostgreSQL inspector 才发现要回头改 schema。

本提案是一个**作者契约 spike**：在最硬的几个用例上把假设证伪/证实，产出一份可复用的《Inspector 作者契约》与一个 fixture 录制器，把「要不要 `hook.py` / `sql_result`」这个裁决用证据钉死，再定 wave-2 形状。

四条承重墙已在代码核实，构成契约的事实基础：

1. Finding DSL 白名单仅 `len/sum/min/max/any/all/now/float/int`（`inspectors/dsl.py`）——无 string/split/regex ⇒ **一切抽取与数值派生必须在 collector 命令里做**，不能在 finding 规则里做。
2. `for_each` 仅单绑定 `"<expr> as <var>"`（`inspectors/dsl.py`）⇒ 跨命令 / 跨行关联必须在 collector 里做。
3. finding 上下文里 `output` 键会被同名 `parameter` 遮蔽（现有 `net/tls_cert_expiry.yaml` 注释踩过）⇒ 必须强制输出键命名约定。
4. `ReplayTarget`（`targets/replay.py`）已存在 ⇒ snapshot/fixture 回放地基已具备，可在其上建录制器。

## 变更内容

- 新增 **2–3 个「最硬」builtin inspector** 作为模式证明（纯 YAML，无 `hook.py`/`sql_result`；尝试 3 个，模式 B 下 redis 以附再现证据的 defer 收尾、不落地）：
  - `postgres.bloat_tables` —— `psql -tAc "SELECT json_build_object('results', coalesce(json_agg(t),'[]'::json)) FROM (… 派生 bloat 列 …) t"` 吐**顶层对象**（`parse.format: json` 要求顶层为对象、拒绝顶层数组，见 `parsers/json.py`；`results` 键同时满足输出键命名约定），bloat 数值派生写进 SQL 计算列；用 `json_build_object`/`json_agg` 而非新增 `sql_result` format。
  - `docker.containers.restart_loop` —— `docker inspect` / `docker ps --format json` 原生 JSON；复用已有 `docker_cli` capability；`for_each` 单绑定 = 容器。
  - `redis.slowlog`（scoped Redis 6+ `--json`）—— **故意纳入最可能撞墙的用例**。允许结论为「证据驱动 defer」：若 `--json` 在真容器上无法干净渲染二进制 command-args，退路是只报时长+计数（丢原始命令文本）仍纯 YAML 成立，或明确记录此场景 defer 到未来 `hook.py` 提案。「找到边界」是合法且成功的 spike 结局。
- 新增 **fixture 录制器 dev-tool**：对真实 target 渲染并执行完整 preflight 探测序列 + 主命令，冻结时窗采样，脱敏 secret，一步写出 `ReplayTarget` 兼容的 JSON fixture（含 binary-probe 结果与 capability 声明）。
- 新增 **《Inspector 作者契约》文档**（`docs/`）：codify 上述四条承重墙派生的编写纪律 + 窄 scope 声明纪律。
- **裁决记录**：`hook.py` 与 `sql_result` 本期 deferred 的**预期**依据（待 redis 实证经 task 5.2 定稿）+ 触发它们的未来条件（写入 `design.md`，定 wave-2 形状）。
- **最小文档漂移补丁**：按**内容类别**（非死行号）修正 SOT 中的直接矛盾，不广义重写——(i) 把 `sql_result` 当**已存在 / 将由 M6 提供**的 parse format（实际只有 raw/table/json/kv，本提案也不加）；(ii) 声称「M6 PostgreSQL bloat 需 `hook.py` / `sql_result`」（postgres 半已证不需）；(iii) 把 `hook.py` loader 加载当**已实现**（本类仅命中 `TODO.md` L144 那条已勾选 `[x]` 的 M1 项——实际 loader 零 hook 支持；`ARCHITECTURE.md` / `inspectors.md` 对应处已正确写「写了 loader 直接 raise」、无此类矛盾）。class (i)/(ii) 覆盖 `docs/ARCHITECTURE.md` / `docs/operations/inspectors.md` / `TODO.md`。**混合行**（把 `hook.py` 留作复杂场景**未来选项**、并以 `PostgreSQL bloat` 为例子之一的，如 ARCHITECTURE L329 / inspectors.md L71「留给 M6 复杂场景（PostgreSQL bloat / TLS expiry）」）须**外科式**只删 `PostgreSQL bloat` 这个例子（class ii）、保留「`hook.py` 留作复杂场景未来选项（TLS expiry 例）」框架（措辞可由 M6 改为「未来独立提案」）。**保留** `hook.py` 作未来逃生舱 / 未来独立提案 的所有表述（如 ARCHITECTURE L451「raw 时由可选 hook.py 自定义解析」、L1333、L1412-1413——本提案保留 hook.py 作未来 broad 场景选项，那些仍成立）。不做广义文档重写。

## 功能 (Capabilities)

### 新增功能
- `inspector-authoring-contract`: 规范化 inspector 编写契约——全解析在 collector / 单 `for_each` / 输出键命名（`results`/`items`/`records` 防 parameter 遮蔽）/ 窄 scope 声明（版本下限·要求传 PID·要求 `--json` 客户端，仅能在 description·tags·契约里声明，因 schema 无 `min_binary_version` 字段；tag 正则 `^[a-z][a-z0-9_-]*$` 不含 `+`，故用 `redis6`/`json-client` 而非 `redis6+`）/ Linux-only 声明模式（如 GNU `date -d`）/ `| sh`（shlex.quote）注入安全 + 参数 regex 校验 / `requires_binaries`·`requires_capabilities` 约定；并规定「模式证明」验收为**二选一成功模式**：**(A)** 3 个跨数据形态（SQL/容器/版本敏感 CLI）硬 inspector 各有 `ReplayTarget` fixture + snapshot；或 **(B)** 2 个落地（fixture+snapshot）+ 第 3 个（redis）以**附具体再现证据的 defer** 收尾——须粘出真容器上 `redis-cli --json SLOWLOG GET` 的实际损坏输出作为首个 `hook.py` 触发证据，**仅贴「defer」标签不算成功**。
- `inspector-fixture-recorder`: fixture 录制器工具契约——录制完整 preflight + 主命令、产出 `ReplayTarget` 兼容 JSON、secret 脱敏、时窗采样冻结、命令字节级匹配。

### 修改功能
- 无 spec 级需求变更。最小文档补丁作用对象是 `TODO.md` / `docs/ARCHITECTURE.md`（非 spec）；`inspector-plugin-system` 规范已在其「目的」声明「不含 `hook.py` / `sql_result`（留给后续 milestone）」，本提案不改其契约（**证明无需改 schema 正是 spike 论点之一**）。

## 非目标 (Non-Goals)

- **不 enable `hook.py`**（Python 逃生舱）；**不新增 `sql_result` parse format`**——本 spike 的核心论点就是证明它们本期不必要。
- **不铺 wave-1 批量 inspector**（compute / mem / disk / net / systemd 等域留后续提案）——本期只尝试 3 个（模式 B 下落地 2 个），作模式证明而非覆盖。
- **不扩 capability enum**（现为 `{shell, file_read, ssh, systemd, docker_cli}`）。
- **不给 manifest schema 加 `min_binary_version` 字段**——redis 6+ 等版本门只靠 description/tags/契约**文档式声明**。
- **不做 `list_inspectors` 的 domain/tags 过滤参数**——40 个 inspector 时 Agent 选择上下文会膨胀，但留到 wave-3 前另起独立提案。
- **不做 `ARCHITECTURE.md` / `TODO.md` / `inspectors.md` 广义刷新**——只动 §变更内容 枚举的那三类（i/ii/iii）直接矛盾，保留 `hook.py` 作未来逃生舱 的表述。
- **不支持 broad 版本无关的 redis / jvm**（jstat 跨 JDK 列漂移、自动发现所有 JVM、忠实还原任意老 redis command-args）——那是未来触发 `hook.py` 的独立提案。

## 对外契约影响

- **Inspector manifest schema**：不变。本提案不增删 manifest 字段、不扩 parse format、不扩 capability enum——「落地的硬 inspector（pg+docker，模式 A 含 redis）全部 load+run 在现有 M1 字段集内」即论据。
- **Inspector registry**（对 Agent 可见）：扩 **2–3** 个 builtin inspector——`postgres.bloat_tables` + `docker.containers.restart_loop` 必落；`redis.slowlog` 在成功模式 A 落地、模式 B（附再现证据的 defer）则不落（见 §退出条件）。Agent 仍只见 `list_inspectors` / `run_inspector` 两个工具，工具数组不变。
- **新增 dev-tool**（fixture 录制器）：开发期工具/CLI，**非 Agent ToolSpec**（不进 Tool Registry，CLAUDE.md §4.10 规则）。
- **不涉及** Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest schema 变更。

## 退出条件

- 达成成功模式 A 或 B（见 §功能 的「模式证明」验收）：模式 B 下 redis 的 defer **必须**附真容器实证再现，不接受仅贴「defer」标签。
- 录制器能对真实 target 一步产出合规 fixture（含 preflight 探测 + capability 声明）。
- 《Inspector 作者契约》文档落地，覆盖四条承重墙 + 窄 scope 纪律。
- 裁决记录写入 `design.md`：postgres + docker 已证零新 infra；`hook.py` / `sql_result` 的「本期 deferred」**预期**待 redis 实证（task 4.1）经 task 5.2 定稿——实证若推翻（redis 落 D-6 结局 3）则改记为 `hook.py` 的未来触发条件。
- 最小文档补丁让 SOT（`TODO.md` / `ARCHITECTURE.md` / `inspectors.md`）不再把 `sql_result` 当已存在 format、不再声称 M6 PostgreSQL 需 `hook.py`/`sql_result`。
- `mypy --strict` + `ruff` + 全量 `pytest` 全绿。

## Demo Path

无真实数据库 / 容器、无付费 API 的本地复现：

1. `hostlens inspect localhost --inspector docker.containers.restart_loop`（本机有 docker 时）跑通容器健康巡检；或用录制好的 fixture 经 `ReplayTarget` 回放 → snapshot 出 `InspectorResult`。
2. `hostlens inspectors show postgres.bloat_tables` 看 manifest + 内嵌 SQL；fixture 回放断言解析后的 bloat 行。
3. CI 全程走 `ReplayTarget` 回放固定 fixture，不依赖网络 / 真实 DB / 真实容器；真实采集仅在录制 fixture 时一次性进行（thin integration lane）。
