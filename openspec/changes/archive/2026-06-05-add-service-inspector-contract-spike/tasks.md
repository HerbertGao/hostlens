## 1. 录制 lane 基础设施

- [x] 1.1 建 `tests/inspectors/compose/`（或等价）放录制用 docker-compose：单实例 redis（固定 digest/patch）+ 单实例 mysql（固定 digest/patch），含一个低 `maxmemory` redis 实例与一个低 `max_connections` mysql 实例用于 semantic-abnormal 录制
- [x] 1.2 确认 `inspector-fixture-recorder` 录制器可对带 `secrets`(env 注入)的 manifest 录制并脱敏 stdout/stderr；缺口则在录制入口侧补脱敏，不改录制器契约
- [x] 1.3 约定录制入口用就绪轮询（redis `PING` / mysql `mysqladmin ping`）而非固定 `sleep`，写进 `_record_*.py` 模板

## 2. 探针 1：redis.memory_usage（已证 client，收敛到契约本身）

- [x] 2.1 写 `src/hostlens/inspectors/builtin/redis/memory_usage.yaml`（见 proposal 完整示例：**secret 声明 `HOSTLENS_REDIS_PASSWORD`、collector remap 到 `REDISCLI_AUTH` env 鉴权通道（密码不进 argv）**、awk 取 used_memory/maxmemory、collector 内派生 used_pct、fail-loud + 数值校验、maxmemory=0→used_pct null、client `-t 5` < timeout 15）
- [x] 2.2 `_record_redis_memory_usage.py`：对 compose 真 redis 录 fixture（healthy / conn_refused；无鉴权实例需 `HOSTLENS_REDIS_PASSWORD=` 空串导出）
- [x] 2.3 录 finding-trigger fixture（健康 + 低 warn_used_pct 触发 warning）+ semantic-abnormal fixture（真实逼近 maxmemory 的高占用实例，**默认阈值** used_pct≥95 触发 critical）
- [x] 2.4 `tests/inspectors/test_redis_memory_usage.py`：snapshot 断言——healthy 无 finding、finding-trigger 出 warning、semantic-abnormal 在**默认阈值**出 critical（含 message 语义）、conn_refused→status=exception、缺 redis-cli→requires_unmet、缺 `HOSTLENS_REDIS_PASSWORD` env（连空串都没有）→requires_unmet
- [x] 2.5 含特殊字符密码回归：用含空格/glob 元字符（`p w*d`）的 `HOSTLENS_REDIS_PASSWORD` 录/回放一份成功 fixture，证明 `REDISCLI_AUTH` remap 通道不因词分割误判为认证失败（防 `$AUTH` 无引号展开类回退）

## 3. 探针 2：mysql.connection_usage（新 client + HOSTLENS_MYSQL_PWD→MYSQL_PWD remap + TSV 归一化）

- [x] 3.1 新建 `src/hostlens/inspectors/builtin/mysql/` 目录 + 写 `connection_usage.yaml`（见 design 完整 manifest：secret 声明 `HOSTLENS_MYSQL_PWD`、collector 内 `MYSQL_PWD="${HOSTLENS_MYSQL_PWD:-}" mysql ...` remap、绝不 `-p<pwd>`、**used_connections 取 `SHOW GLOBAL STATUS LIKE 'Threads_connected'`（全局、免 PROCESS 权限，非 `information_schema.processlist`）**、command-sub 取 raw 再单独 awk（避开 pipeline-exit 陷阱）、collector 内 awk 派生 + printf JSON、fail-loud + 数值校验、`--connect-timeout=5` < timeout 15、host/user 经 `| sh`+pattern）
- [x] 3.2 实测裁定 D-2 + D-1：(a) 对真 mysql 验证「collector 内归一化为 JSON」路径成立、本探针不依赖 table parser，table parser 对 `\N`/转义的观察记入 design D-2 注记（供后续 wave，非阻塞）；(b) 对 5.7+/8 录制环境实测 `MYSQL_PWD` env 鉴权仍生效（D-1 forward-fragility 注记）
- [x] 3.3 `_record_mysql_connection_usage.py`：对 compose 真 mysql 录 fixture（healthy / access_denied（录制前提：`HOSTLENS_MYSQL_PWD` 须设**错值**而非不设，否则 preflight requires_unmet 录不到 Access denied）/ conn_refused）
- [x] 3.4 录 finding-trigger fixture（健康 + 低 warn 触发 warning）+ semantic-abnormal fixture（低 max_connections 实例 + 多保持连接造**真实**高连接率，**默认阈值** used_pct 超阈触发 critical）
- [x] 3.5 `tests/inspectors/test_mysql_connection_usage.py`：snapshot 断言——healthy 无 finding、finding-trigger 出 warning、semantic-abnormal 在**默认阈值**出 critical、access_denied/conn_refused→status=exception、缺 mysql→requires_unmet、缺 `HOSTLENS_MYSQL_PWD` env→requires_unmet；并加一例低权限用户断言 `Threads_connected` 仍返回全局值（防 processlist 类计数失真回退）

## 4. 契约固化与跨探针验收

- [x] 4.1 用两个探针交叉验证 `service-inspector-contract` spec 每条需求：连接注入安全 / secret 经 env 通道不进 argv 与 fixture / service 层失败分类（requires_unmet[缺 client·缺声明 secret] vs exception[服务不可达·认证失败] vs ok[含真实零值]，并确认 timeout/target_unreachable 为正交态不混入）/ 超时与输出纪律 / 跨 target 无分叉 / 双轨 fixture
- [x] 4.2 注入安全回归：对 host/user 参数（两探针的可注入位）跑注入 payload（`'; whoami; #` / `$(curl evil)`）验证渲染转义正确、loader 拒绝未走 `| sh` 的 string 参数（dbname 注入回归留给后续含 dbname 的 wave inspector——本 spike 两探针参数集无 dbname）
- [x] 4.3 secret 不泄漏回归：断言 fixture 的 stdout/stderr 不含明文 `HOSTLENS_REDIS_PASSWORD`/`HOSTLENS_MYSQL_PWD` 值；ReplayTarget 命令匹配不含 env；并静态断言两 manifest 命令文本不含 `-p`/`-a` 形式的 argv 明文密码
- [x] 4.4 跨 target 无分叉静态断言：检视两 manifest 的 collector 命令无按 `target.type` 分叉的分支（满足"跨 local/SSH 无分叉"需求的可机械核验形态）
- [x] 4.5 扩 `tests/inspectors/test_builtin_inspectors.py`：断言 2 个新 inspector 干净注册、registry `errors == []`
- [x] 4.6 扩 `tests/inspectors/test_builtin_capability_gate.py`：断言缺 client 二进制→requires_unmet skip、**缺已声明 secret env→requires_unmet skip**（均不报错中断同 run）
- [x] 4.7 SSH secret 投递遵循既有 ssh 契约（D-6）：两探针 secret 用 `HOSTLENS_` 前缀（对齐 `ssh-execution-target` spec :120-122 的 `AcceptEnv HOSTLENS_*` + `HOSTLENS_` 前缀路径），manifest 注释 + 运行文档声明"SSH 上需远端 sshd 配 `AcceptEnv HOSTLENS_*`";确认未配时为诚实 `exception` 非假健康。SSH 行为等价按 spec 立场为**结构性**（无 per-inspector 真 SSH 容器测试，与 wave-1 同口径），CI 在 local 验证；**若**要补 inspector 级 SSH 鉴权成功验证，须用配 `AcceptEnv HOSTLENS_*`（**非** `HOSTLENS_TEST_*`——后者放不行 `HOSTLENS_REDIS_PASSWORD`/`HOSTLENS_MYSQL_PWD`）的真 sshd 容器，列为可选/follow-up。**不**改 SSH target 代码
- [x] 4.8 登记 follow-up：既有 seed `redis.slowlog`（`REDIS_PASSWORD`）/ `postgres.bloat_tables`（`PGPASSWORD`）用非 `HOSTLENS_` 名、与 ssh 契约 :120-122 漂移 → 在 SSH+`AcceptEnv HOSTLENS_*` 远端会丢 secret；本 spike **不**迁移它们（独立 follow-up），但在 PR 描述/openspec 登记此发现供后续处理

## 5. 收尾

- [x] 5.1 确认零对外契约变更：manifest schema / capability enum / parse format / Agent 工具数组（`list_inspectors`+`run_inspector`）均未变；无新 Python 依赖（mysql client 走 requires_binaries preflight）
- [x] 5.2 `mypy --strict` + `ruff` + 全量 `pytest`（默认 replay 模式，不消耗 API）全绿
- [x] 5.3 跑 Demo Path：`hostlens inspectors list --tag redis`/`--tag mysql` 看注册、`hostlens inspectors show mysql.connection_usage` 看 manifest + 注释、`pytest -k "memory_usage or connection_usage"` 全绿
- [x] 5.4 对本次变更跑对抗性 review（`/review-loop-codex`），triage + 修复到放行（本 spike 含安全边界=secret/失败分类，属应 review 类）
- [x] 5.5 开 feature branch `feat/add-service-inspector-contract-spike` → commit → push → `\gh pr create --base main`，CI 绿后 squash-merge
- [x] 5.6 归档：`openspec-cn archive add-service-inspector-contract-spike`，delta 合入 `openspec/specs/service-inspector-contract/`
