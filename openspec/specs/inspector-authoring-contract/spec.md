# inspector-authoring-contract 规范

## 目的

定义 Inspector 作者契约——一切抽取与数值派生必须在 collector 命令内完成、collector 输出顶层键使用约定命名防 parameter 遮蔽、命令注入安全三件套必须应用、运行前提文档式声明(schema 不提供机器门)、契约由跨数据形态硬 inspector 证明且不引入新基础设施。
## 需求
### 需求:一切抽取与数值派生必须在 collector 命令内完成

Inspector 的所有数据抽取、字段解析与数值派生**必须**在 `collect.command`（shell / `jq` / SQL 计算列）内完成并产出**已关联、已派生**的 JSON；finding 规则（`when` / `for_each`）**只允许**对已就绪的标量/集合做阈值与成员比较。理由：Finding DSL 白名单仅 `len/sum/min/max/any/all/now/float/int`（`inspectors/dsl.py`），**禁止** string 操作 / split / regex / 推导式 / lambda，且 `for_each` 仅单绑定 `"<expr> as <var>"`——跨命令 / 跨行关联同样**必须**在 collector 内完成。**禁止**试图在 finding 表达式里做解析或派生（会撞 DSL 白名单或静默失败）。

#### 场景:数值派生在 collector 内

- **当** 一个 inspector 需要「磁盘空闲百分比」「复制延迟秒数」「bloat 比率」等派生量
- **那么** 该量**必须**由 collector 命令算出并写入输出 JSON（shell 算术 / `jq` / SQL 计算列），finding 规则只对该数值做阈值比较；**禁止**在 finding 表达式里用算术或字符串操作现算

#### 场景:跨行关联在 collector 内

- **当** 一个 inspector 需要把两条命令的输出或多行结果关联（如容器列表 join inspect 详情）
- **那么** 关联**必须**在 collector 命令内完成（单条命令 / 管道 / `for_each $(...)` 展开）后吐单一 JSON；**禁止**依赖 finding 层做 join（`for_each` 单绑定无法表达）

### 需求:collector 输出顶层键必须使用约定命名以防 parameter 遮蔽

由于 finding 求值上下文将 `output` 与 `parameters` 合并、且**同名 `parameter` 会遮蔽 `output` 键**，collector 输出的顶层结果键**必须**取自 `results` / `items` / `records` 之一，**禁止**与任一已声明 parameter 同名。

#### 场景:输出键不与参数名碰撞

- **当** 一个 inspector 声明了名为 `endpoints` 的 parameter，并需要输出一组结果
- **那么** 输出顶层键**必须**为 `results`（或 `items` / `records`），**禁止**也叫 `endpoints`——否则 finding 上下文中该键被参数值静默遮蔽

### 需求:命令注入安全三件套必须应用

凡把调用方参数（尤其数组 / 字符串）插入 `collect.command` 的 inspector，**必须**：(a) 经 `| sh`（shlex.quote）过滤后再进 shell；(b) 在 `parameters` JSON Schema 用 `pattern` 收紧取值域（如 `host:port` 正则）；(c) 不得用裸 `{{ param }}` 直接拼进可执行位置。

#### 场景:数组参数安全进 shell

- **当** 一个 inspector 把数组参数（如 endpoint 列表）展开进 shell 循环
- **那么** 每个元素**必须**经 `| map('sh')` / `| sh` 引用，且参数 schema **必须**对元素施加 `pattern` 校验；**禁止**未引用直接插值

### 需求:运行前提必须文档式声明（schema 不提供机器门）

Inspector 的运行前提——客户端 / 服务版本下限（如 Redis 6+、MySQL 8.0+）、必须由调用方提供的标识（如 JVM PID）、平台依赖（如 GNU `date -d` 的 Linux-only）、所需 `--json` 能力客户端——**必须**在 `description` 与 `tags` 中显式声明，并由《作者契约》文档统一约定措辞（注意 tag 正则 `^[a-z][a-z0-9_-]*$` 不含 `+`，故版本下限用 `redis6` / `mysql8` / `json-client` 等 tag 配合 `description` 自由文本表达，**禁止**写会被 loader 拒的 `redis6+`）。因 manifest schema `extra="forbid"` 且无 `min_binary_version` 字段，**禁止**依赖任何不存在的 schema 字段做机器式版本门；不满足前提时由 preflight 的 `requires_binaries` 探测或主命令失败兜底。

#### 场景:版本敏感 inspector 声明前提

- **当** 一个 inspector 依赖 `redis-cli --json`（Redis 6+）
- **那么** 其 `description` **必须**写明「需 Redis 6+ 且 `redis-cli` 支持 `--json`」、`tags` 含相应标记；**禁止**通过新增 manifest 字段来声明（会被 `extra="forbid"` 拒）

### 需求:作者契约必须由跨数据形态硬 inspector 证明，且本提案不引入新基础设施

本契约**必须**由覆盖三种数据形态的硬 inspector 证明——SQL 型（`postgres.bloat_tables`）、容器 JSON 型（`docker.containers.restart_loop`）、版本敏感 CLI 型（`redis.slowlog`）。**本提案**内**禁止**改动 inspector manifest schema、**禁止** enable `hook.py`、**禁止**新增 `sql_result` parse format（模式 B 下 redis 的 defer 是把「该场景需 hook.py」记为**未来独立提案**的触发证据，而非在本提案里加 infra——故本提案零新 infra 在两种模式下都成立）。验收为**二选一成功模式**，**禁止**模糊处理：

- **模式 A**：三个 inspector 各有 `ReplayTarget` fixture + snapshot 测试。
- **模式 B**：两个（pg + docker）有 fixture + snapshot，第三个（redis）以**附具体再现证据的 defer** 收尾——该 defer **必须**粘出真容器上 `redis-cli --json SLOWLOG GET` 的实际输出（证明 `--json` 对二进制 command-args 渲染损坏或不可靠）并写入 `design.md` 作为首个 `hook.py` 触发证据。

**仅贴「defer」标签、无真容器再现证据，不计为契约证明成功**（防止 spike 以「找到边界」之名实则什么都没证）。

#### 场景:硬 inspector 经 ReplayTarget 验证

- **当** 实现一个本契约下的硬 inspector
- **那么** 它**必须**附带录制的 `ReplayTarget` fixture 与 snapshot 测试、且仅使用现有 manifest 字段与 4 种 parse format（raw/table/json/kv）；**禁止**引入 `hook` / `sql_result` / 新 capability 值

#### 场景:边界用例允许证据驱动 defer

- **当** `redis.slowlog` 在「无 hook.py / 无 sql_result」约束内无法干净还原二进制 command-args
- **那么** **允许**收窄为只报时长+计数（仍纯 YAML，须附 fixture + snapshot），或以 defer 收尾——但 defer **必须**粘出真容器 `redis-cli --json SLOWLOG GET` 的实际损坏/不可靠输出作为再现证据并写入 `design.md`；**禁止**仅凭一句「defer」而无再现证据就判通过

### 需求:容器适用性——inspector 声明 docker target 的判据

一个 inspector **仅当**其采集信号在「collector 命令跑在单个容器的 PID / mount / net namespace 内、读取该容器自身的进程 / 应用 / 文件 / 网络状态」时**正确且有意义**，才**允许**在 `targets` 中声明 `docker`。**禁止**把读取 host 全局硬件 / 内核 / init / 块设备 / 物理内存 / 时钟 / host 包管理 / host 认证状态的 inspector 声明 `docker`——这类信号在容器内要么读不到、要么读到的是 **host 共享值造成误归因**（最危险，因为不报错而是静默报错值）、要么容器视角本身误导。

**判据按 collector 的实际读取源逐项判定，禁止按域名通配符整域放行**——同一域内不同 inspector 的读取源可能分属容器隔离与 host 全局两侧（如 `log.exception_burst` 读容器文件 vs `log.tail.error_burst` 读 host journal；`linux.process.zombies` 走 PID namespace vs `linux.process.fd_usage` 读 `/proc/sys` 内核全局 sysctl）。作者必须打开 collector 命令确认其读取的每个源在容器内是否隔离，**不得仅凭 inspector 落在「进程域」「日志域」就声明 `docker`**。

- **允许声明 `docker`**（逐项列举，不用整域通配）：
  - 应用服务类（容器「一容器一应用」，经容器内 CLI 连本容器内服务）：`nginx.{config_test,error_rate,health}` / `mysql.{connection_usage,replication_lag,slow_queries}` / `postgres.{bloat_tables,connection_usage,long_queries,replication_lag}` / `redis.{memory_usage,persistence,replication_lag,slowlog}`（逐项列举——本契约禁止整域通配，新增同域 inspector 须重新按读取源评审，不自动继承）
  - 语言运行时类（容器内单进程）：`jvm.{gc,heap,threads}` / `go.{goroutines,heap}`
  - 进程级（走 PID namespace 的命令）：**仅** `linux.process.zombies`（`ps axo`）/ `linux.process.critical_alive`（`pgrep`）
  - 应用日志类（读容器自身日志文件）：**仅** `log.exception_burst`（`cat {{log_path}}`，mount namespace）
  - 网络类（容器 netns 视角即为目标视角）：`net.{connections,listening_ports}` / `net.dns.resolve` / `net.dependency.tcp_check` / `net.tls.{cert_expiry,chain_validity}`
- **禁止声明 `docker`**（保持 `local` / `ssh`）：
  - host 硬件：`linux.cpu.*`（cpufreq / throttling / 全局 top_processes）
  - host 块设备与文件系统：`linux.disk.*` / `linux.fs.*`
  - host 共享内核：`linux.kernel.*`（dmesg / oom / taint）
  - host 物理内存与 swap：`linux.memory.*`（容器读 host `/proc/meminfo` 是 host 内存，**非** cgroup 限制——误归因）
  - **读 `/proc/sys/*` 内核全局 sysctl 的进程类**：`linux.process.fd_usage`（`/proc/sys/fs/file-nr`）/ `linux.process.total`（`used_pct` 的分母 `/proc/sys/kernel/pid_max`）——`/proc/sys/*` 非 namespace 隔离，容器内读到 host 全局值（同样的误归因，与「进程域」名义无关）
  - **读 host systemd journal 的日志类**：`log.tail.error_burst`（`journalctl`）——容器多无 journald（空假阴性）或 bind-mount 到 host journal（误归因），是 host-journal inspector 而非 app-log inspector
  - host init / 调度：`linux.systemd.*` / `linux.cron.*`
  - host 系统级：`linux.system.*`（load_avg / reboot_required）/ `system.uptime`（实抽 host load average，`/proc/uptime` 与 `uptime` 均非 namespace 隔离）
  - host 时钟：`net.ntp.drift`
  - host 包管理与补丁：`pkg.*`
  - host 认证与安全基线：`security.*`
  - docker 自身管控类：`docker.*`（需 docker-in-docker，非目标）

**capability gate 是兜底而非主防线**：DockerTarget 不声明 `Capability.SSH`、其 `systemd` capability 靠探测 `systemctl` 是否存在——故要求 `ssh` / `systemd` capability 的 inspector 即便误声明 `docker` 也会被 preflight `requires_unmet` 挡掉。但**误归因类**（如 memory 读 host `/proc/meminfo`、`linux.process.fd_usage` 读 `/proc/sys/fs/file-nr`）capability gate **挡不住**——它们只要 `shell` capability，preflight 不拦，collector 照跑、静默返回 host 值。必须靠本判据 + 内容式 meta-guard（见下场景）在作者侧拦住。

#### 场景:应用服务 inspector 允许声明 docker

- **当** `redis.memory_usage` 经容器内 `redis-cli` 连接本容器内的 redis 实例采集内存
- **那么** **允许**在其 manifest `targets` 声明 `docker`（采集信号是本容器内 redis 的真实状态，容器视角即目标视角）

#### 场景:host 内存 inspector 禁止声明 docker

- **当** `linux.memory.pressure` 读取 `/proc/meminfo` / `/proc/pressure/memory`
- **那么** **禁止**在其 manifest `targets` 声明 `docker`——容器内读到的是 host 物理内存而非该容器的 cgroup 限制，会造成静默误归因；该 inspector 必须保持 `targets: [local, ssh]`

#### 场景:host 共享资源 inspector 禁止声明 docker

- **当** 一个 inspector 读取 host 硬件 / 内核 / 块设备 / init / 时钟 / host 包管理 / host 认证状态（如 `linux.cpu.throttling` / `linux.kernel.oom_killer` / `linux.systemd.failed_units` / `net.ntp.drift` / `pkg.pending_updates` / `security.failed_logins`）
- **那么** **禁止**声明 `docker`，必须保持 `local` / `ssh`

#### 场景:读 /proc/sys 内核全局 sysctl 的 inspector 禁止声明 docker（与域名无关）

- **当** 一个 inspector 的 collector 读取 `/proc/sys/*`（内核全局 sysctl，非 namespace 隔离），即便它落在「进程域」（如 `linux.process.fd_usage` 读 `/proc/sys/fs/file-nr`、`linux.process.total` 的 `used_pct` 分母读 `/proc/sys/kernel/pid_max`）
- **那么** **禁止**声明 `docker`——容器内读到 host 全局值造成静默误归因；判据看**读取源**不看域名，同域的 `linux.process.zombies` / `linux.process.critical_alive`（走 PID namespace）才允许

#### 场景:读 host journal 的日志 inspector 禁止声明 docker

- **当** 一个日志域 inspector 用 `journalctl` 查 systemd journal（如 `log.tail.error_burst`）而非 `cat` 容器内日志文件
- **那么** **禁止**声明 `docker`——容器内多无 journald（空假阴性）或读到 bind-mount 的 host journal（误归因）；同域的 `log.exception_burst`（`cat {{log_path}}` 读容器文件）才允许

#### 场景:内容式 meta-guard 机械拦截误归因类声明 docker

- **当** 任一 builtin manifest 的 `collect.command` 含 host 全局读取标记（`/proc/sys/`、`/proc/meminfo`、`journalctl`、`/proc/loadavg`、`/proc/uptime`）
- **那么** 测试套件 **必须**断言该 manifest 的 `targets` **不含** `docker`（内容式 guard，覆盖人工维护的 EXCLUDE 名单之外、防未来作者据域名误加）；**禁止**仅靠人工维护的 INCLUDE/EXCLUDE 名单断言

#### 场景:docker 派发路径必须有代表性回放验证

- **当** 本提案放开一批 inspector 的 `docker` target 支持
- **那么** **必须**至少对「应用服务 / 语言运行时 / 进程级 / 网络」各类中的代表性 inspector 提供经 `ReplayTarget(impersonate="docker")` 的端到端回放测试，断言 `InspectorResult.status == "ok"`、且 snapshot 匹配；**禁止**仅靠机械追加 `targets: docker` 而无任何 docker 派发路径的测试覆盖
