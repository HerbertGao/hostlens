# replication-inspector-contract 规范

## 目的

定义**复制 / 多实例巡检 inspector** 的契约,建立在 `service-inspector-contract`(单实例)之上、继承其全部采集纪律(注入安全三件套 / `HOSTLENS_*` secret remap / 失败三态 / 超时 / 跨 local·SSH 无分叉),并**补充**单实例契约显式排除的复制维度:

- **统一形态三元组** `replication_configured` / `link_healthy` / `lag_seconds(integer|null)`,跨 DB 同形;`lag_seconds` 形态统一但语义随 DB 异构,每个 inspector 必须显式声明其**语义类**(`link_freshness` / `apply_lag`),两类不可直接跨 DB 比较。
- **三态 by-finding**:未配置复制(`ok` 无 finding)/ 链路断(critical)/ 滞后(warn·critical),区分由 finding 规则表达、禁污染 status;fail-loud 按 role 上下文。
- **采集视角归约**:副本侧自报(N=1)退化为 identity;主库侧多行(如 postgres `pg_stat_replication`)按冻结归约函数(`lag_seconds` 取最大、`link_healthy` 取逻辑与、空集守卫、检测边界)在 collector 内归约;明细禁回吐给 DSL。
- **双轨 fixture**:finding-trigger(降阈值证接线)+ 两个语义不同的真造 semantic-abnormal(`link_down` / 滞后),poll-until-condition 录制、ReplayTarget 逐字回放。
- **独立 crosscheck**:不进单实例 cohort(`_ALL_SERVICE_MANIFESTS` 11 / `_SECRET_SERVICE_MANIFESTS` 6 冻结),由 `test_replication_contract_crosscheck.py` 复跑继承项 + 复制专属项,带 cohort 计数守卫。
- **覆盖矩阵追加式冻结**:每个 wave 只追加单元格、禁回溯改。已交付:**redis**(spike,`INFO replication` 副本侧,`link_freshness`)、**mysql**(wave-2c,`SHOW REPLICA STATUS` 副本侧,`apply_lag`)、**postgres**(wave-3,`pg_stat_replication` 主库侧,`apply_lag`)。

## 需求
### 需求:复制 inspector 契约建立在单实例契约之上并继承其采集要求

`replication-inspector-contract` **必须**以独立 capability 建立在 `service-inspector-contract` 之上,**继承**其全部单实例采集要求——连接参数注入安全三件套、secret 经 `HOSTLENS_*` 声明并 remap 到 client 原生 env 通道(从不进 argv、从不 `{{ }}` 插值)、service 层失败三态(`requires_unmet` / `exception` / `ok`)、超时与输出纪律、跨 local 与 SSH target 无分叉。本契约**禁止**重复立法这些已有要求,只**补充**单实例契约显式排除的多实例 / 复制维度。一个复制 inspector **禁止**绕过本契约直接援引单实例契约作为完备依据。

#### 场景:继承项不重复立法但被复跑验证

- **当** 一个复制 inspector(如 `redis.replication_lag`)落地
- **那么** 它的连接注入安全、secret remap、失败三态、超时、无分叉**必须**由其独立 crosscheck 测试**复跑**断言通过(继承被机械证明,而非仅在文档里声明),且本契约 spec **不**复制这些要求的条文

#### 场景:禁止援引单实例契约为多实例完备依据

- **当** 评审一个多实例 / 复制 inspector 是否满足契约
- **那么** 评审**必须**以 `replication-inspector-contract` 为依据;仅满足 `service-inspector-contract`(单实例)**不**构成多实例 inspector 的完备合规证明

### 需求:复制 inspector 必须归一出统一形态三元组并声明 lag 语义类

每个复制 inspector 的 `output_schema` **必须**归一出统一**形态**的三元组,无论底层 DB 的原始信号形态:
- `replication_configured: bool` —— 本实例是否处于复制关系(跨 DB 真正同形)。
- `link_healthy: bool` —— 复制链路当前是否正常(跨 DB 真正同形)。
- `lag_seconds: integer | null` —— 副本侧可测得的滞后秒数;未配置复制或无法测量时为 `null`。

`lag_seconds` 的**形态统一**(秒 + null guard,DSL 比法跨 DB 一致),但其**语义随 DB 而异**:每个复制 inspector **必须**在 manifest `description` 与其 spec 中**显式声明** `lag_seconds` 的语义类——`link_freshness`(链路新鲜度,如 redis `master_last_io_seconds_ago`=距上次主从 IO 的秒数,**非**数据 apply 滞后)或 `apply_lag`(数据应用滞后,如 mysql `Seconds_Behind_Source` / postgres replay-timestamp 差)。本契约**禁止**假装 `lag_seconds` 跨 DB 语义统一;两个语义类**不可直接跨 DB 比较**(详见「裁定」需求)。各 DB 原始信号**必须**在各自 collector 命令内换算成该形态三元组。Finding DSL **只允许**对三元组标量做比较;**禁止**让 DSL 理解任何 DB 专有原始字段或单位。

#### 场景:redis 副本归一出三元组并声明 freshness 语义

- **当** `redis.replication_lag` 对一个 `role:slave`、`master_link_status:up`、`master_last_io_seconds_ago:3` 的副本采集
- **那么** collector 输出 `replication_configured=true`、`link_healthy=true`、`lag_seconds=3`(语义类 `link_freshness`,在 description 与 spec 中声明),且 finding 规则只对这三个标量比较

#### 场景:无法测量时 lag 为 null 而非 0

- **当** 实例未配置复制(`replication_configured=false`)
- **那么** `lag_seconds` **必须**为 `null`(而非伪造的 `0`),以区分"没有滞后"与"无从测量"

#### 场景:redis 同步哨兵 -1 归一为 null

- **当** redis 副本在初始同步 / 链路重建瞬间吐 `master_last_io_seconds_ago:-1`
- **那么** collector **必须**把 `-1` 归一成 `lag_seconds=null`(无从测量),**禁止**输出 `lag_seconds=-1`(否则 `-1>=阈值` 永假、把"正在同步"误当健康)

### 需求:复制 inspector 必须区分未配置复制 / 复制故障 / 复制滞后三态

在继承的失败三态(`requires_unmet` / `exception` / `ok`)之上,复制 inspector **必须**在 `ok` 内部按复制语义再分三态,且该区分**必须由 finding 规则表达,禁止污染 status**:
- **未配置复制**(`replication_configured=false`,如 standalone 或 primary):status **必须**为 `ok`,且**禁止**产生任何 finding(合法单机不是故障)。
- **配置但链路断**(`replication_configured=true && link_healthy=false`):status `ok`,**必须**产生 critical finding。
- **配置且滞后**(`link_healthy=true && lag_seconds>=阈值`):status `ok`,**必须**按 `lag_seconds` 产生 warn / critical finding。

连不上副本 / 认证失败**必须**走继承的 `exception`(collector fail-loud:非零退出 + 空 stdout);缺 client 二进制**必须**走 `requires_unmet`。**禁止**把"未配置复制"映射成 `exception`,也**禁止**把"链路断"吞成无 finding 的 `ok`。**fail-loud 必须按 role 上下文**:未配置实例(如 redis `role:master`)的采集输出**本就缺** replica-only 字段(链路/lag 字段),collector **禁止**把这种"缺字段"当 fail-loud 而误判 `exception`——**必须**先据 role 判定未配置后走 `ok` + `replication_configured=false` 路径;只有处于复制关系的实例(`role:slave`)而其链路/lag 字段缺失才算真异常。

#### 场景:未配置复制不告警

- **当** 对一个 `role:master`(无 `master_host`、无 `master_link_status`/`master_last_io_seconds_ago` replica-only 字段)的 standalone redis 采集
- **那么** status=`ok`、`replication_configured=false`、`lag_seconds=null`、findings 为空(不产生假告警);collector **禁止**把缺失的 replica-only 字段当 fail-loud 而返回 `exception`

#### 场景:链路断产生 critical

- **当** 副本 `master_link_status:down`(`replication_configured=true && link_healthy=false`)
- **那么** status=`ok` 且产生一条 critical finding「replication link down」

#### 场景:连不上副本走 exception 而非伪造健康

- **当** redis-cli 连副本失败 / NOAUTH(非零退出 + 空 stdout)
- **那么** status=`exception`,**禁止**伪造一个 `link_healthy=true` 的健康结果

### 需求:副本侧采集视角必须聚合为 identity 且明细禁回吐给 DSL

副本侧自报视角(N=1,本 spike 探针所走)的聚合**必须**退化为 identity——输出即该副本的三元组,无需归约。任何复制 inspector **禁止**把多行 per-replica 明细回吐给 DSL 让其自行聚合(违反 authoring-contract"派生在 collector 内")——多副本归约**必须**在 collector 内完成。

**主库侧多副本视角的归约函数**(postgres `pg_stat_replication` 多行;redis `connected_slaves` 为潜在未来路径,当前已交付的 `redis.replication_lag` 走**副本侧** `INFO replication`、不是主库侧)由独立需求「主库侧采集视角必须按冻结归约函数归约」承载——该函数已被 wave-3(`postgres.replication_lag`)**冻结为 normative 并测试**(`lag_seconds` 取所有副本最大滞后、`link_healthy` 取所有副本链路逻辑与、空集守卫、主库侧检测边界)。本需求**只**规范副本侧 identity 退化;主库侧归约**禁止**在本需求重复立法,以兄弟需求为准。

#### 场景:副本侧 N=1 聚合为 identity

- **当** `redis.replication_lag` 连接单个副本读其自报 link/lag
- **那么** 聚合为 identity(N=1),输出即该副本的三元组,无需归约;collector **禁止**把 per-replica 明细交给 DSL

#### 场景:主库侧归约以冻结的兄弟需求为准

- **当** 评审一个主库侧复制 inspector(如 `postgres.replication_lag`)的多行归约
- **那么** 评审**必须**以「主库侧采集视角必须按冻结归约函数归约」需求为依据(滞后取最大 / 链路取逻辑与 / 空集守卫 / 检测边界);本「副本侧 identity」需求**不**承载主库侧归约的 normative 验收

### 需求:复制 inspector 的 semantic-abnormal fixture 必须制造两个语义不同的真实故障且禁低阈值凑

每个复制 inspector **必须**附双轨 fixture(继承单实例契约的双轨要求):finding-trigger(健康拓扑 + 降低阈值,只证接线)与 semantic-abnormal(**真实**异常拓扑 + **默认**阈值触发,证检出能力)。复制 inspector 的 semantic-abnormal **必须**通过**真造**复制故障状态获得,**禁止**用降低阈值在健康副本上凑出 finding,且**必须**覆盖**两个语义不同**的故障分支——`link_healthy=false`(链路断)与 `link_healthy=true && lag_seconds>=阈值`(链路陈旧/滞后),两个 fixture 语义**禁止**重合(否则只测了一个分支)。录制 readiness **必须**用 poll-until-condition(poll 一个状态条件,如 `master_link_status==down` / `master_link_status==up && master_last_io_seconds_ago>=N`),**禁止**用固定 `sleep N` 等待。录制产物**必须**冻结(ReplayTarget 逐字回放),使 replay 不依赖时钟、snapshot 确定可复现。

#### 场景:真实链路断 fixture（link_down）

- **当** 录制 semantic-abnormal「链路断」
- **那么** 录制器建立真实 master+replica 复制 → poll 确认 link up → **停 master 容器**(TCP 断开,链路秒级判 `down`,**不依赖 `repl-timeout`**——`repl-timeout` 仅服务于 link_stale 的 link-up 保持)→ **poll 副本直到 `master_link_status==down`** → 冻结该快照(`link_healthy=false`);snapshot 在**默认**阈值下触发 critical

#### 场景:真实链路陈旧 fixture（link_stale）用 poll 而非 sleep 且与 link_down 语义不同

- **当** 录制 semantic-abnormal「链路陈旧」
- **那么** 录制器在 master 上异步 `DEBUG SLEEP <T>`、**T 大于默认 `critical_seconds` 且远小于 pin 的 `repl-timeout`**(冻结主事件循环、暂停发 ping 但不至判链路 down)→ **poll 副本(经另一连接)直到 `master_link_status==up` 且 `master_last_io_seconds_ago>=` 默认阈值** → 冻结该真实值(`link_healthy=true` 但 `lag_seconds` 高);默认阈值**必须**高于 `repl-ping-replica-period`(否则健康空闲副本误报);**禁止**固定 `sleep` 等值长出来;录制时**必须**断言本 fixture 的 `master_link_status==up`(与 link_down 的 `==down` 语义区分)

### 需求:复制 inspector 不进单实例 cohort 且须独立 crosscheck

复制 inspector **禁止**加入 `service-inspector-suite` 的单实例 cohort 与 `test_service_contract_crosscheck.py` 的 `_ALL_SERVICE_MANIFESTS` / `_SECRET_SERVICE_MANIFESTS`(显式 dict 枚举,其单实例计数冻结 11 / 6 **必须**保持不变)。复制 inspector **必须**由独立的 `test_replication_contract_crosscheck.py` 验收,该 crosscheck **必须**同时:(a) 复跑断言全部继承的单实例契约项;(b) 断言复制专属项(归一三元组在 output_schema、lag 语义类已声明、三态 by-finding、副本侧 N=1、`link_down` 与 `link_stale` 两个语义不同的 semantic-abnormal fixture 在默认阈值触发)。复制 inspector 的参数名**禁止**引入多实例词(`replica`/`primary`/`replication`/`lag`/`instances`/`nodes`),其多实例语义**必须**体现在 output_schema 三元组与 fixture 拓扑,而非参数名。新增复制 inspector 文件会被 builtin 全量测试(均经 `rglob` 枚举全部 builtin yaml——`test_builtin_capability_gate.py` 直接 `rglob`、`test_builtin_inspectors.py` 经全量 registry 构建间接 `rglob`)自动纳入,**必须主动通过**(而非仅"计数不误红"):①静态 capability gate(只声明静态 `requires_capabilities`);②全量注册 `errors == []`(干净加载)。

#### 场景:被全量测试纳入仍主动通过

- **当** `redis.replication_lag.yaml` 落地并被 `test_builtin_capability_gate.py`(直接 rglob)/ `test_builtin_inspectors.py`(经 registry 构建)自动扫描
- **那么** 它**必须**通过静态-capability 断言(只声明 `requires_capabilities:[shell]`)且全量注册 `errors == []`(干净加载),既有 wave cohort 子集断言与宽松计数下界不因多一个 builtin 而误红

#### 场景:单实例计数冻结不变

- **当** `redis.replication_lag` 落地
- **那么** `test_service_contract_crosscheck.py` 的 `_ALL_SERVICE_MANIFESTS`(11)与 `_SECRET_SERVICE_MANIFESTS`(6)计数**保持不变**,该文件不枚举复制 inspector

#### 场景:独立 crosscheck 复跑继承项

- **当** 运行 `test_replication_contract_crosscheck.py`
- **那么** 它对 `redis.replication_lag` **复跑**注入安全 / secret remap 不进 argv / 失败三态 / 超时 / 无分叉,并断言归一三元组、lag 语义类声明、`link_down` 与 `link_stale` 两个语义不同的 semantic-abnormal fixture 存在

### 需求:复制 spike 必须裁定契约可沿用性并记录 lag 语义异构发现

本 spike **必须**产出一条可归档的**裁定**,作为 wave-2c(`add-replication-lag-inspectors`)的范围依据。裁定**必须**首先记录本 spike 的**核心发现:统一 `lag_seconds` 跨 DB 语义异构**——redis 副本侧单连接的干净信号是 `link_freshness`(`master_last_io_seconds_ago`,非数据 apply 滞后;真 apply 滞后需 offset 字节差 + 主连接),mysql/pg 的是 `apply_lag`;两个语义类**不可直接跨 DB 比较**,契约**禁止**假装统一。在此基础上,对 lag 副本侧自报、与探针契约贴合的 DB **必须**判定为"可沿用,机械铺"(并标注其 lag 语义类与已知坑);对 lag 形态分叉、沿用性未经真实录制验证的 DB **必须**判定为"待 wave-2c 录制验证,否则推 wave-3"。裁定**禁止**停留在对话或 PR 描述里,**必须**写入 spec / design 以便 wave-2c 援引。

#### 场景:记录 lag 语义异构核心发现

- **当** 归档本 spike 的裁定
- **那么** 裁定**必须**显式声明 redis `lag_seconds` 是 `link_freshness`、mysql/pg 是 `apply_lag`、两者不可直接跨 DB 比较,并对 `add-replication-lag-inspectors` 骨架中 redis 行"offset 差"(apply-lag 路径,本 spike 不交付)给出 hand-off 更正

#### 场景:同形 DB 判为可沿用（带已知坑）

- **当** 裁定 mysql(`Seconds_Behind_Source` 副本自报、apply_lag 语义、单位秒)
- **那么** 记录"高置信可沿用,wave-2c 机械铺",给出 `Seconds_Behind_Source`→`lag_seconds`(语义类 `apply_lag`)、`Replica_IO_Running==Yes && Replica_SQL_Running==Yes`→`link_healthy` 的映射,并标注已知坑:`Seconds_Behind_Source`(8.0.22+;5.7–8.0.21 为 `Seconds_Behind_Master`)在 IO 断开/追赶中会返回 NULL(须归一成 `lag_seconds=null` 而非 0)、IO/SQL 线程列名同步在 8.0.22 由 `Slave_*` 改 `Replica_*`(按目标版本择名)

#### 场景:分叉 DB 判为待验证或推 wave-3

- **当** 裁定 postgres(lag 既可副本侧 `pg_last_wal_replay_lsn`/`pg_last_xact_replay_timestamp`、又可主库侧 `pg_stat_replication` 聚合)
- **那么** 记录"形态分叉,沿用性待 wave-2c 真实录制验证;若主库侧聚合 + LSN/idle 换算撑破契约则推 wave-3",并标注坑:`now()-pg_last_xact_replay_timestamp()` 在主库空闲时虚高、须 guard(无在途事务时滞后视为 0/null),而非默认塞进 wave-2c

### 需求:复制 inspector 覆盖矩阵随 wave 追加冻结

`replication-inspector-contract` **必须**维护一个**覆盖矩阵**,记录每个已交付复制 inspector 的 DB、采集路径与 `lag_seconds` 语义类;矩阵**追加式冻结**——每个 wave 只**追加**单元格,**禁止**回溯 MODIFY 已冻结单元格(mirror `service-inspector-suite` 的 cohort 冻结纪律)。已冻结单元格:

- **redis**(spike 交付):`INFO replication` 副本侧,语义类 `link_freshness`。
- **mysql**(wave-2c 交付):`SHOW REPLICA STATUS` 副本侧,语义类 `apply_lag`。
- **postgres**(wave-3 交付):**主库侧** `pg_stat_replication`,`lag_seconds = FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint`(整数化见「主库侧采集视角必须按冻结归约函数归约」需求),语义类 `apply_lag`。副本侧 `now()-pg_last_xact_replay_timestamp()` + idle-guard 路径已被 wave-2c 录制门**否决**(receiver 断时 `recv==replay` 仍 TRUE → 捏造 lag=0;`recv_lsn` 未流式时 NULL),见 wave-2c design「门裁定」。postgres 是**首个主库侧单元格**,其多行归约遵守「主库侧采集视角必须按冻结归约函数归约」需求(空集→ok、主库侧测不到副本全断、`pg_monitor`/superuser 读 lag 列前提);本单元格**兑现了 spike 裁定的「postgres 形态分叉 → 推 wave-3」分支**(spike 历史裁定为待验证档,wave-3 经主库侧路径交付)。

矩阵内每个已交付复制 inspector **必须**遵守本契约既有的全部需求(继承单实例契约 / 归一三元组 + 声明语义类 / 三态 by-finding / 副本侧 identity 或主库侧冻结归约 / 两个语义不同 semantic-abnormal / 不进单实例 cohort)。`lag_seconds` 的语义类**随单元格而异且不可直接跨 DB 比较**(redis `link_freshness` ≠ mysql/pg `apply_lag`);本需求**禁止**抹平该差异。

#### 场景:mysql 单元格按 apply_lag 归一并声明语义类

- **当** `mysql.replication_lag` 对一个配置了复制的副本采集
- **那么** collector 从 `SHOW REPLICA STATUS`(8.0.22+;5.7–8.0.21 `SHOW SLAVE STATUS`)归一出 `replication_configured=true`、`link_healthy`=`Replica_IO_Running==Yes && Replica_SQL_Running==Yes`(5.7 `Slave_*`)、`lag_seconds`=`Seconds_Behind_Source`(5.7 `Seconds_Behind_Master`;**NULL 必须归一成 `lag_seconds=null` 而非 0**),语义类 `apply_lag` 在 `description` 与覆盖矩阵中声明

#### 场景:非复制 mysql 实例走未配置路径而非 exception

- **当** 对一个**未配置复制**的 mysql 实例采集(`SHOW REPLICA STATUS` 返回空结果集)
- **那么** status=`ok`、`replication_configured=false`、`lag_seconds=null`、findings 为空;collector **禁止**把「空结果集」当 fail-loud 而返回 `exception`(role-contextual fail-loud,对应 redis role:master standalone)

#### 场景:postgres 单元格按主库侧 apply_lag 归一并声明前提

- **当** `postgres.replication_lag` 连接**主库**,`pg_stat_replication` 返回在线 standby 行
- **那么** collector 归一出 `replication_configured=(行数>0)`、`link_healthy=bool_and(coalesce(state::text,'')=='streaming')`(NULL state→false)、`lag_seconds=FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint`(全 NULL→null),语义类 `apply_lag` 在 `description` 与覆盖矩阵中声明;`description` **必须**声明硬前提 `pg_monitor`(或 superuser,否则 lag 列读成 NULL → 静默假健康)与「指向 primary」(指向 standby 则 `pg_stat_replication` 空 → 假未配置)

#### 场景:postgres 主库侧空集走未配置路径而非 exception

- **当** 对一个**无在线 standby** 的 postgres 主库采集(`pg_stat_replication` 空结果集)
- **那么** status=`ok`、`replication_configured=false`、`lag_seconds=null`、findings 为空;collector **禁止**把空集当 fail-loud 而返回 `exception`(主库侧测不到副本全断,显式划边界);psql 非零退出 / 连不上 / 认证失败才走 `exception`,缺 `psql` 走 `requires_unmet`

#### 场景:postgres semantic-abnormal 两个 fixture 语义不同且主库侧拓扑造

- **当** 录制 `postgres.replication_lag` 的两个 semantic-abnormal fixture
- **那么** `link_down` **必须**用「行在、`state≠'streaming'`」(录制实证:`catchup` 在快速 loopback 太瞬态不可靠 latch,**改用可保持的 `backup` 态**——throttled `pg_basebackup --max-rate` 在 `pg_stat_replication` 持续显示一个 `state='backup'` walsender;`backup` 同属接受误报集、满足 `state != 'streaming'`)而**非**物理断开(物理断开使该行消失→空集→ok,测不到);`lagging` **必须**用 standby `recovery_min_apply_delay` + 主库持续写(poll 主库直到该行 `replay_lag>=` 默认 critical 阈值且 `state=='streaming'` → 冻结,`link_healthy=true` 且 lag 高);两 fixture 语义**禁止**重合(link_down 行 `state!='streaming'`、lagging 行 `state=='streaming'`),全程 poll-until-condition 禁固定 sleep
- **且** link_down finding 的 message **必须含子串「link down」**(大小写不限)——既有 `test_replication_contract_crosscheck.py` 对所有复制 inspector 的 link_down fixture 硬断言 `"link down" in findings[0].message.lower()`;postgres 即便语义是「非 streaming」,message 也**必须**写成如「PostgreSQL replication link down (standby not streaming)」以兼容该泛化断言,**禁止**只写「not streaming」而漏「link down」导致既有 crosscheck 变红

#### 场景:postgres 多行归约由录制时断言兑现(回放 fixture 不重跑归约)

- **当** 验收 ADDED「主库侧采集视角必须按冻结归约函数归约」需求的 `max_over_rows`/`AND_over_rows`
- **那么** 因 `ReplayTarget` 冻结的是 collector 已归约的三元组 stdout、回放**不重跑** SQL 聚合,reduction 正确性**必须由录制时断言**保证:录制器用**单条查询在同一 MVCC 快照内**同时取 raw 多行(`json_agg(row_to_json(...))`)与聚合三元组(`count`/`bool_and(coalesce...)`/`FLOOR(EXTRACT(EPOCH FROM max))::bigint`),在 Python 端从该 raw 独立算 `max(FLOOR(EPOCH))`/`all(state=='streaming')`,断言**等于同一查询的聚合列**(验证聚合 SQL 逻辑在一致快照上正确);**禁止两次独立往返**(`replay_lag` 实时漂移→断言 race)。**拓扑必须使 max 与 AND 都非平凡且在同一 fixture 内**:**≥2 个 non-NULL 且 distinct 的 streaming 行**(令 `max` 须在两真值间取较大者、非 identity;用 `recovery_min_apply_delay` 在 standby 上撑出 distinct lag——**禁止**「只一行有值、其余 NULL」,那让 max 退化成只对唯一 non-NULL 取值)+ **同一 fixture 内 ≥1 个非 streaming 行**(令 `AND` 从混合得 false、非单行 identity;录制取 `backup` 态 walsender——throttled `pg_basebackup`)——**禁止**把 max 与 AND 拆进两个 fixture(spec 要求单载体同时非平凡兑现二者)。录制实测载体:3 个 distinct-lag streaming standby(如 30/2/0 秒)+ 1 个 `backup` walsender,同一快照 `max=30`、`AND=false`。冻结的 `multi_replica` fixture 回放只供 DSL/parse + 作录制时同快照重算的留痕证据,**不**声称在回放时证伪 reduction bug。**这是 postgres 净新增技术、不是 mirror mysql**(mysql N=1 无多行可重算);**禁止**把录制时断言退化成「断言已归约三元组自身」(自证、reduction 未测)

#### 场景:postgres 未配置 / 空闲 NULL 两个稳态由真录制 fixture 守卫

- **当** 验收主库侧空集守卫(N5)与全行 NULL→null(N4)
- **那么** **必须**有真录制的 `unconfigured` fixture(单机主库或 0 在线 standby 的真 `pg_stat_replication` 空集,断言 `ok`/`(false,false,null)`/无 finding,守住 vacuous-true bug 的回归)与 `idle`(streaming 且 `replay_lag` NULL 的空闲稳态,断言 `link_healthy=true`/`lag_seconds=null`/无 finding);注入式 `_UNCONFIGURED_OK_STDOUT` 只测 DSL 旁路、**不**替代 collector 空集分支的真录制守卫

#### 场景:门实证欠权 NULL 后必须有下游强制门(不得静默不改)

- **当** wave-3 录制门(tasks 1.2)实证「`state` 列在欠权下仍可见而仅 lag 列 NULL」(W3-6 的 b 分支,静默假健康)
- **那么** 实现**必须**三件套兜底:① collector 加防护分支;② 补一个欠权-NULL 回归 fixture;③ crosscheck 加对应断言;**禁止**因「门实证结论是负面」就停留在 design 散文、manifest/fixture/crosscheck 三处无痕(负面实证结论必须有强制下游)

#### 场景:复制 crosscheck 枚举全部已交付复制 inspector 且单实例 cohort 不受影响

- **当** 运行 `test_replication_contract_crosscheck.py`
- **那么** 它**必须枚举**覆盖矩阵里全部已交付复制 inspector(redis + mysql + postgres)、对每条复跑继承的单实例契约项 + 复制专属项,并带**计数守卫**冻结复制 cohort 规模为 **3**;新增 postgres 复制 inspector **禁止**导致单实例 `_ALL_SERVICE_MANIFESTS`(11)/ `_SECRET_SERVICE_MANIFESTS`(6)计数变化或全量 rglob 测试(`test_builtin_capability_gate` / `test_builtin_inspectors`)误红

### 需求:主库侧采集视角必须按冻结归约函数归约

当一个复制 inspector 的采集视角是**主库侧**(单连接读到**多行** per-replica 状态,如 postgres `pg_stat_replication`),其归约**必须**在 collector 内按下列**冻结的 normative 函数**完成(把契约此前的「前向暂定方向」升级为可验收、可测试的规则),归一出与副本侧同形态的三元组;DSL **只**比较归约后的三元组标量,**禁止**理解任何 DB 专有 per-replica 字段。归约函数:

- `replication_configured = (在线副本行数 > 0)`。
- `link_healthy = replication_configured ? AND_over_rows(单行链路健康) : false`(**链路逻辑与**:任一在线副本链路不健康即 `false`)。
- `lag_seconds = replication_configured ? max_over_rows(单行滞后秒数, 仅取 non-NULL 行) : null`(**滞后取最大**);若所有在线行滞后均 NULL **则** `lag_seconds=null`(无从测量,不当 0)。

**归约层与整数化(normative)**:多行归约**必须**下推进**单条 SQL 聚合**(`SELECT count(*), bool_and(coalesce(state::text,'') = 'streaming'), FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint FROM pg_stat_replication`),由 collector 的 psql 一次往返返回**已归约的单行**,shell 只做空集短路与 JSON 成形——**禁止**让 shell 对多行做 awk 逐行归约(mysql 单行 awk 范例不覆盖多行,且 shell 浮点 max 易错)。

- **`bool_and` 的 NULL-state 三值逻辑必须显式中和(normative)**:SQL `bool_and` **忽略 NULL**——若某行 `state` 因欠权(`pg_monitor` 缺失,别人的 walsender 行 state 读成 NULL)或未来未知值读成 NULL,**裸** `bool_and(state='streaming')` 会**跳过该 NULL 行**,使「一行 streaming + 一行 NULL」聚合出 `t` → 假健康(实测 postgres 16:`bool_and over (true, NULL)=t`)。这绕过「未知 state 偏 fail-safe 判 false」意图(NULL 不是「未知枚举成员」,`state='streaming'` 对 NULL 求值是 NULL 不是 false)。故**必须**用 `coalesce(state::text,'') = 'streaming'` 把 NULL state 落进 **false**,使每行贡献非 NULL 布尔、`bool_and` 在 `count>0` 时恒非 NULL——NULL/未知/欠权 state 一律 `link_healthy=false`→critical(响错可接受,暴露权限问题,而非静默假健康)。**禁止**裸 `bool_and(state='streaming')`。
- **psql boolean 渲染必须映射成 JSON bool(normative)**:`psql -tA` 把 boolean 打成 `t`/`f`(**非** `true`/`false`、非 `1`/`0`,mirror mysql 显式 `Yes`→`true` 映射)。collector 的 JSON 成形步骤**必须**映射 `t`→`true`、其余(`f`)→`false`(空集已被 count 短路、不会落到这里);**禁止**把 `t`/`f` 直接塞进 JSON(`{"link_healthy":t}` 是非法 JSON → parse 崩 → 把健康主从录成 exception)。
- **`lag_seconds` 必须为整数**(契约 `output_schema` 是 `integer | null`):`EXTRACT(EPOCH FROM replay_lag)` 返浮点秒,**必须** `FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint` 取整(向下取整、单调,等价于先 floor 再 max);psql 把 SQL NULL 渲染成**空字符串**(非字面 `NULL`),collector 据 lag 字段为空 → 归一 `lag_seconds=null`;**禁止**输出带小数的 `lag_seconds`(会被 integer 校验 reject 或静默截断)。`replay_lag` 是 typed interval、经 `FLOOR(...)::bigint` 恒返 integer|NULL,故**无** mysql 那种字符串字段的「非数值 fail-loud」风险(唯一 fail-loud 是 psql ERROR 泄进 stdout,由 command-sub + 退出码捕获)。`replay_lag` 是主库**单时钟**测得(非跨时钟差),真负值不现实;若极端出现负值则 `FLOOR(负)` 更负、`>=warn` 为假 → 视健康,**接受**该残留、不额外裁定。

**归约正确性的验证层(normative,新技术非 mirror mysql)**:因 `ReplayTarget` 冻结的是 **collector 命令的最终 stdout(已归约三元组 JSON)**、回放时归约逻辑(SQL 聚合)**不重跑**,`max_over_rows`/`AND_over_rows` 的正确性**禁止**声称由回放 fixture 证明;**必须**由**录制时断言**保证。**注意这是一项新录制技术、不是 mirror mysql**:mysql 是 **N=1**(副本侧单值 `Seconds_Behind_Source`),其录制器只断言**已归约的标量本身**(`out["lag_seconds"]>=阈值`),**没有**多行 `max`/`AND` 可重算;postgres 的录制时 **raw-row 重算**是净新增逻辑,mysql 录制器只提供 compose/poll/脱敏脚手架、**不**提供可照抄的 reduction 重算。

录制时断言**必须同快照重算(normative)**:`replay_lag` 是**实时漂移**量(主库持续写 + apply_delay 下每刻不同),故**禁止**用「两次独立 psql 往返」(一次聚合、一次取 raw)再断言相等——两次往返跨不同 MVCC 快照、`replay_lag` 漂移会使断言 race/flaky 或假通过。**必须**用**单条查询在同一快照内同时取 raw 多行与聚合**(如 `WITH r AS (SELECT state, replay_lag FROM pg_stat_replication) SELECT json_agg(row_to_json(r)) AS raw, (SELECT count(*) FROM r), (SELECT bool_and(coalesce(state::text,'')='streaming') FROM r), (SELECT FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint FROM r) FROM r LIMIT 1`),录制器在 Python 端从该 `raw` 独立算 `max(FLOOR(EPOCH))`/`all(state=='streaming')`,断言**等于同一查询返回的聚合列**——这验证的是 collector 所用聚合 SQL 的**逻辑**在一致快照上正确(独立于另被冻结的 collector 输出)。**禁止**把录制时断言退化成「只断言已归约三元组自身」(那样 reduction 等于自证、未被测)。多 standby fixture 的作用是「冻结一个由多行派生的三元组供 DSL/parse 回放 + 作录制时同快照重算的留痕证据」,**不是**在回放时证伪 reduction bug。

**单行链路健康的 per-DB 定义必须穷举该 DB 的状态全集(normative)**:主库侧 inspector **禁止**只论证"健康"状态而对其余状态隐式兜底——**必须**对该 DB per-replica 状态字段的**全部枚举成员**显式裁定每个成员算 `link_healthy` 真还是假。对 postgres,`pg_stat_replication.state` 枚举全集为 `{streaming, catchup, startup, backup, stopping}`:**仅 `streaming` 算单行链路健康**,其余四个成员(`catchup`/`startup`/`backup`/`stopping`)**一律**算 `link_healthy=false`(继承「意外/未知 state 值偏 fail-safe 判不健康」)。其中 `backup`(standby 正在 `pg_basebackup` 拉基线)与 `startup`/`stopping`(walsender 握手/关停瞬态)是**已知会被判 critical 的正常操作态**——本契约**接受**这组**误报**(rationale:非 streaming 的副本当下不提供 apply-lag 保护、值得在快照里浮出;瞬态在单次调度快照命中率低;给其宽限则 link_down 语义无处可造,破坏「两个语义不同 semantic-abnormal」)。该"接受的误报集"**必须**写进 description 与 design,**禁止**留作未论证的隐式行为。

**空集守卫(normative)**:collector **必须先**判 `count(*)`(在线副本行数)**再**采信任何聚合标量;`count(*)==0`(零在线副本)时 collector **必须**短路输出 `(replication_configured=false, link_healthy=false, lag_seconds=null)`,**禁止**采信空集上的聚合值。注意 SQL 聚合在空集上的真实返回:`SELECT count(*), bool_and(coalesce(state::text,'')='streaming'), max(...)` 对空 `pg_stat_replication` **返回一行** `(0, NULL, NULL)`(**`bool_and` over 空集是 NULL、不是 true**,coalesce 不改变此点——无行可聚合)——故 shell **必须**据 `count==0` 显式短路成 unconfigured,**禁止**把 `bool_and` 的 NULL 当真、也**禁止**把它当 `link_healthy=true`(无论走 SQL 的 NULL 还是 shell 逐行 `AND` 的 vacuous-true,空集都**禁止**漏出 `link_healthy=true`,把单机主库捏造成健康)。

**主库侧检测边界(normative)**:主库侧空结果集**无法区分**「单机主库(从未配置复制)」与「曾有副本、现全部断开/全挂」——两者 `pg_stat_replication` 都是空集。故主库侧 inspector **必须**把空集**一律**判为 `ok` 无 finding(继承「先证后铺、不假装能测撑破的东西」),且 spec/description **必须显式声明「主库侧 inspector 无法检测副本全断,该场景留给副本侧 receiver-health 或外部拓扑探测」**。该声明**必须同时注明**:此 fallback(副本侧 receiver-health inspector,如查 `pg_stat_wal_receiver`)在**本仓当前不存在、也无本期计划**——即 postgres apply-lag 链对「standby 全挂/指错到 standby」当前**无任何 inspector 兜底**,**禁止**让读者误以为存在该兜底。主库侧 inspector **禁止**引入「期望副本数 / application_name 列表」参数去检测全断(会撞契约禁用的多实例参数词、并把运维拓扑塞进 inspector 配置)。

本需求与既有「副本侧采集视角必须聚合为 identity 且明细禁回吐给 DSL」需求**并存**:副本侧视角(N=1)归约退化为 identity,主库侧视角(N 行)按本需求的冻结函数归约;两者都**必须**在 collector 内完成归约,**禁止**把 per-replica 明细回吐给 DSL。

#### 场景:postgres 主库侧多副本按冻结函数归约(SQL 聚合 + 整数化)

- **当** `postgres.replication_lag` 连接主库,`pg_stat_replication` 返回多行(每个在线 standby 一行)
- **那么** collector 经**单条 SQL 聚合**返回已归约单行:`replication_configured=(count(*)>0)`、`link_healthy=bool_and(coalesce(state::text,'')=='streaming')`(NULL state→false)、`lag_seconds=FLOOR(EXTRACT(EPOCH FROM max(replay_lag)))::bigint`(**整数**;全行 `replay_lag` NULL → `lag_seconds=null`),shell 把 `t`→`true`/`f`→`false` 成形,DSL 只对该三元组标量比较

#### 场景:lag_seconds 必须整数化(EXTRACT EPOCH 浮点不得直出)

- **当** `max(replay_lag)` 经 `EXTRACT(EPOCH FROM ...)` 算出带小数的秒(如 `3.472814`)
- **那么** collector **必须** `FLOOR(...)::bigint` 取整成 `3` 再出,`lag_seconds` 形态恒为 `integer | null`(契约 output_schema);**禁止**直出浮点(会被 integer 校验 reject 或静默截断,使 max-of-rows 在亚秒抖动下不确定)

#### 场景:空集不得捏造健康(SQL 聚合 NULL 与 shell vacuous-true 都禁漏)

- **当** 主库侧 `pg_stat_replication` 返回零行(单机主库,或副本全断)——`SELECT count(*), bool_and(coalesce(state::text,'')='streaming'), max(...)` 此时**返回一行** `(count=0, bool_and=NULL, max=NULL)`(`bool_and` over 空集即便加 coalesce 仍是 NULL,因无行可聚合)
- **那么** collector **必须据 `count==0` 显式短路**输出 `(replication_configured=false, link_healthy=false, lag_seconds=null)`;**禁止**采信空集聚合值——`bool_and` over 空集是 `NULL`(不是 `true`),**禁止**当真或当 `link_healthy=true`;status=`ok`、无 finding

#### 场景:主库侧无法检测副本全断须显式声明边界

- **当** 一个曾有 standby 的主库其副本**全部**断开(`pg_stat_replication` 变空集)
- **那么** 主库侧 inspector 判为 `ok` 无 finding(与单机主库不可区分),且 spec/description **必须**显式声明「主库侧无法检测副本全断」;**禁止**为检测全断而引入期望副本数 / application_name 列表参数

#### 场景:滞后取最大、链路取逻辑与(max 与 AND 都须非平凡)

- **当** 主库侧多行,A `state='streaming'` 且 `replay_lag=3s`、B `state='streaming'` 且 `replay_lag=40s`(**两个 non-NULL 且 distinct** 的 streaming 行)、C `state` 为非 streaming(`replay_lag` NULL;录制取 `backup`——见 semantic-abnormal 场景的 recipe)
- **那么** `lag_seconds=40`(`max` 在两个 non-NULL 值间**取较大者 40 而非 3**——非平凡 max,**禁止**用「一行有值一行 NULL」的拓扑,那会让 max 退化成 identity 而无从验证)、`link_healthy=false`(`AND(streaming,streaming,非streaming)`——非平凡 AND,**false 来自 C 而非单行**);归约在 collector 内完成,DSL 不见 per-replica 行。此拓扑同时非平凡兑现 `max_over_rows`(≥2 distinct non-NULL)与 `AND_over_rows`(≥1 非 streaming 混 streaming),是 reduction 录制时断言的载体

#### 场景:catchup 行 replay_lag 常为 NULL 时 lag_seconds 反映 streaming 行

- **当** 主库侧两行,A `state='streaming'` 且 `replay_lag=2s`、B 为非 streaming 行(`backup`/`catchup`)且 `replay_lag=NULL`(非 streaming 行通常无 replay 时间样本,典型现实)
- **那么** `link_healthy=false`(B 非 streaming)、`lag_seconds=2`(B 的 NULL 被 max 跳过,只见 A);**故障由 `link_healthy=false` 的 critical 兜住、不靠 lag_seconds**——spec/description **必须**说明「`link_healthy=false` 时 `lag_seconds` 可能反映健康行而非落后行,仅作信息、不作故障判据」,**禁止**用乐观的「catchup 必有数值 lag」示例掩盖此点
- **且** 反过来,当 `link_healthy=false` 时即便某 streaming 行有**大滞后**(如 A streaming `replay_lag=50s` + B catchup),lag finding 的 `link_healthy` guard 为假 → **不**触发 lag finding,A 的 50s 真滞后**不在本快照体现**(只喷 link-down critical);这是 AND/critical-tier 模型的固有取舍,spec/description **必须**明示「混合拓扑下 `link_healthy=false` 会吞掉 streaming 行的真实滞后」,**禁止**让读者误以为 lag 维度在 link 不健康时仍生效

#### 场景:非 streaming 正常操作态被判 critical 属接受的误报

- **当** 一个 standby 正在 `pg_basebackup` 拉基线(其 walsender 行 `state='backup'`),或 walsender 处于 `startup`/`stopping` 瞬态
- **那么** `link_healthy=AND(state=='streaming')=false` → 产生 critical「link down」finding(**已知误报**:这些是正常操作态);本契约**接受**该误报,且 description 与 design **必须**把 `{catchup, startup, backup, stopping}` 显式列入「接受的误报集」并给 rationale;录制 healthy fixture 时**必须** poll 至该行 `state=='streaming'`(**禁止**在 `backup`/`startup` 窗口冻结 healthy,否则录进一个 critical 当健康)

#### 场景:NULL state(含部分欠权)经 coalesce 落 false 而非被 bool_and 吞

- **当** 多 standby 拓扑下**部分行** `state` 因欠权(对别人的 walsender 行)或未知值读成 NULL,另有真 streaming 行(裸 `bool_and(state='streaming')` 会跳过 NULL 行、聚合出 `t` 假健康)
- **那么** collector 的 `bool_and(coalesce(state::text,'')='streaming')` 把 NULL state 落进 **false** → `link_healthy=false` → critical(**响错**:暴露权限/异常,而非静默假健康);**禁止**裸 `bool_and(state='streaming')`(实测 postgres 16 `bool_and over (true,NULL)=t`)。该 coalesce 中和**消除** state 列欠权/全欠权的假健康路径(全欠权→所有行 false→critical)

#### 场景:streaming 但 replay_lag NULL 的残留可信度边界(仅 lag 列欠权 vs 空闲)

- **当** 某行 `state='streaming'`(state 列可见)但 `replay_lag` **单独**读成 NULL —— 二义:(a) 空闲已追平(真健康)/ (b) 巡检账户对 lag 列欠权(state 可见、仅 lag 列不可读;实际任意滞后)
- **那么** collector 归一输出 `link_healthy=true` + `lag_seconds=null`(形态不可区分该残留二义);经上一场景的 coalesce 中和后,**仅剩「state 可见而 lag 列单独 NULL」这一窄残留**靠 description 硬前提 `pg_monitor`/superuser 兜;**且** wave-3 录制门(tasks 1.2)**必须**实证欠权下 `state` 与 `replay_lag` 列各自的可见性——若该窄残留(state 可见、lag 列单独 NULL)实证可达,collector **必须**加防护分支并补回归 fixture,**禁止**把该已知假健康路径只留在 design 散文里而 spec 无痕、fixture 无守卫
