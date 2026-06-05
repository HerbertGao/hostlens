## 1. Fixture 录制器（dev-tool，bulk 作者前置）

- [x] 1.1 新增录制器模块（dev-tool / CLI，**不进** Tool Registry）：输入 inspector manifest + 真实 `ExecutionTarget`，复用 runner 的渲染路径产出「完整 preflight 探测命令 + 主 `collect.command`」的命令序列
- [x] 1.2 对每条命令记录 stdout/stderr/退出码，写出 `ReplayTarget` 兼容 JSON（含 capability 声明）；命令字节级与 runner 实际发送一致
- [x] 1.3 secret 脱敏（`secrets_env` 注入值 / token / 密码 / webhook 不落 fixture）+ 时窗/采样冻结（双采样间隔、时间戳确定化）
- [x] 1.4 单测：录制产物经 `ReplayTarget` 回放命中、命令漂移导致回放失败、fixture 内无明文 secret、采样冻结使 snapshot 可重复

## 2. `postgres.bloat_tables`（SQL 型，纯 YAML）

- [x] 2.1 manifest：`collect.command` 用 `psql -tAc "SELECT json_build_object('results', coalesce(json_agg(t),'[]'::json)) FROM (… pg_stat_user_tables 派生 bloat 列 …) t"` 吐**顶层对象**（`parse.format: json` 拒绝顶层数组、要求 dict，见 `parsers/json.py`；顶层键 `results` 同时满足输出键命名约定）；bloat 数值派生全在 SQL 计算列（不进 DSL，承重墙 1）
- [x] 2.2 `requires_binaries: [psql]`、`secrets_env` 注入连接口令；参数（库名/阈值）经 schema `pattern` 收紧 + `| sh` 引用（注入安全三件套）；输出顶层键用 `results`/`records`（防 parameter 遮蔽）
- [x] 2.3 finding 规则：仅对 bloat 比率/死元组阈值做比较；`description`/`tags` 声明 PostgreSQL 版本前提（文档式，承重墙 5）
- [x] 2.4 用录制器产出 fixture + snapshot 测试

## 3. `docker.containers.restart_loop`（容器 JSON 型，纯 YAML）

- [x] 3.1 manifest：`docker inspect` / `docker ps --format json` 取原生 JSON；`for_each` 单绑定 = 容器（跨容器关联在命令内完成，承重墙 2）；复用已有 `docker_cli` capability（不扩 enum）
- [x] 3.2 finding 规则：对 `RestartCount` / `State` / `Health` 阈值判 restart-loop / unhealthy；输出键命名合规
- [x] 3.3 用录制器产出 fixture + snapshot 测试（含「无 unhealthy 容器」空集场景）

## 4. `redis.slowlog`（版本敏感 CLI 型，故意的边界探针）

- [x] 4.1 对真 Redis 6+ 容器实证 `redis-cli --json SLOWLOG GET n` 对嵌套 RESP / 二进制 command-args 的渲染质量（裁决 D-6 的实证步骤）
- [x] 4.2 按实证结果择一落地：(a) `--json` 干净 → 完整 manifest（模式 A）；(b) 二进制 args 不可靠 → 收窄为只报时长+计数（丢原始命令文本）仍纯 YAML + fixture（模式 A）；(c) 必须忠实还原老版本 args → **附再现证据的 defer**（模式 B）——把 task 4.1 真容器 `redis-cli --json SLOWLOG GET` 的实际损坏输出粘进 `design.md` 裁决记录作为首个 `hook.py` 触发证据，**仅贴标签无证据不算通过**
- [x] 4.3 `description`/`tags` 声明 Redis 6 与 `--json` 前提（用 `redis6`/`json-client` tag，**不**写 `redis6+`，tag 正则不含 `+`）；落地分支配 fixture + snapshot 测试，或（defer 分支）以 task 4.2(c) 的再现证据替代

## 5. 《Inspector 作者契约》文档 + 裁决记录

- [x] 5.1 `docs/` 下新增《Inspector 作者契约》：codify 承重墙 1-5 派生的全部规则（全解析在 collector / 单 `for_each` / 输出键命名 / 窄 scope 文档式声明 / Linux-only 声明 / 注入安全三件套 / `requires_*` 约定），并以本期 3 个 inspector 为活例
- [x] 5.2 依 task 4.1 redis 实证结果，把 `design.md` §D-7 的**预期裁决**确认或推翻为**最终裁决**：实证支持→定稿「`hook.py` / `sql_result` 本期 deferred」及未来触发条件；实证落到 D-6 结局 3→推翻预期、记为触发 `hook.py` 的独立提案前置

## 6. 最小文档漂移补丁

- [x] 6.1 按**内容类别**（非死行号）修矛盾（删除或加「superseded by 作者契约」注记），不做广义重写：(i) `sql_result` 当已存在 / 将由 M6 提供 parse format、(ii) 声称 M6 PostgreSQL bloat 需 `hook.py`/`sql_result`、(iii) `hook.py` loader 加载当已实现（**仅** `TODO.md` L144 那条已勾选 M1 项；ARCH/inspectors.md 已写「loader 直接 raise」无此类矛盾）。class (i)/(ii) 覆盖 `docs/ARCHITECTURE.md` + `docs/operations/inspectors.md` + `TODO.md`。**混合行**（ARCHITECTURE L329 / inspectors.md L71「hook.py 留给 M6 复杂场景（PostgreSQL bloat / TLS expiry）」）外科式只删 `PostgreSQL bloat` 例子、保留「留作复杂场景未来选项」框架；**保留** `hook.py` 作未来逃生舱表述（ARCHITECTURE L451 / L1333 / L1412-1413 不动）

## 7. 收尾

- [x] 7.1 `mypy --strict` + `ruff` + 全量 `pytest` 全绿
- [x] 7.2 跑通 Demo Path（`hostlens inspectors show` / `ReplayTarget` 回放 snapshot），确认达成成功模式 A（3 个 fixture+snapshot）或 B（2 个 + redis 附再现证据的 defer）、覆盖矩阵对应单元格更新
