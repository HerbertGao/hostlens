## 上下文

`add-os-shell-inspectors-wave1` / `add-security-baseline-and-package-inspectors`（均已归档）证明了 OS/Linux 纯 shell inspector 的批量铺量模式（纯 YAML + 录制器 fixture + snapshot），`add-single-instance-service-inspectors` 等证明了**依赖外部进程/端点**的 service inspector 模式（连接参数化 + secret remap + 失败三态）。当前 59 个 builtin inspector 里**语言运行时**域（JVM heap/GC/thread、Go goroutine/heap）仍为 0——这是 §M6 覆盖矩阵唯一仍空白且不依赖 M8 target 的域。

本提案补 JVM + Go 两个运行时域共 5 个 inspector，复用既有 manifest schema、Finding DSL、录制器、capability-gate 测试，**不引入任何新基础设施**。与 os-shell 域的本质差异：运行时 inspector **需要目标运行时进程/端点在场**（JVM 进程 / 暴露 pprof 的 Go 进程），采集要参数化目标（PID 或 pprof 端点），前提与机制更接近 service inspector，与 os-shell「零外部依赖」不变量正交——故**另立 `runtime-inspector-suite` capability**，不混进 `os-shell-inspector-suite`。

约束（《作者契约》+ schema 实测）：纯 YAML（不写 `hook.py`）；4 种 parse format 内；**参数进 shell 必经 `{{ x | sh }}`，loader 对 `string` 类型参数无条件强制此 filter（`pattern` 不豁免）**；DSL 禁 comprehension/lambda；collector 对进程/端点不可达必须 fail-loud（主命令 `|| exit 1`，禁 `|| true`）；capability enum 现为 `{shell, file_read, ssh, systemd, docker_cli}`，本提案只用 `shell`，不扩 enum。

## 目标 / 非目标

**目标**：JVM（`jvm.heap` / `jvm.gc` / `jvm.threads`，≥3）+ Go（`go.goroutines` / `go.heap`，≥2）共 5 个纯 YAML inspector，参数化目标进程/端点，含 finding-trigger + unreachable fixture，达 §M6 运行时域退出条件；新建 `runtime-inspector-suite` 套件契约（覆盖度 + 质量门 + 零新基础设施 + 进程/端点参数化注入安全）。

**非目标**：APM / 持续 profiling / 火焰图 / trace 抓取；内嵌 JMX/java-agent（只用运行时自带 CLI + 已暴露 pprof）；Python/Node/Ruby 运行时（留后续 proposal）；改 schema / capability enum / parse format；K8s 内运行时（待 M8 target）。

## 决策

### D-1：另立 `runtime-inspector-suite` capability，不塞进 `os-shell-inspector-suite`，也不塞进 `service-inspector-suite`

**选择**：新建 `specs/runtime-inspector-suite/spec.md`，全部 `新增需求`。**不** MODIFY os-shell 或 service 套件 spec。

**理由**：os-shell 套件的核心不变量是「零外部服务依赖、纯 OS/Linux shell 探针」，其 spec 已显式写「`requires_binaries` 与 command **禁止**引用语言运行时工具（`jstat` / `jcmd` / pprof）」——运行时 inspector 天然违反该不变量，塞进去会污染该套件。service 套件的不变量是「单实例服务 + 连接 secret remap」，运行时 inspector 既非「服务」也通常无连接 secret（jstat 读本机、pprof 是本机内部端点），塞进去同样错配。运行时域有**自己的正交前提**（「目标运行时进程/端点在场」），值得独立 capability 承接，与既有两套件并列、互不回溯修改（沿用追加式冻结纪律）。

**代价**：多一个 capability 文件。换来三套件各自不变量清晰、后续运行时 wave（Python/Node）可向本套件 ADDED 追加而不触碰 os-shell/service。

### D-2：JVM 进程定位用 `pid`（整数校验）或 `process_pattern`（经 `pgrep` 解析）；Go 用 `pprof_endpoint`（`pattern` 收紧为 host:port）——参数注入安全

**选择**：
- JVM inspector 主参数 `pid`（`type: integer`，`minimum: 0`、`default: 0`——`0` 作 **unset 哨兵**：`pid<=0` 时 collector 回退 `process_pattern`/`pgrep`，整数天然无 shell 注入面）。可选 `process_pattern`（`type: string`，`pattern: "^[A-Za-z0-9._/-]*$"`、`default: ""`，经 `{{ process_pattern | sh }}` 引用后喂 `pgrep`），collector 内 `pgrep` 解析出 pid；二者择一，pid 优先（pid>0 时直接用，否则用 pattern）。**两参数都带 default 是必需的**：mutual-exclusion 下若 `pid` 取 `minimum: 1` 且无 default，则用 `process_pattern` 路径时 `{{ pid }}` 在 Jinja StrictUndefined 下 render 报错；故用 `0` 哨兵 + `*`（允许空）pattern（空 `process_pattern` default 被 collector 的 `[ -n "$pat" ] || exit 1` 守住）。
- Go inspector 主参数 `pprof_endpoint`（`type: string`，`pattern: "^[A-Za-z0-9._-]+:[0-9]{1,5}$"` 收紧为 `host:port`，经 `{{ pprof_endpoint | sh }}` 拼进 `curl` URL）。

**理由**：`pid` 是整数，不进 shell 注入面（schema `integer` + `minimum`）。`process_pattern` / `pprof_endpoint` 是 string，必经 `| sh` 引用 + `pattern` 收紧取值域（双重防护：loader 强制 `| sh`，pattern 限字符集）。pprof 端点收紧为 `host:port` 形而非任意 URL，杜绝 `;`/空格/`$()` 等注入字符。**禁止**裸 `{{ pid }}` 之外的 string 参数不过 `| sh`。

### D-3：fixture 沿用 D-7 录制约定（命令串真捕获 + 场景 stdout 作者编/录制），collector 内解析逻辑只命令串级锁、不在 offline 行为级执行

**选择**：用既有录制器约定（`_CaptureTarget` 模式，见 `tests/inspectors/_record_security_pkg.py` / `_record_os_*.py`）为 5 个 inspector 录 ReplayTarget fixture：写 `tests/inspectors/_record_runtime.py` 驱动**真** `InspectorRunner`，**字节级捕获真实渲染的 collector 命令串**（防 fixture-runner 漂移），**每场景的 stdout/exit_code 代表该状态下 collector 会吐的最终输出**。

**关键澄清（与 proposal「录真实 JVM/pprof」的措辞对齐）**：ReplayTarget 在回放时对整条 `collect.command` 返回录制的**最终 stdout**（如 `{"goroutines":50000}`），collector 内的 `jstat` 调用 / `awk` 抽取 / `curl` 管道 / `pgrep` 解析在 offline **不执行**。因此：
- **可在 dev 机（含 macOS）对真实 JVM/真实 pprof 端点录制最终 stdout**（JVM/Go 跨平台，比 Linux-only os-shell 更易录真；**注**：macOS 录 JVM 需确认已装含 `jstat`/`jcmd` 的 JDK，非系统默认；Go pprof 无平台依赖），但录到的仍是「命令串 → 最终 JSON」对，**collector 解析逻辑的正确性由命令串级锁 + 真机 Demo Path 验证，不由 offline snapshot 行为级验证**（与 security/pkg 的 D-7 偏离登记同构）。
- jstat 列名锚定（D-4）、awk 抽取、pgrep→pid、curl fail-loud 的执行正确性 → **命令串捕获 + Demo Path**；下游「计数过阈值 → 检出 finding」由通用 finding-trigger fixture 证。

**理由**：项目对此类 collector-shell-in-manifest 已有稳定 D-7 约定，运行时 inspector 与 service/os-shell 同属此类——ReplayTarget 不跑真 shell，强行声称 offline fixture 锁住 jstat 解析正确性是过度声明。tasks.md 含统一偏离登记，manifest/test 注释不夸大 offline 锁定范围。

### D-4：jstat/jcmd 输出列随 JDK 版本漂移 → `table` parse 锚**列名**而非列位；collector 在目标机内把派生量算成最终标量

**选择**：`jvm.heap` / `jvm.gc` / `jvm.threads` 的 collector **不**直接把 `jstat` 表格喂 `parse.format: table` 让 finding 现算占比，而是 collector 内用 `awk` 按**表头列名**定位字段（`jstat -gc` 表头含 `EU`/`OU`/`MU` 等命名列）、在目标机内算出 heap **used/committed 占比标量**（committed 为 live ceiling——jstat -gc 无 -Xmx max 列，故口径为「占已提交容量比」非「占 -Xmx 比」，vs -Xmx 留后续；awk 在锚定的列缺失时 fail-loud 输出空→`[ -n ]` 门→exception，不吐 field-0 垃圾标量）、写进输出 JSON；finding 规则只对已就绪标量做阈值比较。

**理由**：`jstat -gc` 的列集与列序随 JDK 版本（8/11/17/21）漂移，按列**位**取值会在跨版本时静默错位 → 假值。按**列名**锚定（awk 先扫表头建列名→列号映射）跨版本稳定。数值派生（占比、Full GC 次数、GC 耗时占比）一律在 collector 内算成标量（遵守《作者契约》「数值派生在 collector 内、finding 只做标量比较」），不在 finding DSL 现算。Failure Mode 4 由「多 JDK 版本样本」命令串级回归覆盖（真机 Demo Path 验证跨版本计数边界）。

### D-5：`jvm.gc` 用 `sampling_window` 双采差求时窗内 GC 增量，采样时坍缩成标量、回放确定

**选择**：`jvm.gc` 声明 `collect.sampling_window.duration_seconds`，collector 在**采样时刻**于目标机内做 read→sleep→read 双采 `jstat -gcutil`，算出**时窗内** Full GC 次数增量 + GC 耗时占比，坍缩成**最终标量**冻结进输出 JSON；`ReplayTarget` 回放原样返回该冻结标量。

**sleep 时长必须取注入的 `{{ window_seconds }}`，不得硬编码**：`sampling_window` 是注入 schema 字段，runner 仅注入三个保留变量 `window_start` / `window_end`（UTC 墙钟串，journalctl 取向，对 jstat **无用**）+ `window_seconds`（int）——它**不**替 collector 做双采。jvm.gc 的 sleep **必须**用 `sleep {{ window_seconds }}`（`window_seconds` 是 runner 注入保留变量、非声明 parameter，故裸用不过 `| sh`），使 `duration_seconds` 真正驱动采样窗口；**禁止**像 `disk_io.yaml` 那样硬编码 `interval=1` 而把 `duration_seconds` 架空成死字段。`window_start` / `window_end` 会一并注入 DSL 上下文但 jvm.gc 不引用，属无害冗余（接受，换取沿用既有 schema 字段、零新 infra）。

**理由**：`sampling_window` 是现有 schema 字段（`log.tail.error_burst` 已在用），零新基础设施。GC 压力本质是**速率/增量**信号（瞬时 GC 计数无意义，需时窗差），故须双采。**确定性约束**（沿用 service wave-2b「窗口聚合采样时坍缩成标量」）：窗口增量**必须**在采样时算成最终标量并冻结，**禁止** collector 回吐需在回放时按 `now()` 重聚合的原始带时间戳明细（否则回放非确定，违反「离线回放确定性出结果」）。

### D-6：进程/端点不在场必须 fail-loud（`status=exception`）非假 ok；binary 缺失走 `requires_unmet`

**选择**：
- **POSIX/dash-safe 管道纪律（D-6.0，全 collector 强制）**：collector 经 `create_subprocess_shell` 跑 **`/bin/sh -c`**（Debian/Ubuntu 上即 **dash**）。**禁止 `set -o pipefail`**——它非 POSIX，dash 作为特殊内建遇非法 `set -o` 选项**整脚本 abort、exit 2、空 stdout**，会让**健康目标**也永久误判 `status=exception`（且 offline fixture 与 macOS Demo Path 均 `/bin/sh`→bash 检不出，最隐蔽）。沿用既有 builtin 约定（见 `process_total.yaml` / `containers_restart_loop.yaml` 注释）：把主命令（`jstat`/`jcmd`/`curl`）**raw-capture 进变量并在该命令上判退出码**（`raw=$(主命令 ...) || exit 1`，管道前），再单独把 `$raw` 喂 `awk`——使主命令失败不被末段 awk（恒 `exit 0`）吞掉。
- **进程不存在 / pid 错**：`raw=$(jstat <pid> ...) || exit 1` → `status=exception`。`process_pattern` 经 `pgrep` 解析**空结果**（无匹配进程）同样 fail-loud `exit 1`，**禁止**当作「0 个进程 → ok」。
- **pprof 端点不通 / 未暴露**：`raw=$(curl -fsS --max-time 5 ...) || exit 1`（连接拒绝/4xx/5xx 非零退出）→ `status=exception`。**禁止** `|| true` 把「端点不通」吞成空计数。
- **无 JDK（jstat/jcmd 缺失）/ 无 curl**：preflight `requires_binaries` 拦 → `status=requires_unmet` skip（非崩溃、非假 ok）。
- **不能只信工具退出码 —— 必须额外校验抽取标量非空（D-6.1）**：部分 **JDK 8** 的 `jstat` 在读不到目标 `/tmp/hsperfdata_<user>/<pid>`（跨用户 attach EACCES / 进程刚退）时**打错误到 stderr 却 `exit 0`**，使退出码判定不触发；若 awk 又从错误流凑出个数字，会得 `status=ok` + 假堆值（最隐蔽的假阴）。故所有 JVM/Go collector **必须**在抽取后**显式校验派生标量非空再 printf**（`[ -n "$used" ] || exit 1` 二道门，与 `go.goroutines` 的 `[ -n "$total" ] || exit 1` 同构），**不得**仅靠退出码判成败。

**理由**：运行时 inspector 的核心假阴面是「目标进程/端点不在场被误判为健康」——「读不到 JVM 堆」绝不能伪造成「堆健康」。fail-loud（exception）与 binary-missing（requires_unmet）二态明确区分「目标不在场」与「工具不在场」，二者都**不**产生假 ok。JDK 8 `jstat` exit-0 边角使「只判退出码」不足以兜底，故加 D-6.1 非空二道门。每个 inspector 强制录 unreachable fixture 锁此行为。

### D-7：`privilege: none`，不自动提权；跨用户 JVM attach 失败按 exception 呈现并文档式声明同用户前提

**选择**：所有 5 个 inspector `privilege: none`。jstat/jcmd attach 仅能附到**与采集者同 UID**（或 root）拥有的 JVM——跨用户 JVM attach 被拒 → collector 非零退出 → `status=exception`。「采集者须与目标 JVM 同用户」作为运行前提在 `description` **文档式声明**，**不**新增 schema 字段、**不**在 inspector 内自动 `sudo`（沿用全局「写操作拒绝 root / 不自动提权」纪律；本套件纯只读但仍不替用户做提权决定）。

**理由**：自动 `sudo` attach 会制造 root-owned 临时文件并越权，违反项目纪律。同用户前提是 JVM attach API 的固有约束，文档式声明 + attach 失败 fail-loud（exception，非假 ok）是诚实呈现，把提权决定留给用户/部署环境。

### D-8：每个 inspector 至少一份 finding-trigger + 一份 unreachable fixture

**选择**：除 happy-path 外强制录：①**finding-trigger**（`go.goroutines` 给 total=50000 + threshold=10000 → 过阈值检出；`jvm.heap` 给 heap 占比 >95% → 检出；`jvm.gc` 给时窗内高 Full GC → 检出）；②**unreachable**（JVM: pid 不存在 → `status=exception`；Go: pprof 端口不通 → `status=exception`；binary 缺失另由 capability-gate 测试覆盖 `requires_unmet`）。两类都进 snapshot，由套件 spec「finding-trigger 证检出」与「不在场 fail-loud 非假阴」场景强制。

**理由**：只录 happy-path 的 inspector 是 vacuous——证明不了 DSL 阈值比较生效、也证明不了「目标不在场」假阴防护。这是套件质量门核心。

## 风险 / 权衡

- **[jstat 列集/列序随 JDK 8/11/17/21 漂移]** → 缓解：D-4 按**列名**（awk 扫表头）锚定而非列位；多 JDK 版本样本命令串级回归 + 真机 Demo Path 验证跨版本计数边界。残余：极冷门 JVM 实现（GraalVM native-image 无 jstat）不支持 → `requires_unmet`/`exception`，不假装成功。
- **[跨用户 JVM attach 被拒被误当无堆数据]** → 由 D-6/D-7 + spec「不在场 fail-loud 非假阴」场景 + unreachable fixture 锁死（attach 失败 → exception 非假 ok）；同用户前提文档式声明。
- **[pprof 端点格式/字段随 Go 版本漂移]** → 缓解：`go.goroutines` 只取 `debug=1` profile 首行 `goroutine profile: total N`（`net/http/pprof` 实现细节、15+ 年事实稳定但**非**正式版本化 API，按 **best-effort 稳定**对待、录 fixture 时核对）；`go.heap` 抽稳定字段。残余：未暴露 net/http/pprof 的 Go 服务 → 端点不通 → `exception`（非目标：不要求目标加 pprof）。
- **[collector 内 jstat/awk/curl 解析逻辑在 D-7 offline 不执行]** → 由命令串级锁（字节级捕获含正确列名/字段/fail-loud）+ 真机 Demo Path 验证；offline fixture **不**声称锁解析执行正确性（tasks.md 偏离登记，manifest/test 注释下调措辞）。
- **[`jvm.gc` 双采 sampling_window 拖慢单 run / timeout 早触发]** → 缓解：`duration_seconds` 取小默认（如 5s）+ `timeout_seconds` **必须** > `duration_seconds` + 余量（建议 ≥10s，留 jstat 两次调用余量）——否则 sleep 窗口本身先触发 timeout 把正常采样误判为超时；窗口聚合采样时坍缩成标量（D-5）保证回放确定。

## 迁移计划

- 纯增量：新增 5 个 manifest（`builtin/jvm/` ×3 + `builtin/go/` ×2）+ fixture + snapshot 测试 + 新 `runtime-inspector-suite` spec + 冻结 cohort 计数 guard + capability-gate 断言扩容。无 schema 迁移、无破坏性契约。
- 回滚：删 `builtin/jvm/` / `builtin/go/` 及对应测试与新 spec 即可；无持久化状态。
- 部署：随包发布，`hostlens inspectors list` 自动出现 5 个新 inspector；目标缺 jstat/curl 时 `requires_unmet`、进程/端点不在场时 `exception` 自动呈现。

## 待解决问题

- **`go.heap` 输出字段选型**：pprof `/debug/pprof/heap` 的文本/protobuf 格式中取 `HeapInuse` / `HeapAlloc` 哪几个字段作 finding 信号，录 fixture 时核对跨 Go 版本稳定性；不稳定则降为 best-effort evidence、finding 只锁 goroutine 计数类稳定信号。
- **`jvm.threads` BLOCKED 计数判定**：`jcmd Thread.print` 输出格式跨 JDK 是否稳定到可计 BLOCKED 线程数，录 fixture 时验证；不稳定则 finding 只锁「线程总数过阈值」稳定信号、BLOCKED 计数作附加 evidence。
- **多 JDK 版本样本覆盖广度**：Demo Path 至少跑一个真实 JDK 版本验证列名锚定；是否补齐 8/11/17/21 全版本样本留 Demo Path 期间按手头 JDK 决定，不阻塞 offline 验收（命令串级锁已覆盖列名正确性）。
