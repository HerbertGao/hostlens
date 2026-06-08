## 1. JVM 运行时域（builtin/jvm/）

- [x] 1.1 `builtin/jvm/heap.yaml`（`jvm.heap`）：`pid`（`type: integer, minimum: 0, default: 0`——0 作 unset 哨兵）或 `process_pattern`（`type: string`，`pattern: "^[A-Za-z0-9._/-]*$"` 收紧 + `default: ""` + `{{ process_pattern | sh }}` → `pgrep` 解析，D-2）定位 JVM；collector **raw-capture** `raw=$(jstat -gc <pid>) || exit 1`（管道前判退出码，D-6.0，**禁 `set -o pipefail`**——dash 整脚本 abort）→ `printf '%s' "$raw" | awk` **按列名锚定**（D-4，非列位）算 heap used/committed/max **占比标量**写进输出 JSON；finding 只对已就绪占比标量做阈值比较。**fail-loud（双道门）**：pid 不存在 → `jstat` 非零退出 → `|| exit 1` → `status=exception`；`pgrep` 空结果 → `exit 1`（D-6，**禁** `|| true`、**禁**「0 进程→ok」）；**且** 抽取后 `[ -n "$used" ] || exit 1` 二道门兜 JDK 8 `jstat` exit-0 假阴（D-6.1，不只信退出码）。`requires_capabilities: [shell]`、`requires_binaries` **只列 command 实际调的工具**（`jstat -gc` → `[jstat]`，**不**同时列 jcmd，合取门，task 4.2）、`privilege: none`；「同用户 attach 前提」文档式声明于 description（D-7）。验收：snapshot 含 finding-trigger（heap 占比逼近上限 → 检出）+ unreachable（pid 不存在 → exception 非假阴）+ **列名锚定与非空二道门命令串锁**（捕获命令含按表头列名抽取 + `[ -n ... ] || exit 1`；**D-3/D-7：jstat 解析在 offline ReplayTarget 不执行——此处只锁命令串、非行为级**，跨 JDK 计数边界真机 Demo Path 验证）

- [x] 1.2 `builtin/jvm/gc.yaml`（`jvm.gc`）：同 1.1 的 pid/process_pattern 参数化；collector 声明 `collect.sampling_window.duration_seconds`，**采样时刻**于目标机内双采 `jstat -gcutil <pid>`（read→**`sleep {{ window_seconds }}`**→read，**sleep 时长必须取注入保留变量 `window_seconds`、不得硬编码 `interval=1`**——否则 `duration_seconds` 沦为死字段，D-5；`window_seconds` 是 runner 注入保留变量非声明 parameter，裸用不过 `| sh`）算**时窗内 Full GC 次数增量 + GC 耗时占比**、坍缩成**最终标量**冻结进输出 JSON（D-5：窗口聚合采样时坍缩、`ReplayTarget` 回放原样返冻结标量，**禁**回吐需 `now()` 重聚合的带时间戳明细）；finding 对 Full GC 增量/耗时占比标量做阈值比较。fail-loud（含非空二道门）同 1.1。`timeout_seconds` ≤10s 兜底（D-5）。验收：snapshot 含 finding-trigger（时窗内高 Full GC → 检出）+ unreachable（pid 不存在 → exception）+ 确定性回放（双采差已冻结为标量）

- [x] 1.3 `builtin/jvm/threads.yaml`（`jvm.threads`）：同参数化；collector `jcmd <pid> Thread.print` / `jstack <pid>` → 线程总数 + BLOCKED 计数（在 collector 内计数成标量）；finding **锁「线程总数过阈值」稳定信号**，BLOCKED 计数作附加 evidence（跨 JDK 稳定性录 fixture 时确认，不稳定则降 best-effort，design 待解决问题）。fail-loud（含非空二道门）同 1.1。验收：snapshot 含 finding-trigger（线程总数突增过阈值 → 检出）+ unreachable（pid 不存在 → exception）+ **计数解析命令串锁（offline 不执行 jcmd 解析、只锁命令串，行为级验证留真机 Demo Path，D-7）**

## 2. Go 运行时域（builtin/go/）

- [x] 2.1 `builtin/go/goroutines.yaml`（`go.goroutines`）：`pprof_endpoint`（`type: string`，`pattern: "^[A-Za-z0-9._-]+:[0-9]{1,5}$"` 收紧为 host:port，`{{ pprof_endpoint | sh }}` 拼进 URL，D-2）；collector `curl -fsS --max-time 5 "http://{{ pprof_endpoint | sh }}/debug/pprof/goroutine?debug=1"`（string 参数必经 `| sh`，**parameters 须 `type:object`/`properties:` wrapper 否则注入门失效**）→ awk 取首行 `goroutine profile: total N`（`net/http/pprof` 实现细节、best-effort 稳定、录 fixture 核对）→ `{"goroutines":N}`；finding `goroutines > threshold`（`threshold` 参数 `type: integer`）。**fail-loud（POSIX/dash-safe raw-capture + 二道门）**：`raw=$(curl -fsS ...) || exit 1`（管道前在 curl 上判退出码）→ `total=$(printf '%s\n' "$raw" | awk ...)` → 末行 `[ -n "$total" ] || exit 1` 二道门 → `status=exception`（**禁** `|| true`；**禁 `set -o pipefail`**——非 POSIX，dash 整脚本 abort exit 2 致健康目标永久假 exception，沿用既有 builtin raw-capture 约定，D-6）。`requires_binaries: [curl]`、`privilege: none`。验收：snapshot 含 finding-trigger（total=50000 + threshold=10000 → 检出泄漏）+ unreachable（端口不通 → exception 非假阴）+ 只取首行 total 不下整个 profile（Operational Limits）

- [x] 2.2 `builtin/go/heap.yaml`（`go.heap`）：同 `pprof_endpoint` 参数化；collector `curl -fsS` 取 `/debug/pprof/heap` → 抽 **HeapInuse / HeapAlloc 等稳定字段**（字段选型录 fixture 时核对跨 Go 版本稳定性，不稳定则降 best-effort evidence、finding 只锁稳定信号，design 待解决问题）算成标量写输出 JSON；finding 对已就绪标量做阈值比较。fail-loud（POSIX/dash-safe raw-capture：`raw=$(curl ...) || exit 1` 再喂 awk + 抽取值非空二道门，**禁 `set -o pipefail`**）同 2.1。验收：snapshot 含 finding-trigger（heap inuse 过阈值 → 检出）+ unreachable（端口不通 → exception）

## 3. Fixture 录制与 snapshot 测试

- [x] 3.1 写 `tests/inspectors/_record_runtime.py` 驱动**真** `InspectorRunner` 为 5 个 inspector 录 ReplayTarget fixture，落 `tests/inspectors/fixtures/jvm/`、`fixtures/go/`。**沿用既有 D-7 录制约定**（`_CaptureTarget` 模式，见 `tests/inspectors/_record_security_pkg.py`）：**字节级捕获真实渲染的 collector 命令串**（防 fixture-runner 漂移，Failure Mode 5），**每场景的 stdout/exit_code 代表该状态下 collector 会吐的最终输出**（JVM/Go 跨平台可在 dev 机对真实 JVM/真实 pprof 录最终 stdout，但录到的仍是「命令串→最终 JSON」对）。`_CaptureTarget` 复用 security/pkg 已扩展的 **exit_code≠0 + 空 stdout** 场景支持（unreachable：collector fail-loud 后 runner 见非零退出 + 空 stdout → JSONDecodeError → `status=exception`）。
  > **review 偏离登记（D-7 架构下「collector shell 逻辑只命令串级锁、不行为级执行」）**：ReplayTarget 回放时对整条 `collect.command` 返回录制的最终 stdout，collector 内的 `jstat`/`jcmd`/`awk`/`curl`/`pgrep` **不执行**。以下 collector-shell 正确性点因此只由命令串捕获 + 真机 Demo Path 锁定、不由 offline snapshot 行为级验证：
  > - **D-4 jstat 列名锚定**：awk 扫表头建列名→列号映射不跑；命令串确含按列名抽取（有断言），跨 JDK 计数边界真机验证。
  > - **D-5 jvm.gc 双采差**：read→sleep→read 不跑；窗口增量已在录制时坍缩为冻结标量，回放原样返回。
  > - **D-2 pgrep→pid / curl fail-loud**：pgrep 解析、curl 退出码判定不跑；命令串确含 fail-loud 守卫（`|| exit 1` 非 `|| true`），下游检出由 finding-trigger fixture 证。
  > 行为级真验证留**真机 Demo Path**（带真实 JDK + 真实 Go pprof 端点的 host 跑一次确认计数边界与跨版本列名锚定）。manifest/test 注释不夸大 offline fixture 的锁定范围。

- [x] 3.2 每个 inspector 写 snapshot 测试（`tests/inspectors/`），覆盖 happy-path + **finding-trigger** + **目标不在场（pid 不存在 / pprof 端口不通）→ exception**（D-8）；离线 replay，非 root 跑通

- [x] 3.3 套件 spec 假阴场景验收：JVM inspector 在 pid 不存在 fixture 下断言 `status=exception`、**非** `status=ok`；Go inspector 在端点不通 fixture 下断言 `status=exception`、**非** `status=ok` 计数 0（运行时域核心假阴防护）

## 4. 套件契约与注册

- [x] 4.1 扩 `tests/inspectors/test_builtin_inspectors.py`：5 个新 inspector 全部 loader 加载、`build_registry_from_search_paths([], ...)` 的 `errors == []`、`name` 出现在 registry；**新增冻结 cohort 计数断言 `len(cohort) == 5`**，**用专属非碰撞符号名**（如 `_RUNTIME_INSPECTORS` / `test_runtime_count_is_frozen_at_5`），**禁止复用** os-shell wave-2（==6）/ service `_WAVE2A`（==6）等既有 cohort 符号名（同模块复用名会符号覆盖、静默吃掉其它 guard）；注明本 cohort 与 os-shell/service cohort 是不同 suite、独立 dict、不互触计数

- [x] 4.2 扩 `tests/inspectors/test_builtin_capability_gate.py`：5 个新 inspector 的 capability/binary 断言（JVM/Go 均 `privilege: none`、`requires_capabilities: [shell]`；Go 含 `curl`，均真实工具非 sh）。**`requires_binaries` 是合取门（列出的全须在场否则 `requires_unmet`）**：每个 JVM manifest **只列其 command 实际调用的工具**（`jvm.heap` 用 `jstat -gc` → `[jstat]`；用 `jcmd` 的 → `[jcmd]`），**禁止**同时列 `jstat`+`jcmd`（会让只装其一的 JRE-trimmed 机误判 `requires_unmet`）；断言据此逐 manifest 校验

- [x] 4.3 确认运行时 inspector 是 **runtime 域非 os-shell/service 域**、**不**纳入 os-shell wave-2 或 service crosscheck 的冻结计数（各 cohort 独立 dict，不互触）；spec「参数化目标 + 注入安全」场景断言：string 目标参数（`process_pattern`/`pprof_endpoint`）经 `| sh` 引用 + `pattern` 收紧、`pprof_endpoint` pattern 为 host:port 形、JVM 有 `pid`(int) 或 `process_pattern`(str) 参数

## 5. 文档与收尾

- [x] 5.1 勾选 `TODO.md` §M6 覆盖矩阵的运行时（JVM）行（jvm.heap / jvm.gc / jvm.threads）+ 运行时（Go）行（go.goroutines / go.heap）单元格、勾上 6.9；更新 M6 状态段「剩余域」移除 JVM / Go 运行时

- [x] 5.2 跑全量验收：`pytest tests/inspectors/ -k "jvm or go" -v` 全绿、`pytest` 全量回归不破坏既有、`hostlens inspectors list | grep -E "jvm\.|go\."` 列出 5 个、冻结计数 == 5；Demo Path 真机复现**必须在 `/bin/sh`→dash 的 host（Debian/Ubuntu）上跑**（验证 collector 不依赖 `set -o pipefail`、健康目标返 `status=ok` 非假 exception、计数边界与跨 JDK 列名锚定）——**禁止**只在 macOS（`/bin/sh`→bash）验，否则 dash 不兼容会被掩盖
