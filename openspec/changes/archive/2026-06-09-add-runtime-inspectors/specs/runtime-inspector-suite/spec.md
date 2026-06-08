## 新增需求

### 需求:runtime-inspector-suite 必须按运行时域覆盖 JVM 与 Go

`runtime-inspector-suite` **必须**作为**语言运行时** inspector 套件的覆盖契约与质量门，在现有 builtin 基线之上、按 `TODO.md` §M6 覆盖矩阵新增覆盖**此前空白（0 inspector）的语言运行时域**：JVM 与 Go。JVM 域**必须**至少新增 3 个 inspector、Go 域**必须**至少新增 2 个 inspector。**遵守 spike D-9**：本需求约束的是**套件层的域覆盖度**，**不**为任一具体 inspector 规定 input/output 行为契约——具体 inspector 清单（名称与采集手法）是**实现**，列在本变更的 `proposal.md` 与 `tasks.md`，由 snapshot 测试验收、冻结于本变更归档时。

**另立 capability 而非塞进 `os-shell-inspector-suite` 或 `service-inspector-suite`**：运行时 inspector 需「目标运行时进程/端点在场」的前提，与 os-shell 套件「零外部服务依赖」不变量正交（os-shell spec 已显式禁止引用 `jstat`/`jcmd`/pprof），亦与 service 套件「单实例服务 + 连接 secret remap」错配（运行时探测通常无连接 secret）。本套件以独立 capability 承接该正交前提，与既有两套件并列、**禁止** MODIFY 任一既有套件的冻结需求。后续运行时 wave（Python/Node/Ruby 等）**必须**以 `新增需求`（ADDED）向本套件追加自己的 sibling 覆盖需求、**禁止** MODIFY 本变更归档的 JVM/Go 冻结覆盖需求。

#### 场景:清单中的 inspector 全部干净注册

- **当** 套件实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** 本变更 `proposal.md`/`tasks.md` 列出的每个运行时 inspector（共 5 个，以本变更**归档时冻结**的清单为准；后续 wave 另立 change，不回溯改本 spec）**必须**以其声明 `name` 出现在 registry 中，且 registry `errors == []`

#### 场景:JVM 与 Go 两域都达最低覆盖且勾上矩阵

- **当** 评估本套件是否达成域覆盖
- **那么** JVM 域**必须**至少新增 3 个 inspector、Go 域**必须**至少新增 2 个 inspector，且每个落地的 inspector **必须**勾上 `TODO.md` §M6 覆盖矩阵对应单元格；**禁止**以「域内已有探针」为由跳过矩阵为该域列出的新增项

#### 场景:不回溯修改既有套件且后续 wave 以 ADDED 追加

- **当** 评估本套件与 os-shell / service 套件的关系，或后续运行时 wave 要纳入本套件
- **那么** 本变更**禁止** MODIFY `os-shell-inspector-suite` 或 `service-inspector-suite` 的任一冻结需求；后续运行时 wave **必须**以 `新增需求` 追加仅约束自己 cohort 的覆盖需求（其清单由该 change 的 proposal/tasks + snapshot 验收）、**禁止** MODIFY 本变更归档的 JVM/Go 冻结覆盖需求

### 需求:套件内每个 inspector 必须是遵守作者契约的纯 YAML

本套件内每个 inspector **必须**为纯 YAML manifest，并**遵守** `inspector-authoring-contract` 的全部规则（一切抽取与数值派生在 collector 内、finding 规则只做标量阈值/成员比较、`for_each` 单绑定、输出键防 parameter 遮蔽、命令注入安全三件套、运行前提文档式声明）。本需求**引用**该契约而非重述其细则，以免两份 spec 漂移。**禁止** enable `hook.py`、**禁止**新增 parse format、**禁止**在 finding 表达式里做解析或数值派生。

裸聚合键允许（纯标量聚合输出用不与 parameter 同名的裸标量键、而非字面要求的 `results`/`items`/`records`）是 `os-shell-inspector-suite` 已归档 spec 既定且被接受的解读，本套件**逐字沿用**；含 array 顶层字段的列表型输出，其 array 顶层键**必须**取自 `results`/`items`/`records` 之一且在 collector 内截断为 top-N + total 计数。两种形态的顶层键都**禁止**与任一已声明 parameter 同名。

#### 场景:数值派生在 collector 内、finding 只做标量比较

- **当** 某套件 inspector 需要派生量（如 heap used/committed 占比、时窗内 Full GC 次数、GC 耗时占比、goroutine 计数过阈值判断）
- **那么** 该派生**必须**由 collector 命令（含 `jstat` 双采差 / `awk` 按列名抽取 / `curl` 取 pprof 首行 total）算出并写入输出 JSON，finding 规则只对已就绪标量做阈值/成员比较；**禁止**在 finding 表达式内现算

#### 场景:jstat 输出按列名锚定而非列位抽取

- **当** JVM inspector 解析 `jstat -gc` / `jstat -gcutil` 表格输出（其列集与列序随 JDK 版本漂移）
- **那么** collector **必须**先扫表头建立列名→列号映射、按**列名**取值，**禁止**按固定列位取值（跨 JDK 版本会静默错位 → 假值）

### 需求:套件内每个 inspector 必须参数化目标运行时进程或端点且参数注入安全

本套件内每个 inspector **必须**通过参数指定目标运行时——JVM inspector **必须**经 `pid`（`type: integer`，`minimum: 0`、`default: 0`，其中 `0` 是「未指定」哨兵——`pid<=0` 回退 `process_pattern`）或 `process_pattern`（`type: string`，经 `pgrep` 解析）定位目标 JVM 进程；Go inspector **必须**经 `pprof_endpoint`（`type: string`）定位目标 pprof 端点。**禁止**硬编码单一进程/端点而无参数化（运行时探测的前提是「目标进程/端点在场」，与 os-shell「零目标依赖」正交）。若 JVM inspector 同时接受 `pid` 与 `process_pattern` 且二者同时提供，collector **必须**以 `pid` 为准（确定性优先级），**禁止**行为依赖二者孰先的隐式约定。

**`parameters` 必须是完整 JSON Schema**（`type: object` + `properties:` + `additionalProperties: false`）：loader 仅从 `parameters.properties` 建参数表，缺 wrapper 会使参数对 loader 不可见、`| sh` 注入门与 `pattern` 约束**双双静默失效**。一切插入 `collect.command` 可执行位置的 **string** 参数（`process_pattern` / `pprof_endpoint`）**必须**声明在 `properties` 下、经 `{{ x | sh }}` 引用、且用 `pattern` 收紧取值域（`process_pattern` 限进程名字符集；`pprof_endpoint` 收紧为 `host:port` 形 `^[A-Za-z0-9._-]+:[0-9]{1,5}$`，杜绝 `;`/空格/`$()` 注入字符）；**禁止**裸 `{{ param }}` 拼进可执行位置。`pid` 为整数参数，由 `type: integer` + `minimum` 约束、不进 shell 注入面。

#### 场景:string 目标参数经 sh 引用且 pattern 收紧

- **当** 某套件 inspector 把 string 目标参数（`process_pattern` / `pprof_endpoint`）插入 `collect.command`
- **那么** 其 `parameters` **必须**是完整 JSON Schema（`type: object` + `properties:` + `additionalProperties: false`、参数声明在 `properties` 下），该参数**必须**经 `{{ x | sh }}` 引用、且用 `pattern` 收紧取值域；**禁止**裸插值进可执行位置；`pprof_endpoint` 的 `pattern` **必须**收紧为 `host:port` 形
- **且** **禁止**省略 `type: object`/`properties:` wrapper（缺 wrapper → loader 看不到该参数 → `| sh` 门与 `pattern` 双双静默失效，等于把已收紧的注入面重新敞开）

#### 场景:JVM inspector 经 pid 或 process_pattern 参数化目标且优先级确定

- **当** 检视某 JVM 套件 inspector 的 `parameters`
- **那么** 它**必须**声明 `pid`（`type: integer`）或 `process_pattern`（`type: string`）以参数化目标 JVM；**禁止**硬编码单一 pid 而无参数化
- **且** 若二者同时声明并在调用时同时提供，collector **必须**以 `pid` 为准、行为确定；snapshot 测试**必须**断言此优先级（**禁止**留「孰先」的隐式约定）

### 需求:套件内每个 inspector 必须在目标进程或端点不在场时 fail-loud 而非伪造健康

本套件内每个 inspector **必须**在**目标运行时进程/端点不在场或采集失败**时以 `status=exception`（collector fail-loud `|| exit 1`）呈现、**禁止**伪造 `status=ok`（运行时域的关键假阴性防护：「读不到 JVM 堆 / 连不上 pprof」绝不能误判为「堆健康 / 服务健康」）。具体：JVM `jstat`/`jcmd` 对不存在的 pid 非零退出 → `exception`；`process_pattern` 经 `pgrep` 解析**空结果** → fail-loud `exit 1`（**禁止**当作「0 进程 → ok」）；Go `curl -fsS` 对端点不通/4xx/5xx 非零退出 → `exception`。collector **禁止**用 `|| true` 掩盖主命令失败。**curl 经管道喂 awk/grep 时**，collector **必须**先对 curl 输出 **raw-capture 并在 curl 上判退出码**（`raw=$(curl ...) || exit 1`，管道前），再单独把 `$raw` 喂 awk——否则 `total=$(curl|awk)` 的退出码只反映末段 awk（恒 `exit 0`）→ curl 失败被吞 → 假计数；**禁止**用 `set -o pipefail`（非 POSIX，dash=`/bin/sh` on Debian/Ubuntu 会整脚本 abort exit 2 → 健康目标也永久假 `exception`；本套件沿用既有 builtin 的 raw-capture 约定）；并**必须**辅以抽取值非空二道门（`[ -n "$total" ] || exit 1`）。

**`set -o pipefail` 禁令对全套件 collector 适用**（非仅 Go）：collector 经 `create_subprocess_shell` 跑 `/bin/sh -c`（Debian/Ubuntu 即 dash），**任一** collector 用 `set -o pipefail` 都会在 dash 上整脚本 abort exit 2 → 健康目标永久假 `exception`；故 JVM 的 `jstat`/`jcmd` 同样**必须**走 raw-capture-before-pipe（`raw=$(jstat ...) || exit 1` 再喂 awk），**禁止** `set -o pipefail`。

**JVM collector 禁止仅信工具退出码——必须额外校验抽取标量非空**：部分 **JDK 8** 的 `jstat` 在读不到目标 `hsperfdata`（跨用户 attach EACCES / 进程刚退）时打错误到 stderr 却 `exit 0`，使退出码判定不触发、若 awk 又凑出数字则得假 `ok` + 假堆值。故 `jvm.heap` / `jvm.gc` / `jvm.threads` collector **必须**在抽取后**显式校验派生标量非空再 printf**（`[ -n "$used" ] || exit 1` 式二道门，与 `go.goroutines` 同构），**不得**仅靠退出码判成败。

工具不在场（无 JDK 即无 `jstat`/`jcmd`、无 `curl`）与目标不在场是**两种**状态：前者由 preflight `requires_binaries` 拦为 `status=requires_unmet` skip，后者为 `status=exception`；二者都**不**产生假 ok。

#### 场景:目标进程/端点不在场必须 exception 非假 ok

- **当** JVM inspector 运行在目标 pid 不存在的目标上，或 Go inspector 运行在 pprof 端点不通的目标上
- **那么** 该 inspector **必须**以 `status=exception`（collector fail-loud `|| exit 1`）呈现，**禁止**伪造 `status=ok`；`process_pattern` 经 `pgrep` 空结果**必须**同样 fail-loud、**禁止**当作「0 进程 → ok」
- **且** 本套件的 snapshot 测试**必须**含至少一份「目标不在场」fixture（JVM pid 不存在 / Go 端口不通）断言此非假阴行为

#### 场景:JVM collector 校验抽取标量非空、不被 JDK 8 jstat exit-0 击穿

- **当** JVM inspector 的 `jstat`/`jcmd` 在跨用户 attach 被拒或进程刚退时（部分 JDK 8 此时打错误却 `exit 0`）
- **那么** collector **必须**在管道后**显式校验派生标量非空**（`[ -n "$used" ] || exit 1` 式二道门）从而仍 `status=exception`，**禁止**仅靠 `jstat` 退出码判定（否则 exit-0 + 凑数 → 假 `ok` + 假堆值）；命令串级断言**必须**锁住该非空二道门的存在

#### 场景:工具缺失走 requires_unmet 而非崩溃或假 ok

- **当** 目标主机缺少某 inspector `requires_binaries` 声明的二进制（无 JDK 即无 `jstat`/`jcmd`，或无 `curl`）
- **那么** runner preflight **必须**将该 inspector 标为 `status=requires_unmet` 并 skip、报告中标注，**禁止**报错中断同 run 其它 inspector、**禁止**伪造 ok

### 需求:套件内每个 inspector 必须附 ReplayTarget fixture 与可证检出的 snapshot

本套件内每个 inspector **必须**附带用 fixture 录制器（`inspector-fixture-recorder`）录制的 `ReplayTarget` 兼容 fixture 与 snapshot 测试，经离线回放确定性出 `InspectorResult`。**禁止**手写 fixture、**禁止**日常 CI 依赖真实运行时进程/网络。

为防止 no-op inspector 满足验收，每个 inspector **必须**至少附**一份触发预期 finding 的异常场景 fixture**（如 `go.goroutines` 给 total 远超 threshold → 检出泄漏；`jvm.heap` 给 heap 占比逼近上限 → 检出；`jvm.gc` 给时窗内高 Full GC → 检出），其 snapshot **必须**断言该场景产出预期 severity + message 语义；**且**至少附一份「目标不在场」fixture（见上一需求）。仅有「干净注册 + happy-path 无 finding」的 snapshot **不满足**验收。

凡涉及时间窗口/双采差的 inspector（如 `jvm.gc` 的 `sampling_window` 双采 `jstat -gcutil`），其窗口聚合（Full GC 次数增量 / GC 耗时占比）**必须**在**采样时刻**于目标机内算成**最终标量**并冻结进 collector 输出，`ReplayTarget` 回放**必须**原样返回该冻结标量；**禁止** collector 回吐需在回放时按 `now()` 重聚合的原始带时间戳明细（否则回放非确定，违反「离线回放确定性出结果」）。

**录制约定（D-7 偏离登记）**：ReplayTarget 回放时对整条 `collect.command` 返回录制的**最终 stdout**，collector 内的 `jstat`/`jcmd`/`awk`/`curl`/`pgrep` 在 offline **不执行**。因此 collector 内解析逻辑（jstat 列名锚定 / awk 抽取 / pgrep→pid / curl fail-loud）的**执行正确性**由**命令串级锁**（字节级捕获含正确列名/字段/fail-loud 守卫）+ **真机 Demo Path** 验证，**不**由 offline snapshot 行为级验证；下游「派生标量过阈值 → 检出 finding」由 finding-trigger fixture 证。offline fixture **不**声称锁住 collector shell 解析的执行正确性。

#### 场景:异常场景 snapshot 证明检出能力

- **当** 对某套件 inspector 运行其 snapshot 测试
- **那么** 测试集**必须**含至少一份异常场景 fixture，其 snapshot 断言 inspector 在该场景下产出预期 severity 与 message 语义的 finding；**禁止**只有 happy-path（无 finding）snapshot 就判该 inspector 验收通过

#### 场景:离线回放确定性出结果

- **当** 在任意平台（含 macOS / CI）对某套件 inspector 运行其 snapshot 测试
- **那么** 它**必须**经 `ReplayTarget` 回放录制的 fixture、不触达真实运行时进程或网络，并产出与快照一致的确定性 `InspectorResult`；时间窗口/双采差聚合**必须**在录制时已坍缩为冻结标量

#### 场景:collector 解析逻辑只命令串级锁不在 offline 行为级验证

- **当** 评估某套件 inspector 的 collector shell 解析逻辑（jstat 列名锚定 / awk 抽取 / pgrep 解析 / curl fail-loud）正确性
- **那么** 该正确性**必须**由命令串级锁（snapshot 断言字节级捕获的命令含正确列名/字段/fail-loud 守卫）+ 真机 Demo Path 验证；**禁止**声称 offline ReplayTarget fixture 锁住了 collector shell 的执行正确性（ReplayTarget 不跑真 shell，只回放最终 stdout）

### 需求:本套件禁止引入新基础设施

本套件**必须**在现有 schema 字段集内完成，证明运行时域铺量无需新 infra：**禁止**改动 inspector manifest schema（不增删字段）、**禁止**新增 parse format（仅 `raw/table/json/kv`）、**禁止**扩 capability enum（现为 `{shell, file_read, ssh, systemd, docker_cli}`，本套件只用 `shell`）、**禁止**新增 Python 运行时依赖、**禁止** enable `hook.py`。允许使用**现有** schema 字段（含 `collect.sampling_window`）。Agent 可见工具数组**必须不因本套件增减**（本套件不注册任何新 `ToolSpec`、不暴露新 Agent 工具）。

运行时依赖（`jstat`/`jcmd` 由 JDK 提供、`curl`）与「采集者须与目标 JVM 同用户」前提**必须**在 `description` 与 `tags`（tag 正则 `^[a-z][a-z0-9_-]*$`）中**文档式声明**；**禁止**新增 manifest 字段做机器式版本门或提权门。本套件 `privilege` **必须**为 `none`，**禁止**在 inspector 内自动 `sudo` attach（跨用户 JVM attach 失败按 `status=exception` 呈现）。

#### 场景:零对外契约变更

- **当** 套件实现完成
- **那么** inspector manifest schema、Agent 可见工具数组、parse format 集合、capability enum **必须**全部保持不变；**禁止**因本套件而改动任何对外运行时契约

#### 场景:运行时依赖与同用户前提用文档式声明、不自动提权

- **当** 某运行时 inspector 依赖 JDK 工具（`jstat`/`jcmd`）或要求采集者与目标 JVM 同用户
- **那么** 该前提**必须**在 `description` 与 `tags` 中文档式声明、`privilege` 保持 `none`；**禁止**新增 manifest 字段做机器式版本/提权门、**禁止**在 inspector 内自动 `sudo` attach（跨用户 attach 失败 → `status=exception`，非假 ok）
