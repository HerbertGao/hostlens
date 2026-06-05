## 为什么

M6 wave-1 铺了 23 个**纯 OS/Linux shell** inspector——零外部服务依赖,fixture 直接从真 host 的 `/proc` 白嫖。wave-2 要进入**中间件/服务域**(nginx / mysql / postgres / redis / docker),性质陡变:多数 inspector 经 client CLI(redis-cli / psql / mysql / curl / docker)够到一个**真服务**(也有走文件/socket/HTTP/systemd 的),引入 wave-1 从未触及的维度——连接参数怎么传、secret 怎么注入(及怎么跨 SSH 到达远端)、服务不可达/认证失败/缺 client 各算什么状态、fixture 必须有真服务才能录。

若不先统一这套**运行契约**就直接铺 17 个 service inspector,最大风险不是"命令跑不通",而是 **17 个 inspector 各搞一套失败语义和连接参数**:CI 全绿、却只在录制环境可靠,失败分类彼此矛盾,Agent 拿到的状态不可信。这正是 M6 spike 当年的教训——先用 3 个最硬用例逼出《作者契约》再铺 wave-1。本提案是 wave-2 的**对应物**:用 **2 个最小真例**逼出「最小公共服务契约」,**先证后铺**。

同时,wave-1 套件 spec 只要求"一份触发 finding 的异常场景 fixture"。对 OS 探针这够了(异常态廉价可真造:僵尸进程/满盘)。但 service 域异常态(replication lag / deadlock)制造昂贵,会诱使作者用「健康服务 + 人为低阈值」凑一份"异常 fixture"——这只证明了阈值比较生效,**没证明 collector 在真实异常下输出正确字段**,是验收钻空子、会放过 no-op inspector。本 spike 一并立**双轨 fixture 契约**堵这个洞。

## 变更内容

- **交付 2 个最小真例 service inspector**(它们是真·铺量项,同时充当契约探针,沿用 M6 spike "用真 inspector 逼契约"的先例):
  - `redis.memory_usage`——代表「**已证 client**(redis-cli)+ 单实例 + secret(`HOSTLENS_REDIS_PASSWORD` remap 到 `REDISCLI_AUTH`)+ 连接发现 + 基础失败分类 + SSH 投递」。复用 wave-1 spike 已证的 redis-cli/env 模式,把变量收敛到"契约本身"。
  - `mysql.connection_usage`——代表「**新 client**(mysql)+ 新 secret(`HOSTLENS_MYSQL_PWD` remap 到 client `MYSQL_PWD`)+ MySQL 单实例契约」。这是 wave-2 唯一未证的 client + secret 路径,必须在铺量前钉死。
- **新立 `service-inspector-contract` capability**:规定**本 spike 起新增/迁移**的 service-dependent inspector 必须遵守的**最小公共运行契约**(向前生效、不回溯绑定 pre-spike seed,见 spec 首条管辖范围需求)——连接参数传入约定、secret env 命名与注入(沿用 wave-1 已证的 `env=secrets_env`,密码经 client 原生 env 通道不进 argv,不引入凭据文件)、service 层失败分类(`requires_unmet`[缺 client·缺声明 secret] / `ok` / `exception`[不可达·认证失败];`timeout`/`target_unreachable` 为正交传输层态,不在本契约收敛范围)、超时与输出纪律、local/SSH 无分叉、stdout/stderr/fixture 脱敏、**双轨 fixture**(finding-trigger + semantic-abnormal)。
- **建立 service fixture 录制 lane**:`docker-compose` 起固定版本真服务 + 每 inspector 轻量 `_record_*.py` 录制入口(沿用 wave-1 录制器),禁固定 `sleep` 竞态,镜像固定 digest/patch 版本。录制不进日常 CI,ReplayTarget 回放必进 CI。
- **明确不立**多实例/拓扑/replication 相关契约(lag 语义、primary/replica 选择、多副本聚合)——留独立 replication spike,避免为未实现的拓扑提前立法。

## 功能 (Capabilities)

### 新增功能
- `service-inspector-contract`: service-dependent inspector 的**最小公共运行契约**——连接参数与 endpoint 传入约定(经 client CLI 时声明 `requires_binaries`,但不强制 CLI 为唯一采集方式)、secret 经 env 注入(声明 `HOSTLENS_` 前缀对齐 ssh-execution-target 契约的 `AcceptEnv HOSTLENS_*`、collector remap 到 client 原生 env、密码不进 argv)、service 层失败分类语义(缺 client / 缺声明 secret → `requires_unmet`;服务不可达 / 认证失败 → `exception`;真实零值 → `ok`;`timeout`/`target_unreachable` 为正交态)、超时与输出纪律、local/SSH 无分叉、回显脱敏、双轨 fixture 验收标准(finding-trigger fixture 验 finding wiring;semantic-abnormal fixture 用真实异常态在**默认阈值**证检出能力——凡 `findings` 非空就**必须**附 semantic-abnormal fixture)。本 capability **引用而非重述**《inspector-authoring-contract》(collector 做派生 / DSL 只比标量 / `for_each` 单绑定 / 输出键防遮蔽 / 注入安全三件套),只补"服务域"这层新维度。具体 inspector 清单(名称/采集手法)是**实现**,列在本 change 的 tasks,由 snapshot 验收(遵守 spike D-9:不为单个 inspector 立行为 spec)。

### 修改功能
- 无。`os-shell-inspector-suite`(wave-1 OS 探针套件)**不修改**——其单异常-fixture 规则对廉价可真造异常态的 OS 探针仍成立;双轨 fixture 是**新增**于 service 域的更强约束,落在新 capability,不回溯收紧已归档的 OS 套件。`inspector-plugin-system` / `inspector-authoring-contract` / `inspector-fixture-recorder` 均不变、仅被引用与使用。

## 影响

- **新增代码**:`src/hostlens/inspectors/builtin/redis/memory_usage.yaml`、`builtin/mysql/connection_usage.yaml`(新建 `builtin/mysql/` 目录)。纯 YAML manifest,无 hook.py。
- **新增测试 / 录制基础设施**:`tests/inspectors/test_redis_memory_usage.py`、`test_mysql_connection_usage.py` + 各自双轨 fixture(`tests/inspectors/fixtures/redis/`、`fixtures/mysql/`)+ `_record_*.py` 录制入口;新增 `tests/inspectors/compose/`(或等价)放录制用 docker-compose(固定版本,CI 不依赖)。
- **对外契约影响**:
  - **Inspector manifest schema**:不变(不增删字段、不扩 parse format、不扩 capability enum)。`HOSTLENS_REDIS_PASSWORD`/`HOSTLENS_MYSQL_PWD` 经现有 `secrets` 字段声明(合 schema secret pattern `^[A-Z_][A-Z0-9_]*$`),走现有 `env=secrets_env` 注入路径。
  - **Inspector registry(对 Agent 可见)**:+2 builtin inspector;Agent 仍只见 `list_inspectors` / `run_inspector`,**工具数组不变**。
  - **不涉及** Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令变更。
- **依赖**:不新增 Python 运行时依赖。`mysql` client 由 preflight `requires_binaries` 探测,缺失→`requires_unmet` skip。

## 非目标 (Non-Goals)

- ❌ **不铺其余 15 个 service inspector**——本 spike 只交付 2 个契约探针,其余进 wave-2a/2b/replication waves(本 spike apply 后另起)。
- ❌ **不立 replication/多实例契约**——lag 语义、primary/replica 拓扑选择、多副本聚合、滞后副本 fixture 录制留独立 replication spike;本 spike 的契约**显式声明其边界止于单实例**。
- ❌ **不引入凭据文件 secret 模型**(`--defaults-extra-file` 等)——service 域沿用已证的 env 注入;"是否全项目从 env 迁移到凭据文件"是独立安全 proposal,不在 wave-2。
- ❌ **不改 SSH target 实现**——本 spike 的两探针**采用**既有 `ssh-execution-target` 契约(spec :120-122)已定的 `HOSTLENS_` 前缀 + `AcceptEnv HOSTLENS_*` 投递路径(secret 声明为 `HOSTLENS_*`、collector 内 remap 到 client 原生 env),不改 SSH target 代码。**迁移既有 seed**(`redis.slowlog`/`postgres.bloat_tables` 仍用非 `HOSTLENS_` 名,与该契约漂移)是独立 follow-up(见 design D-6)。
- ❌ **不新增 parse format / 不改 plugin-system 契约**——两探针均经 collector 内 awk 归一化为 `json`(`parse.format: json`),刻意绕开 `kv`/`table` parser 以收敛变量;"现有 kv/table 是否够用"留后续 wave **实测**裁定,只有实测证伪多 inspector 共同需求才另起 parser proposal。
- ❌ **不动 DockerTarget / docker-py**——docker 域 inspector(后续 wave)继续走 `docker` CLI over shell。
- ❌ **不碰 K8s(M8)/ JVM·Go(6.9 运行时)**。
- ❌ **CI 不依赖真实服务**——全程 ReplayTarget 离线回放;docker-compose 仅录制期手动/thin lane 使用。

## Failure Modes

1. **目标缺 client 二进制**(无 `mysql` / 无 `redis-cli`)→ runner preflight `requires_binaries` 探测失败 → `status=requires_unmet` skip 并标注,不影响同 run 其它 inspector。**契约规定**:缺 client = `requires_unmet`(环境前提不满足),**非** `exception`。
2. **服务不可达 / 认证失败**(conn refused / NOAUTH / Access denied)→ client 非零退出 + 空/非数值 stdout → collector fail-loud `exit 1` → parse 异常 → `status=exception`(诚实)。**契约规定**:服务存在但连不上/认证失败 = `exception`(采集失败),**非** `ok`——杜绝 dead backend 吐空对象被 bless 成"健康"。失败分类边界(`requires_unmet`[缺前提] vs `exception`[采集失败])是本 spike 要钉死的核心契约之一;`timeout`/`target_unreachable` 为正交传输层态、不混入。
3. **secret 被命令回显进 stdout/stderr**(client 报错带连接串/密码)→ 录制器写盘前对 fixture 脱敏;运行期经现有报告脱敏管线。fixture 中**禁止**出现明文密码。
4. **录制环境与真实运行漂移 / 非确定输出**(`used_memory` 随机、`now()` 时间戳)→ 强制用录制器驱动真 runner 录制(字节级匹配命令)+ 冻结非确定值;**禁手写 fixture**。
5. **作者用低阈值凑假异常 fixture** → 双轨 fixture 契约:finding-trigger fixture 只算验 wiring;**凡 manifest `findings` 非空**(客观判据)即**必须**另附在**默认阈值**下触发的 semantic-abnormal fixture(真实异常态,provenance 由人工 review),否则验收不通过。

## Operational Limits

- **并发预算**:不引入新并发;2 个 inspector 在现有 runner 调度内顺序/并行运行。
- **超时设置**:每 manifest 显式 `collect.timeout_seconds`(探针类默认 ≤15s);契约规定 service inspector **必须**显式声明超时,客户端连接超时(如 `redis-cli -t` / `mysql --connect-timeout`)**必须** < `collect.timeout_seconds`,避免连不上时 hang 满超时。
- **输出上限**:契约规定 collector 输出为聚合标量小 JSON(连接数/内存字节),**禁止**回吐高基数结果集(如全部 client 连接明细);需列表时由 collector 截断 + 计数。
- **无 LLM 调用**:纯 Inspector 层。

## Security & Secrets

- **新增 secret**:`HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD`,经现有 `secrets` 字段声明 + `env=secrets_env` 注入,**不进**命令字符串、**不进** fixture/snapshot。`HOSTLENS_` 前缀对齐既有 `ssh-execution-target` 契约(spec :120-122)的 `AcceptEnv HOSTLENS_*` 投递路径;collector 内 remap 到 client 原生 env(`REDISCLI_AUTH` / `MYSQL_PWD`),**不引入新 secret 机制**。
- **脱敏**:录制器对 stdout/stderr 脱敏(回显的密码/连接串打码);运行期经现有 `redact_report_for_render`。
- **密码不进 argv**:redis remap 到 `REDISCLI_AUTH` env、mysql remap 到 `MYSQL_PWD` env(client 隐式读),**绝不** `-a <pwd>`/`-p<pwd>`(全局 `ps` 可见)。
- **SSH 投递(对齐既有契约)**:runner SSH target 经 AsyncSSH `conn.run(env=)` 传 env,受远端 sshd `AcceptEnv` 约束(默认仅 `LANG`/`LC_*`)。既有 `ssh-execution-target` spec(:120-122)已定:Inspector secret 用 `HOSTLENS_` 前缀 + 远端配 `AcceptEnv HOSTLENS_*`。两探针**遵循**此契约。SSH 行为等价按 spec 立场为**结构性**(无 per-inspector 真 SSH 容器测试、CI 在 local 验证);若补 inspector 级 SSH 鉴权成功验证,须用配 `AcceptEnv HOSTLENS_*` 的真 sshd 容器(**非** ssh-target 自测用的 `HOSTLENS_TEST_*`),列可选/follow-up。**发现**:既有 seed slowlog/bloat 用非 `HOSTLENS_` 名、与该契约漂移 → 迁移是独立 follow-up(D-6)。
- **攻击面**:连接参数(host/port/user)经注入安全三件套(`| sh` + `pattern` + 不裸拼)进 shell;`privilege` 默认 `none`;不暴露任意命令执行、不新增 Agent 可见工具。
- **契约硬条款**:secret **只**经 env 通道、不进 argv;**禁止**任何 service inspector 把密码插进 `collect.command` 字符串或经 `{{ }}` 渲染。

## Cost / Quota Impact

- **零 token / 零 API 调用**:纯 Inspector manifest + fixture + snapshot,无 LLM 调用点。
- CI 全程 ReplayTarget 离线回放,不消耗 Anthropic 配额。
- 录制 fixture 时对本地 docker-compose 真服务的一次性采集,无外部计费。

## Demo Path

无 SSH、无付费 API、无真实生产访问的本地复现:

1. `hostlens inspectors list --tag redis`(或 `--tag mysql`)看到 `redis.memory_usage` / `mysql.connection_usage` 已注册、`errors == []`。
2. `hostlens inspectors show mysql.connection_usage` 看 manifest + 内嵌 collector 命令 + secret env 注释 + 失败分类注释。
3. `pytest tests/inspectors -k "memory_usage or connection_usage"` 全绿——经 ReplayTarget 回放双轨 fixture:finding-trigger fixture 断言 finding wiring;semantic-abnormal fixture(真实高内存/高连接率录制)断言产出预期 severity+message。
4. (可选,需 docker)`python tests/inspectors/_record_redis_memory_usage.py` 起 compose 真 redis 重录 fixture,验证录制 lane 可重复、不依赖 sleep 竞态。

## 完整 YAML manifest 示例(契约探针 1:redis.memory_usage)

```yaml
name: redis.memory_usage
version: 1.0.0
# Service-inspector-contract probe: proves the minimal common contract for a
# PROVEN client (redis-cli) + single instance + secret env + failure
# classification. Requires Redis 6+ and a redis-cli supporting INFO.
description: >-
  Redis 内存使用巡检(单实例)。需 Redis 6+ 与 redis-cli;经 HOSTLENS_REDIS_PASSWORD
  env 注入鉴权(collector 内 remap 到 redis-cli 的 REDISCLI_AUTH,从不进 argv;HOSTLENS_
  前缀对齐 ssh-execution-target 契约的 AcceptEnv HOSTLENS_* 投递路径)。只报聚合标量
  (used/max bytes 与派生使用率),不回吐高基数明细。服务不可达/NOAUTH → status=exception
  (诚实);缺 redis-cli → requires_unmet。SSH 上需远端 sshd 配 AcceptEnv HOSTLENS_*。
tags: [redis, redis6, service, memory]
targets: [local, ssh]
requires_capabilities: [shell]
requires_binaries: [redis-cli]
secrets: [HOSTLENS_REDIS_PASSWORD]
privilege: none

parameters:
  type: object
  properties:
    host:
      type: string
      pattern: "^[a-zA-Z0-9._-]+$"   # blocks shell injection in -h value
      default: "127.0.0.1"
    port:
      type: integer
      minimum: 1
      maximum: 65535
      default: 6379
    warn_used_pct:     { type: number, default: 80.0 }
    critical_used_pct: { type: number, default: 95.0 }
  additionalProperties: false

collect:
  # All derivation in the collector (Authoring Contract rule 1): used_pct is
  # computed here, the DSL only threshold-compares the ready scalar. Two INFO
  # fields (used_memory / maxmemory) are folded into one top-level JSON object
  # so parse.format=json sees a dict. FAIL-LOUD (rule 8): redis-cli must
  # succeed AND yield numerics, else exit 1 + empty stdout → status=exception
  # (a dead/NOAUTH backend never fabricates used_pct=0 → "healthy").
  # maxmemory=0 means "no limit": used_pct is then null (the DSL pct rules
  # guard against null), so an unbounded instance reports raw bytes only.
  # Secret declared as HOSTLENS_REDIS_PASSWORD (HOSTLENS_ prefix per the
  # ssh-execution-target contract: only AcceptEnv HOSTLENS_* survives SSH). The
  # collector REMAPS it to redis-cli's native REDISCLI_AUTH env channel — the
  # password NEVER reaches argv (can't leak via `ps`, and spaces/glob chars can't
  # word-split into bogus args the way an unquoted `-a $pwd` would). It is never
  # `{{ }}`-interpolated (contract: secret via env only). The no-auth branch
  # (empty HOSTLENS_REDIS_PASSWORD — exported as "" so preflight's secret-presence
  # gate passes) omits REDISCLI_AUTH entirely. `host` flows through `| sh`; `port` int.
  command: |
    if [ -n "${HOSTLENS_REDIS_PASSWORD:-}" ]; then
      info=$(REDISCLI_AUTH="$HOSTLENS_REDIS_PASSWORD" redis-cli --no-auth-warning -t 5 -h {{ host | sh }} -p {{ port }} INFO memory) || { echo "redis INFO memory failed" >&2; exit 1; }
    else
      info=$(redis-cli -t 5 -h {{ host | sh }} -p {{ port }} INFO memory) || { echo "redis INFO memory failed" >&2; exit 1; }
    fi
    used=$(printf '%s' "$info" | awk -F: '/^used_memory:/{gsub(/\r/,"",$2);print $2}')
    max=$(printf '%s' "$info" | awk -F: '/^maxmemory:/{gsub(/\r/,"",$2);print $2}')
    case "$used" in ''|*[!0-9]*) echo "used_memory non-numeric: $used" >&2; exit 1;; esac
    case "$max"  in ''|*[!0-9]*) echo "maxmemory non-numeric: $max"   >&2; exit 1;; esac
    if [ "$max" -gt 0 ]; then
      pct=$(awk -v u="$used" -v m="$max" 'BEGIN{printf "%.2f", (u/m)*100}')
      printf '{"used_memory":%d,"maxmemory":%d,"used_pct":%s}' "$used" "$max" "$pct"
    else
      printf '{"used_memory":%d,"maxmemory":%d,"used_pct":null}' "$used" "$max"
    fi
  timeout_seconds: 15   # client -t 5 < collect timeout (Operational Limits)

parse:
  format: json

output_schema:
  type: object
  properties:
    used_memory: { type: integer }
    maxmemory:   { type: integer }
    used_pct:    { type: [number, "null"] }
  required: [used_memory, maxmemory, used_pct]
  additionalProperties: false

findings:
  # Aggregate-mode: only scalar threshold comparisons over ready fields.
  # used_pct may be null (maxmemory=0); guard before comparing.
  - when: "used_pct != None and used_pct >= critical_used_pct"
    severity: critical
    message: "Redis memory at {used_pct}% of maxmemory ({used_memory}/{maxmemory} bytes)"
  - when: "used_pct != None and used_pct >= warn_used_pct and used_pct < critical_used_pct"
    severity: warning
    message: "Redis memory at {used_pct}% of maxmemory ({used_memory}/{maxmemory} bytes)"
```

> 第二个探针 `mysql.connection_usage`(新 client + `HOSTLENS_MYSQL_PWD`→`MYSQL_PWD` remap + collector 内归一化为 `json`)的完整 manifest 在 design.md 给出——它承载本 spike 最关键的"未证 client + secret"裁定。
