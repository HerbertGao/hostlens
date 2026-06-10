# service-inspector-contract 规范

## 目的

定义 service(单实例服务)inspector 契约——管辖范围与既有 seed 祖父化、采集服务且连接参数注入安全、secret 经 env 注入从不进命令字符串、遵守 service 层失败分类、声明超时并限制输出规模、跨 local 与 SSH target 无分叉(secret 投递有 SSH 前提)、附双轨 fixture 且证检出能力、契约边界止于单实例。
## 需求
### 需求:本契约管辖范围与既有 seed 祖父化

本 `service-inspector-contract` 的**全部需求**(连接注入安全 / secret / 失败分类 / 超时输出 / 跨 target 无分叉 / 双轨 fixture / 单实例边界)**仅管辖本 spike 起新增或迁移**的 service inspector;作为新立(ADDED)契约,它**向前生效**、**不回溯绑定**已归档的 pre-spike inspector。曾有两个 pre-spike 既有 seed(`redis.slowlog`、`postgres.bloat_tables`)被祖父化;此二者**均已由独立 follow-up 迁移至全合规**——`redis.slowlog`(secret 改 `HOSTLENS_REDIS_PASSWORD` + remap 到 `REDISCLI_AUTH`、补 default-阈值 semantic-abnormal fixture 轨)与 `postgres.bloat_tables`(secret 改 `HOSTLENS_POSTGRES_PASSWORD` + remap 到原生 `PGPASSWORD`、补 `PGCONNECT_TIMEOUT=5`、列表输出截断 top-N + total 计数、补 finding-trigger 轨)现**均受本契约管辖**、不再祖父化。**至此祖父化 seed 列表归零、祖父条款闭合**:**无任何在册祖父化 inspector**。本需求作为「契约向前生效、不回溯绑定已归档 pre-spike inspector」的**管辖范围原则**继续生效,但**不再豁免任何具体 inspector**——日后若审计发现某 pre-spike inspector 与本契约漂移,**必须**经独立 follow-up 迁移合规,**禁止**新立祖父化豁免。

#### 场景:契约不回溯绑定已归档 pre-spike inspector

- **当** 审计某 inspector 对本契约各需求的合规性
- **那么** 仅**本 spike 起新增或迁移**的 service inspector 须满足本契约 MUST;已迁移的 `redis.slowlog`、`postgres.bloat_tables` 现受本契约管辖须满足全部 MUST;祖父化 seed 列表已归零、**无在册祖父化 inspector**,**禁止**据「契约不回溯绑定」为任何在册 inspector 新立祖父化豁免

#### 场景:redis.slowlog 迁移后受契约管辖

- **当** 审计 `redis.slowlog` 对本契约 secret / argv / 双轨 fixture 需求的合规性
- **那么** 它**必须**满足全部 MUST:secret 声明为 `HOSTLENS_REDIS_PASSWORD` 并在 collector 内 remap 到 `REDISCLI_AUTH`、命令串**禁止**含 `-a ` 明文密码 flag、**必须**附 default-阈值下触发 finding 的 semantic-abnormal fixture;**禁止**再将其按祖父化豁免

#### 场景:postgres.bloat_tables 迁移后受契约管辖

- **当** 审计 `postgres.bloat_tables` 对本契约 secret / 超时 / 输出规模 / 双轨 fixture 需求的合规性
- **那么** 它**必须**满足全部 MUST:secret 声明为 `HOSTLENS_POSTGRES_PASSWORD` 并在 collector 内 remap 到 client 原生 `PGPASSWORD`、client 连接超时 `PGCONNECT_TIMEOUT=5` **必须**小于 `collect.timeout_seconds`、列表形态输出 **必须**经 `max_results` 参数截断为 top-N 并附 `total_tables` total 计数标量、**必须**附 default-阈值下触发 finding 的 semantic-abnormal fixture(`bloated.json`)与降阈值触发的 finding-trigger 轨;**禁止**再将其按祖父化豁免

### 需求:service inspector 采集服务且连接参数注入安全

service-dependent inspector(依赖外部服务进程:nginx / mysql / postgres / redis / docker 等)**当**经服务 client CLI(`redis-cli` / `psql` / `mysql` / `curl` / `docker` 等)在 `collect.command` 内采集时,**必须**把所用 client 二进制声明于 `requires_binaries`。本契约**不**规定 client CLI 为唯一采集方式——经文件读取(`requires_files`,如 nginx 日志)、本地 socket、HTTP 探针(curl)、systemd(`systemctl`)或服务自带导出指标采集**均**允许,只需各自声明对应 `requires_binaries` / `requires_files` 前提。无论采集方式如何,一切把调用方连接参数(`host` / `port` / `dbname` / endpoint / 日志路径等)插入 `collect.command` 的位置**必须**遵守《inspector-authoring-contract》注入安全三件套:经 `| sh`(或数组 `| map('sh')`)引用、`parameters` JSON Schema 用 `pattern` 收紧取值域、**禁止**裸 `{{ param }}` 拼进可执行位置。本需求**引用**该契约不重述其余细则(collector 做派生 / DSL 只比标量 / `for_each` 单绑定 / 输出键防遮蔽),只补"服务域"维度。

#### 场景:连接参数经 pattern 与 sh 引用进 shell

- **当** 某 service inspector 把 `host` / `port` / `dbname` / 日志路径等连接参数插入 collector 命令
- **那么** 该参数**必须**在 `parameters` 中声明收紧的 `pattern`(数值参数除外)、且在命令中经 `| sh` 引用;**禁止**以裸 `{{ param }}` 形式出现在可执行位置

#### 场景:经 client CLI 采集时声明该 client 二进制

- **当** 某 service inspector **经** client CLI 采集
- **那么** 该 client **必须**列入 `requires_binaries`,使 runner preflight 在缺失时按失败分类处理(见对应需求);**当**改经文件 / socket / HTTP / systemd 采集时,**必须**改为声明对应的 `requires_files` / 探针二进制前提

### 需求:service inspector 的 secret 必须经 env 注入且从不进命令字符串

（适用范围见首条「本契约管辖范围与既有 seed 祖父化」需求:下述 secret 规则对**本契约管辖**的 inspector 为 MUST;两个 pre-spike seed `redis.slowlog`、`postgres.bloat_tables` 均已迁移、现受本契约管辖,祖父化 seed 列表已归零、无在册豁免。）

service inspector 的连接凭据(密码 / token)**必须**经 manifest `secrets` 字段声明、由 runner 经 `env=secrets_env` 注入。声明的 secret 名**必须**用 `HOSTLENS_` 前缀(如 `HOSTLENS_REDIS_PASSWORD` / `HOSTLENS_MYSQL_PWD` / `HOSTLENS_POSTGRES_PASSWORD`)——这对齐既有 `ssh-execution-target` 契约(其 spec 规定 SSH secret 投递走 `AcceptEnv HOSTLENS_*` + `HOSTLENS_` 前缀变量名),使 secret 能跨 SSH 到达远端。collector 内**必须**把该 `HOSTLENS_` 变量 **remap** 到 client 原生 env 鉴权通道(`redis-cli` 读 `REDISCLI_AUTH`、`mysql` 读 `MYSQL_PWD`、`psql` 读 `PGPASSWORD`),使凭据**不进** `argv`。**禁止**把凭据经 `{{ }}` 渲染进命令字符串;**禁止**以会进 `argv`(全局 `ps` 可见)的命令行明文密码参数(如 `mysql -p<pwd>` / `redis-cli -a <pwd>`)传递。本 spike **不**引入凭据文件(`--defaults-extra-file` 等)或其它新 secret 机制;client **无**原生 env 鉴权通道(如 `curl` 的 bearer token)的 secret 机制留对应 wave 定(本 spike 两探针的 client 均有原生 env 通道),届时仍**禁** `argv` 明文。

凡 manifest 声明了某 secret,runner preflight 即要求该 env **存在**于环境(按 `name in os.environ` 判定);**无鉴权**实例(如无密码 Redis)需显式导出**空串**(`HOSTLENS_REDIS_PASSWORD=`)使 preflight 通过,collector 内再按 `[ -n "$VAR" ]` 分流有/无鉴权——空串"存在"即满足声明前提,与"完全不设 env"(→ `requires_unmet`,见失败分类)区分。

**SSH 投递**:runner 的 SSH target 经 AsyncSSH `conn.run(env=)` 传 env(命令字符串绝不改写),该路径**受远端 sshd `AcceptEnv` 约束**(默认仅 `LANG`/`LC_*`)。故 secret 用 `HOSTLENS_` 前缀 + 远端配 `AcceptEnv HOSTLENS_*` 是其跨 SSH 到达的**前提**(既有 ssh 契约已定的路径);本契约**不**声称在默认(未配 AcceptEnv)sshd 下透明跨 SSH。

#### 场景:凭据经 HOSTLENS_ 声明 remap 到 client 原生 env 通道

- **当** 某 service inspector 需要连接凭据
- **那么** 该凭据**必须**以 `HOSTLENS_` 前缀经 `secrets` 声明、并在 collector 内 remap 到 client 原生 env 鉴权通道;**禁止**出现 `{{ password }}` 插值,**禁止** `-p<pwd>` / `-a <pwd>` 等会进 `argv` 的命令行明文密码

#### 场景:声明 secret 即强制其 env 存在

- **当** 某 manifest 声明了 `secrets: [X]` 但环境未设 `X`(连空串都没有)
- **那么** runner preflight **必须**标 `status=requires_unmet`(与缺 client 二进制并列),collector 不执行;无鉴权实例**必须**显式导出空串 `X=` 才能跑

#### 场景:回显的凭据不落 fixture

- **当** 录制 fixture 时 client 把凭据回显进 stdout/stderr(如连接错误带连接串)
- **那么** 产出 fixture 的 stdout/stderr 中**禁止**出现明文凭据;录制器**必须**在写盘前脱敏

### 需求:service inspector 必须遵守 service 层失败分类

本契约规约 service inspector 在 **service-collector 层**的失败语义,落入三类:(1)**缺前提**——目标缺 client 二进制、缺 `requires_files` 前提、或缺已声明的 secret env → runner preflight 标 `status=requires_unmet` 并 skip,**不**中断同 run 其它 inspector;(2)**采集失败**——服务存在但不可达 / 认证失败 / client 返回非预期(conn refused / NOAUTH / Access denied / 非数值回复)→ collector **必须** fail-loud(非零退出 + 空 stdout)→ parse 异常 → `status=exception`;(3)**成功**——服务可达且返回有效数据 → `status=ok`。

runner 另有 `timeout`(采集超 `collect.timeout_seconds`)与 `target_unreachable`(`target.exec` 抛 `TargetError`,如 SSH 隧道断)两个**正交传输/超时层**状态,由既有 `inspector-plugin-system` 契约管辖、对所有 inspector 同构,**不在**本 service 层契约的收敛范围——故本契约不声称"结果必落三态之一"(SSH 断会落 `target_unreachable`、整体挂会落 `timeout`)。区分点:**服务端**(redis/mysql 端口)连不上 → 目标 host 上 client 非零退出 → `exception`;**目标 host 本身**够不到 → `target_unreachable`。

**禁止**在服务不可达 / 认证失败时由 collector 伪造"健康默认值"对象(如 `{"used_pct":0}`)使其被判 `status=ok`——这会让监控在后端宕机时静默报"健康"。

#### 场景:缺 client 二进制或缺声明 secret 映射到 requires_unmet

- **当** 目标主机缺少某 service inspector `requires_binaries` 声明的 client(如无 `mysql`)、或缺其 `secrets` 声明的 env
- **那么** runner preflight **必须**标 `status=requires_unmet` 并 skip、报告中标注;**禁止**报错中断同 run 其它 inspector,**禁止**误判为 `exception`

#### 场景:服务不可达或认证失败映射到 exception 而非 ok

- **当** client 已存在但服务不可达(conn refused)、认证失败(NOAUTH / Access denied)或返回非预期格式
- **那么** collector **必须**以非零退出 + 空 stdout fail-loud,使结果为 `status=exception`;**禁止**回吐任何可被解析为成功的"默认健康"对象

#### 场景:真实空结果与采集失败可区分

- **当** 服务可达且其真实状态恰为"零 / 空"(如连接数为 0、慢日志为空)
- **那么** collector **必须**输出携带该真实零值的有效对象(`status=ok`),与采集失败的空 stdout(`status=exception`)**区分开**

### 需求:service inspector 必须声明超时并限制输出规模

service inspector **必须**在 manifest 显式声明 `collect.timeout_seconds`,且 client 的连接超时(如 `redis-cli -t` / `mysql --connect-timeout` / `psql` 经 `PGCONNECT_TIMEOUT` env 或连接串 `connect_timeout=`)**必须**小于 `collect.timeout_seconds`,使服务不可达时快速失败而非 hang 满整个超时窗。collector 输出**必须**为聚合标量小 JSON(计数 / 字节 / 派生率);需返回列表时**必须**在 collector 内截断为 top-N(N 由 manifest 参数声明)并附 total 计数,**禁止**回吐高基数明细(如全部活动连接逐行)。本约束是**作者纪律**(prose + snapshot 验收),与输出键命名约定同口径——loader **不**做机器式字节门(守"零新 infra";机器式 manifest lint 列为后续工作)。

#### 场景:客户端连接超时小于采集超时

- **当** 某 service inspector 声明 `collect.timeout_seconds`
- **那么** 其 client 调用**必须**设置小于该值的连接超时,使服务不可达时在采集超时前快速 fail-loud

#### 场景:输出为聚合标量而非高基数明细

- **当** 某 service inspector 的底层查询可能返回大量行(如所有连接 / 所有 key)
- **那么** collector **必须**在命令内聚合为标量或截断列表 + 计数;**禁止**把高基数结果集整体回吐进输出 JSON

### 需求:service inspector 跨 local 与 SSH target 无分叉(secret 投递有 SSH 前提)

service inspector **必须**对 `local` 与 `ssh` target 用**同一** manifest、**同一** collector 命令文本、**同一** secret 声明,**禁止**在 manifest / collector 内出现按 target 类型分叉的连接参数约定或失败处理逻辑(无 target-specific 旁路)。该「无分叉」是**可经代码检视机械核验**的属性(检 manifest 无 target 条件分支),CI 在 local 上验证非 secret 行为。

**secret 跨 SSH 走既有契约的 `HOSTLENS_` 路径**:runner 的 SSH target 经 AsyncSSH `conn.run(env=)` 传 env(命令字符串绝不改写),该路径受远端 sshd `AcceptEnv` 约束(默认仅 `LANG`/`LC_*`)。既有 `ssh-execution-target` 契约已定 SSH secret 投递路径 = `HOSTLENS_` 前缀变量名 + 远端 `AcceptEnv HOSTLENS_*`;本契约的 secret 需求**遵循**之(secret 声明 `HOSTLENS_*`、collector remap)。故需 secret 的 inspector 在 SSH 上的运行**前提**是远端配 `AcceptEnv HOSTLENS_*`;本契约**不**声称在未配 AcceptEnv 的默认 sshd 下透明跨 SSH。非 secret 行为由 runner 对 target 的统一 dispatch 结构性等价。**注**:两个 pre-spike seed `redis.slowlog`(用 `HOSTLENS_REDIS_PASSWORD`)、`postgres.bloat_tables`(用 `HOSTLENS_POSTGRES_PASSWORD`)均已迁移合规,祖父化 seed 列表已归零、无在册 `HOSTLENS_`-命名漂移项。

#### 场景:manifest 无 target 分叉逻辑

- **当** 检视某 service inspector 的 manifest 与 collector 命令
- **那么** 其连接参数传入、secret 引用、失败处理**必须**不含按 `target.type` 分叉的分支;**禁止**为某一 target 特设旁路

#### 场景:secret inspector 在 SSH 上遵循 HOSTLENS_ + AcceptEnv 路径

- **当** 某需 secret 的 service inspector 跑在 ssh target 上
- **那么** 其 secret **必须**以 `HOSTLENS_` 前缀声明,且其到达远端的**前提**是远端 sshd 配 `AcceptEnv HOSTLENS_*`;该前提**必须**被文档式声明(manifest 注释 / 运行文档),**禁止**默认它在未配 AcceptEnv 的 sshd 下自动成立

### 需求:service inspector 必须附双轨 fixture 且证检出能力

service inspector **必须**附带用 fixture 录制器(`inspector-fixture-recorder`)对真实服务录制的 `ReplayTarget` 兼容 fixture 与 snapshot 测试,经离线回放确定性出 `InspectorResult`,**禁止**手写 fixture、**禁止**日常 CI 依赖真实服务/网络。fixture 分两轨,验收**必须**满足:(1)**finding-trigger fixture**——**允许**对健康服务用**降低的阈值参数**触发 finding,只用于验证 finding wiring;(2)**semantic-abnormal fixture**——对**真实异常态**服务录制,且 snapshot 断言其在 manifest **默认阈值**下即产出预期 severity + message。

验收分**机械门**与**人工 review 门**两层,不混为一谈:

- **机械门(可机械判定)**:(a) 适用性——**凡 manifest 的 `findings` 列表非空**,该 inspector **必须**附至少一份在 manifest **默认阈值**下触发 finding 的 fixture(读 `findings` 是否非空 + 用默认参数回放断言 finding 非空,均机械可判);**不得**以"本 inspector 不依赖异常字段"为由免除。(b) 两轨区分——finding-trigger 可降阈值触发,semantic-abnormal **必须**在默认阈值下触发,故"同一健康 fixture 改阈值冒充 semantic-abnormal"会被"默认阈值下无 finding"机械暴露。
- **人工 review 门(不可机械判定,显式承认)**:fixture 文件只存命令+输出、**不带 provenance**,无法从字节机械区分该默认阈值触发的 fixture 是录自**真实异常态服务**还是手工构造的等价输出。"semantic-abnormal 录自真实异常态"是**人工 review 条件**(reviewer 查录制脚本/compose 是否真造了异常),本契约**不**声称它机械可判。

仅有 finding-trigger fixture(健康态 + 低阈值)**不满足**验收——它只证明阈值比较生效,未证明 collector 在真实异常下输出正确字段。

#### 场景:findings 非空的 inspector 必须有默认阈值触发的 semantic-abnormal fixture

- **当** 某 service inspector 的 `findings` 列表非空
- **那么** 其测试集**必须**含一份对真实异常态录制的 semantic-abnormal fixture,snapshot 断言该 fixture 在 manifest **默认阈值**下产出预期 severity + message;**禁止**仅以"健康态 + 人为低阈值"的 finding-trigger fixture 判该 inspector 验收通过,**禁止**以"不依赖异常字段"为由免除 semantic-abnormal fixture

#### 场景:离线回放确定性出结果

- **当** 在任意平台(含 macOS / CI)对某 service inspector 运行其 snapshot 测试
- **那么** 它**必须**经 `ReplayTarget` 回放录制的 fixture、不触达真实服务/网络,并产出与快照一致的确定性 `InspectorResult`;非确定输出(随机内存值 / 时间戳)**必须**在录制时冻结

### 需求:本契约边界止于单实例

本 spike 立的 service-inspector-contract **仅**覆盖单服务实例的采集语义。多实例 / 复制拓扑相关契约——primary/replica 角色识别与选择、replication lag 语义与单位归一、未配置复制与复制故障的区分、多副本指标聚合成标量、确定性制造 lag 的 fixture 录制——**明确不在**本契约范围,留独立 replication spike 按真实实现补充。本契约**禁止**被援引为多实例 inspector 的完备依据。

#### 场景:多实例语义不由本契约规定

- **当** 评估某 replication / 多实例 inspector 的契约依据
- **那么** 本 service-inspector-contract **禁止**被当作其完备契约;多实例语义**必须**由后续独立 replication spike 确立
