## 上下文

wave-1 把 inspector 作者契约在**零外部依赖的 OS 探针**上验满了。wave-2 进中间件域,引入一整层 wave-1 没有的运行维度:连接、认证、服务存活分类、真服务才能录的 fixture。M6 spike 的成功范式是"**先用最硬真例逼出契约,再机械铺量**"。本 spike 复刻该范式:不直接铺 17 个 service inspector,而是用 **2 个最小真例**把「最小公共服务契约」逼出来并固化为 `service-inspector-contract` spec,后续 wave-2a/2b 坐在其上铺量。

两个探针的选择是刻意的**变量隔离**:
- `redis.memory_usage` —— client(redis-cli)、env 鉴权模式均为 wave-1 spike 已证(secret 声明为 `HOSTLENS_REDIS_PASSWORD` remap 到 `REDISCLI_AUTH`),变量收敛到"契约本身"(service 层失败分类边界、双轨 fixture、超时/输出约定、SSH 投递)。
- `mysql.connection_usage` —— 引入**唯一未证的 client(mysql)+ secret(`HOSTLENS_MYSQL_PWD` remap 到 client `MYSQL_PWD`)+ TSV 输出归一化**。这是 wave-2 secret/parser 两个开放决策的裁判台。

与 Codex 的两轮对抗性讨论把三个原本悬而未决的决策钉死了(见下 Decisions),本 design 把结论固化。

## 目标 / 非目标

**目标:**
- 用 2 个真·可交付 inspector 逼出并固化 service-inspector-contract(连接 / secret / service 层失败分类 / 超时输出 / local-SSH 无分叉 + SSH secret 投递前提 / 脱敏 / 双轨 fixture)。
- **实测裁定** mysql secret 模型(env vs 凭据文件)与 parser 是否零新增——用真服务输出,不靠推测。
- 建立可重复、不依赖 sleep 竞态的 service fixture 录制 lane(docker-compose + 录制入口)。

**非目标:**
- 不铺其余 15 个 service inspector(wave-2a/2b)。
- 不立多实例 / replication 契约(独立 replication spike)。
- 不引入凭据文件 secret 机制、不新增 parse format、不改 plugin-system 契约、不动 DockerTarget。

## 决策

### D-1:secret 沿用 env 注入,mysql 经 client 原生 `MYSQL_PWD`,不引凭据文件

**选 env 注入,否决 `--defaults-extra-file`。** 两层命名(见 D-6):manifest **声明** `HOSTLENS_MYSQL_PWD`(HOSTLENS_ 前缀走 SSH AcceptEnv),collector 内 **remap** 到 client 原生 `MYSQL_PWD`。本决策讨论的"env vs 凭据文件"针对 client 原生 env 通道(`MYSQL_PWD` / `PGPASSWORD` / `REDISCLI_AUTH`)。

与 Codex 两轮对抗后的结论:client 原生 `MYSQL_PWD` / `PGPASSWORD` / `REDIS_PASSWORD` 的泄露面**同构**——都可被有权读目标进程环境者获取、被子进程继承、可能因错误诊断泄露,都依赖 runner 正确隔离 + 录制器脱敏。mysql 文档对 `MYSQL_PWD` 的安全警告,并不构成它相对另两个 env 变量**新增**了技术泄露通道(pg 文档对 `PGPASSWORD` 有同类警告)。

反观 `--defaults-extra-file` 会**新增**一整套远端文件生命周期契约:SSH 远端 `mktemp` + `chmod 0600` + 命令跑完(含超时/断连/进程被杀)可靠 `rm` + 文件路径不进命令字符串/日志 + 并发执行隔离 + runner 须持远端写权限。这是比 env 注入复杂一个量级的**新 infra**,而 env 注入已过 review 归档。

**裁定**:本 spike 沿用 env 注入,`MYSQL_PWD` 加入既有模式;wave-2 后续批次**默认**沿用此模式,但**非不可变法令**——见下风险注记。"是否全项目从 env 迁移到凭据文件"是**独立安全 proposal**,不在 wave-2。

> **风险注记(forward-fragility)**:MySQL 官方文档把 `MYSQL_PWD` 标为"extremely insecure"且长期不建议使用,未来版本存在**移除**可能。本 spike 接受此风险,理由:(a)其泄露面与已归档的 `PGPASSWORD`/`REDIS_PASSWORD` 同构(上文);(b)替代的凭据文件路径是更重的新 infra(下段)。**缓解**:若某支持版本移除 `MYSQL_PWD`,则触发"凭据机制"独立 proposal(候选 `--login-path` / mysql_config_editor 加密文件 / `--defaults-extra-file`),届时统一迁移 mysql 系 inspector;在此之前 `MYSQL_PWD` 是本 spike 与 wave-2 的**当前**方案、非永久承诺。tasks 须对 5.7+/8 录制环境实测 `MYSQL_PWD` 仍生效。

> 替代方案 `--defaults-extra-file`:被否决,理由如上(新远端文件生命周期契约,收益不抵成本)。

### D-2:parser 零新增是默认路线,实测证伪才动 plugin-system 契约

**两个探针均经 collector 内 awk 归一化为 JSON(`parse.format: json`),不新增 parse format,也不实际依赖 kv/table parser。** `kv`/`table` 是"考虑过、可选"的现有 parser,本 spike 两探针刻意绕开它们(把归一化压进 shell)以收敛变量;它们对真实 redis INFO / mysql TSV 是否够用,留后续 wave 实测裁定。

`os-shell-inspector-suite` 把"禁新增 parse format"立为硬约束,且这是项目"纯铺量无需新 infra"论点的支柱。新增 parse format = 改 `inspector-plugin-system` 契约(影响 loader/schema/所有 adapter),远超铺量范畴。

- **redis INFO**:`INFO memory` 第一层是规范 `key:value\r\n`,所需字段(`used_memory`/`maxmemory`)是简单标量,collector 内 `awk -F:` 取后 `printf` 成 JSON(`parse.format: json`),不走 `kv` parser。复合值(`db0:keys=...`)本 spike 不涉及。
- **mysql**:`mysql --batch -N -s` 输出 TSV;本 spike 的 connection_usage 只取**聚合标量**(`Threads_connected` / max_connections / 派生率),collector 内直接 `printf` 成 JSON(`parse.format: json`),**不**依赖 table parser 处理 `\N`/转义/列名——把归一化压进 shell(与 postgres.bloat_tables 的 `json_build_object` 同向)。

**裁定**:用真服务输出**实测** kv/table/json/raw 是否够用;只有实测证伪"多个 inspector 的共同需求无法可靠表达"才另起 parser proposal。本 spike 预期**零新增**。

> 替代方案"顺手加个小 parser":被否决——主动放弃项目零新 infra 卖点,且低估对插件系统契约的影响。

### D-3:service 层失败分类 —— 缺前提=requires_unmet,不可达/认证失败=exception

这是本 spike 要钉死的核心契约。延续 wave-1 spike fail-loud 纪律,但在 service 域把边界讲清:

```
缺 client 二进制 / 缺声明 secret → preflight 命中  → status=requires_unmet (跳过, 非错误)
服务不可达 / 认证失败            → collector fail-loud → status=exception   (诚实, 非健康)
服务可达 + 真实零值              → 有效零值对象       → status=ok           (真空, 非失败)
服务可达 + 有效数据              → 有效对象           → status=ok
```

> **正交态**:runner 另有 `timeout`(采集超时)与 `target_unreachable`(`target.exec` 抛 `TargetError`,如 SSH 隧道断)两个传输/超时层状态,由既有 plugin-system 契约管辖、对所有 inspector 同构,**不在**本 service 层分类内——故契约不写"必落三态之一"(spec 同步澄清)。区分:**服务端**端口连不上 → host 上 client 非零 → `exception`;**目标 host** 够不到 → `target_unreachable`。
>
> **缺声明 secret → requires_unmet**:声明 `secrets: [X]` 后 preflight 要求 `X` 存在于 env(`X in os.environ`);无鉴权实例须显式导出空串 `X=` 才能跑(collector 内 `[ -n "$X" ]` 分流),否则 preflight `requires_unmet`——与缺 client 二进制并列,manifest 的无鉴权 `else` 分支因此**仅在**空串约定下可达(非死代码)。

关键反模式(契约禁止):dead/NOAUTH backend 时 collector 兜底吐 `{"used_pct":0}` 被 bless 成 `ok`——监控在后端宕机时静默报"健康"。collector **必须**对每段 client 调用 `|| exit 1` + 数值校验(`case "$v" in ''|*[!0-9]*) exit 1`),空 stdout 触发 parse 异常。真实零值(连接数=0)与采集失败(空 stdout)由此**可区分**。

### D-4:双轨 fixture,落在新 capability 不回溯收紧 OS 套件

finding-trigger fixture(健康态 + 低阈值,验 finding wiring)+ semantic-abnormal fixture(真实异常态,证检出)。**凡 manifest `findings` 非空**(客观判据)即必须附后者,且其在**默认阈值**下触发。

**关键 scope 决策**:双轨要求落在**新** `service-inspector-contract` capability,**不**MODIFIED 已归档的 `os-shell-inspector-suite`。理由:OS 探针的异常态廉价可真造(僵尸进程/满盘),其单-异常-fixture 规则不是漏洞;阈值凑假异常的诱因**只在** service 域(真异常昂贵)才出现。把双轨写进 OS 套件等于回溯收紧已审批的归档 spec、扩大本 spike 范围。

- redis.memory_usage 的 semantic-abnormal:真实高内存占用(compose 内 `DEBUG QUICKLISTS` 或写入大量数据逼近 maxmemory,或起一个 `maxmemory 1mb` 的实例并填满)→ used_pct 真实 ≥95%。
- mysql.connection_usage 的 semantic-abnormal:compose 内把 `max_connections` 设很小 + 开多个保持连接 → 已连接数/上限真实超阈。注意:这是**真实高连接率状态**,不是改 inspector 阈值——区别于 finding-trigger。

### D-5:录制 lane = docker-compose 场景编排 + 轻量录制入口

compose 负责服务版本(固定 digest/patch)、单实例拓扑、异常场景编排;`_record_*.py` 负责等待条件(轮询服务就绪,**禁** `sleep 5` 竞态)、驱动真 runner 录制、脱敏、冻结非确定值、落 fixture。逐个脚本各自起服务会复制大量不稳定编排逻辑,故 compose 集中编排。录制不进日常 CI;ReplayTarget 回放进 CI。

### D-6:SSH secret 投递 —— 采用既有 ssh-execution-target 契约的 HOSTLENS_ 前缀

**核验发现(对真实代码 + 既有契约)**:
1. runner 的 SSH target 经 AsyncSSH `conn.run(cmd, env=env)` 传 env(`src/hostlens/targets/ssh.py:556,602-603`,docstring 明言"No env smuggling、命令字符串绝不改写")。AsyncSSH 的 `env=` 走 SSH 协议 env 请求,**受远端 sshd `AcceptEnv` 白名单约束**(OpenSSH 默认仅放行 `LANG`/`LC_*`)。
2. **既有 `ssh-execution-target` spec(:120-122)早已为此立约**:Inspector secret 经 SSH 的投递路径限于 ① 远端配 `AcceptEnv HOSTLENS_*` + Inspector 用 `HOSTLENS_` 前缀变量名(推荐)② stdin ③ 未来 secret transport;且其集成测试(:176/187)用 `AcceptEnv HOSTLENS_TEST_*` 容器约定。

**裁定**:本 spike 的两探针**直接遵循**既有 ssh 契约——secret **声明为 `HOSTLENS_` 前缀**(`HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD`),collector 内 **remap** 到 client 原生 env(`REDISCLI_AUTH="$HOSTLENS_REDIS_PASSWORD"` / `MYSQL_PWD="${HOSTLENS_MYSQL_PWD:-}" mysql ...`)。这样:
1. **SSH 上真正能用**:远端一行 `AcceptEnv HOSTLENS_*` 即放行全部 hostlens secret,secret 经协议 env 请求到达远端、再 remap 给 client——不是"documented prerequisite that doesn't work",而是与既有 SSH 契约对齐的可用路径。
2. **不改 SSH target 代码**:沿用 `conn.run(env=)` + `HOSTLENS_*` 约定,零 `ExecutionTarget` 改动。
3. **契约诚实**:spec 把 secret 跨 SSH 的"AcceptEnv HOSTLENS_* 前提"显式声明(非透明,但**有**可用路径);非 secret 行为结构性等价。

**发现的既有漂移(→ 独立 follow-up)**:既有 seed `redis.slowlog`(`secrets:[REDIS_PASSWORD]`)/ `postgres.bloat_tables`(`secrets:[PGPASSWORD]`)用**非** `HOSTLENS_` 名,与 ssh 契约 :120-122 **漂移**——它们在 `AcceptEnv HOSTLENS_*` 远端跑 SSH 会丢 secret。把这两个 seed 迁移到 `HOSTLENS_` 前缀是**独立 follow-up**(影响既有归档 inspector,不该被本"2 探针逼契约"的 spike 吞掉);本 spike 的新探针**从一开始就对齐契约**,并把该漂移作为发现记录。

> 替代方案"本 spike 沿用 `REDIS_PASSWORD`/`MYSQL_PWD`(与 seed 一致)+ 仅文档化 AcceptEnv 前提":被否决——直接违反既有 `ssh-execution-target` spec :120-122 的 `HOSTLENS_` 前缀契约,制造新漂移;且"文档前提"在默认+HOSTLENS_* sshd 下仍投递失败。对齐既有契约才是正解。

### 第二个探针完整 manifest:mysql.connection_usage

承载 D-1(env 注入)+ D-6(`HOSTLENS_MYSQL_PWD`→client `MYSQL_PWD` remap)+ D-2(TSV→collector 内归一化为 JSON,不依赖 table parser)+ D-3(service 层失败分类 fail-loud):

```yaml
name: mysql.connection_usage
version: 1.0.0
# Service-inspector-contract probe: proves the NEW client (mysql) + secret
# declared as HOSTLENS_MYSQL_PWD (HOSTLENS_ prefix per ssh-execution-target
# contract) remapped to the client-native MYSQL_PWD env + TSV→JSON collector
# normalization. Requires MySQL 5.7+/8+ and the mysql client. Auth via env
# (D-1), never inlined (no -p<pwd>).
description: >-
  MySQL 连接使用率巡检(单实例)。需 MySQL 5.7+/8+ 与 mysql client;secret 声明为
  HOSTLENS_MYSQL_PWD(HOSTLENS_ 前缀对齐 ssh-execution-target 契约的 AcceptEnv
  HOSTLENS_* 投递),collector 内 remap 到 client 原生 MYSQL_PWD(从不内联、不用
  -p<pwd> 明文)。只报聚合标量(已用连接数 / max_connections / 派生率)。
  服务不可达/Access denied → status=exception(诚实);缺 mysql client → requires_unmet。
  SSH 上需远端 sshd 配 AcceptEnv HOSTLENS_*。
tags: [mysql, mysql57, service, connections]
targets: [local, ssh]
requires_capabilities: [shell]
requires_binaries: [mysql]
secrets: [HOSTLENS_MYSQL_PWD]
privilege: none

parameters:
  type: object
  required: [user]
  properties:
    host:
      type: string
      pattern: "^[a-zA-Z0-9._-]+$"      # blocks shell injection in -h value
      default: "127.0.0.1"
    port:
      type: integer
      minimum: 1
      maximum: 65535
      default: 3306
    user:
      type: string
      pattern: "^[A-Za-z0-9_][A-Za-z0-9_.-]*$"   # mysql identifier charset
    warn_used_pct:     { type: number, default: 80.0 }
    critical_used_pct: { type: number, default: 95.0 }
  additionalProperties: false

collect:
  # D-1/D-6: secret declared as HOSTLENS_MYSQL_PWD (HOSTLENS_ prefix survives SSH
  # AcceptEnv HOSTLENS_*); the collector REMAPS it to the client-native MYSQL_PWD
  # env prefix on the mysql invocation, read implicitly by the client — NEVER
  # `-p<pwd>` (which leaks in `ps`) and NEVER `{{ }}`-interpolated. D-2: status
  # vars read with `--batch -N -s`
  # (tab-separated, no header, silent); we DERIVE used_pct in awk then `printf`
  # a top-level JSON object — TSV never reaches a parser, so no table-parser
  # dependency on \N/escaping. used_connections comes from the GLOBAL status var
  # `Threads_connected` (NOT `COUNT(*) FROM information_schema.processlist`,
  # which a non-PROCESS-privileged inspector user sees ONLY its own thread for →
  # silent under-count → false "healthy" while the backend is connection-
  # saturated). `SHOW GLOBAL STATUS` needs no special privilege and is global.
  # D-3 fail-loud: each mysql call must succeed AND yield a bare integer, else
  # exit 1 + empty stdout → status=exception (a down/Access-denied backend never
  # fabricates used_pct=0). NOTE the value is captured into `$raw` via command
  # substitution and awk'd on a SEPARATE line — piping `mysql … | awk` would
  # make `|| exit 1` inspect awk's exit (always 0 on empty input), masking a
  # mysql failure (the same pipeline-exit trap docker.restart_loop documents).
  # host/user flow through `| sh`; port is int. --connect-timeout 5 < timeout 15
  # (covers the TCP-connect phase; an auth/query hang past 15s falls to
  # status=timeout, an orthogonal transport-layer state — see spec failure-class).
  command: |
    raw=$(MYSQL_PWD="${HOSTLENS_MYSQL_PWD:-}" mysql -N -s --batch --connect-timeout=5 -h {{ host | sh }} -P {{ port }} -u {{ user | sh }} \
      -e "SHOW GLOBAL STATUS LIKE 'Threads_connected'") || { echo "mysql threads_connected failed" >&2; exit 1; }
    used=$(printf '%s' "$raw" | awk '{print $2}')
    maxc=$(MYSQL_PWD="${HOSTLENS_MYSQL_PWD:-}" mysql -N -s --batch --connect-timeout=5 -h {{ host | sh }} -P {{ port }} -u {{ user | sh }} \
      -e "SELECT @@max_connections") || { echo "mysql max_connections failed" >&2; exit 1; }
    case "$used" in ''|*[!0-9]*) echo "used non-numeric: $used" >&2; exit 1;; esac
    case "$maxc" in ''|*[!0-9]*) echo "maxc non-numeric: $maxc" >&2; exit 1;; esac
    if [ "$maxc" -gt 0 ]; then
      pct=$(awk -v u="$used" -v m="$maxc" 'BEGIN{printf "%.2f", (u/m)*100}')
      printf '{"used_connections":%d,"max_connections":%d,"used_pct":%s}' "$used" "$maxc" "$pct"
    else
      printf '{"used_connections":%d,"max_connections":%d,"used_pct":null}' "$used" "$maxc"
    fi
  timeout_seconds: 15

parse:
  format: json

output_schema:
  type: object
  properties:
    used_connections: { type: integer }
    max_connections:  { type: integer }
    used_pct:         { type: [number, "null"] }
  required: [used_connections, max_connections, used_pct]
  additionalProperties: false

findings:
  - when: "used_pct != None and used_pct >= critical_used_pct"
    severity: critical
    message: "MySQL connections at {used_pct}% ({used_connections}/{max_connections})"
  - when: "used_pct != None and used_pct >= warn_used_pct and used_pct < critical_used_pct"
    severity: warning
    message: "MySQL connections at {used_pct}% ({used_connections}/{max_connections})"
```

## 风险 / 权衡

- **[client 原生 `MYSQL_PWD` 在 env 可见]** → 已与 pg/redis 同构(D-1);declared secret 为 `HOSTLENS_MYSQL_PWD`、collector remap 到 client `MYSQL_PWD` env 而非 `-p<pwd>`(后者在 `ps aux` 全局可见,更糟);录制器脱敏回显。残余风险接受,迁移凭据文件作为独立 proposal。
- **[mysql TSV 实测发现 table parser 不可靠]** → 本 spike 的 connection_usage 走 collector 内 awk→JSON 绕开 parser,故 spike 本身不受影响;若后续 wave 的 mysql inspector 需要 table parser 且实测证伪,届时另起 parser proposal(D-2 已留口)。
- **[semantic-abnormal fixture 制造成本]** → 单实例异常态(高内存/高连接率)compose 内可廉价真造;真正昂贵的(replication lag)已被 D-1 边界排除出本 spike。
- **[docker-compose 录制 lane 不稳定]** → 录制入口用就绪轮询非固定 sleep;镜像固定 digest;录制不进 CI,只影响作者重录,不影响 CI 绿。
- **[契约写得过窄,铺量时撞到未覆盖维度]** → 接受。两层渐进契约:本 spike 只立"几乎每个 service inspector 都撞"的公共边界;撞到新维度(尤其多实例)在对应 wave 补,不预先立法。
- **[secret 经 SSH 被 sshd AcceptEnv 丢弃 → 认证失败]** → D-6:两探针**采用**既有 ssh 契约的 `HOSTLENS_` 前缀路径(secret 声明 `HOSTLENS_*` + 远端 `AcceptEnv HOSTLENS_*`),secret 可跨 SSH 到达;机制已定(非"待裁定")。残余风险:用户若不配 `AcceptEnv HOSTLENS_*` 则 secret inspector 在 SSH 上落 `exception`(诚实失败、非假健康),可接受。**唯一** follow-up 是迁移既有 seed slowlog/bloat 到 `HOSTLENS_` 前缀(它们漂移、非本 spike 引入)。

## 迁移计划

- 纯新增,无数据迁移。2 个 inspector + 1 个 spec capability + 录制 lane。
- 回滚:删除 2 个 manifest + 测试 + spec 目录即可,无运行时状态。
- feature branch `feat/add-service-inspector-contract-spike` → PR → CI 绿 → squash;archive 时 delta 合入 `openspec/specs/service-inspector-contract/`。

## 待解决问题

- **table parser 精确行为**:mysql `--batch -N` 的 `\N`/转义在现有 table parser 下的实际表现,需在实现期对真 mysql 输出实测(本 spike 探针绕开了它,故非阻塞;结论记入 D-2 供后续 wave 用)。
  - **实测(mysql:8.0.40,task 3.2)**:`mysql -N -s --batch` 的 TSV 实测——(i) NULL 渲染为**字面字符串 `NULL`**(不是 `\N`;`\N` 是 `mysqldump`/`SELECT … INTO OUTFILE` 的约定,不是交互 `--batch` 的);(ii) 值内的真实 tab 被**反斜杠转义**为 `\t`(`SELECT 'a<tab>b'` → 字节 `a \ t b`),即列分隔符与数据中 tab **靠转义区分**而非引号。这意味着后续 wave 若用现有 `table` parser 处理多列 mysql TSV,必须确认其按 `\t` 反转义 + 把字面 `NULL` 当 sentinel(而非真值)——否则 `\N`-类假设会错。connection_usage 探针只取**裸整数标量**(`Threads_connected` 值 / `@@max_connections`),既无 NULL 也无嵌入 tab,collector 内 awk 取第 2 列 + `printf` 成 JSON(`parse.format: json`)实测产出合法顶层对象 `{"used_connections":1,"max_connections":151,"used_pct":0.66}`,**不经任何 table/kv parser**——故本 spike 不受上述转义语义影响,D-2「零新增 parser」在本探针成立。
- **MYSQL_PWD env 鉴权(D-1 forward-fragility,task 3.2(b))实测**:mysql:8.0.40 上 `MYSQL_PWD="…" mysql …`(无 `-p<pwd>`)鉴权**成立**(`SELECT VERSION()` 返回 `8.0.40`、exit 0);错值 → Access denied、exit 非零(fail-loud 路径成立)。当前(本 spike 录制环境)`MYSQL_PWD` 未被移除,D-1 的「沿用 env 注入」前提对录制基线有效;若未来某支持版本移除 `MYSQL_PWD` 再触发凭据机制独立 proposal(D-1 风险注记)。
- **redis maxmemory=0(无上限)语义**:本 spike 把 used_pct 置 null;后续 wave 的 redis 内存 inspector 是否要加"无上限但绝对值过高"的告警维度,留 wave-2a 决定。
- **既有 seed 迁移时点**:SSH secret 投递机制**已定**为 `HOSTLENS_` 前缀 + `AcceptEnv HOSTLENS_*`(D-6,采用既有 ssh 契约);唯一开放项是**何时**把既有 seed `redis.slowlog`/`postgres.bloat_tables` 从非 `HOSTLENS_` 名迁移过来——留独立 follow-up proposal 安排。
