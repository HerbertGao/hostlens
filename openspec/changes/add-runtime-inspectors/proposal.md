## 为什么

§M6 覆盖矩阵里**语言运行时**域（JVM heap/GC/thread、Go goroutine/heap）仍为 0 inspector。真实运维里「JVM 频繁 Full GC / 堆逼近上限」「Go 服务 goroutine 泄漏」是高频且难从 OS 层（CPU/内存）直接定位的故障——OS 看到的是「进程吃内存」，但根因在运行时堆/GC，需要运行时自带的探测工具（`jstat`/`jcmd`/Go pprof）才能看清。本提案补这个域。

与 os-shell wave（security/pkg，纯 OS 命令、无目标依赖）不同：运行时 inspector **需要目标运行时进程在场**（JVM 进程 / 暴露 pprof 的 Go 进程），采集要参数化目标（PID 或 pprof 端点），机制更接近 service inspector。因此**独立成 proposal**，不混进 os-shell wave。

## 变更内容

- 新增 **5 个运行时 builtin inspector**，启用 `jvm.*` / `go.*` 命名空间（文件落 `builtin/jvm/` / `builtin/go/`）：

  | 域 | 新增 inspector | 采集手法（preflight-gated binary；表中 `/` 为候选手法二选一，落地时 `requires_binaries` 只列 command 实际调用的那一个，合取门见 tasks 4.2） |
  |---|---|---|
  | JVM | `jvm.heap` | `jstat -gc <pid>` → heap used/committed 占比（committed 为 live ceiling；jstat -gc 无 -Xmx 列，vs -Xmx 口径留后续） |
  | JVM | `jvm.gc` | `jstat -gcutil <pid>`（collector 自行 read→`sleep {{ window_seconds }}`→read 双采；`sampling_window` 仅注入 `window_seconds`、不替 collector 差分）→ 时窗内 Full GC 次数 + GC 耗时占比 |
  | JVM | `jvm.threads` | `jcmd <pid> Thread.print` 计数 或 `jstack` → 线程总数 + BLOCKED 计数 |
  | Go | `go.goroutines` | `curl <pprof_endpoint>/debug/pprof/goroutine?debug=1` → goroutine 总数 |
  | Go | `go.heap` | `curl <pprof_endpoint>/debug/pprof/heap` → heap inuse / alloc 速率 |

- JVM inspector 参数 `pid`（或 `process_pattern` 经 `pgrep` 解析）；Go inspector 参数 `pprof_endpoint`（`pattern` 收紧为 IPv4 `host:port` 形）。
- 每个 inspector 纯 YAML（collector 内 `awk` 按表头列名抽取 + 派生标量、走 `parse.format: json`；pprof 取首行计数）；fixture 录真实 JVM（起个 sample JVM 进程）与真实 pprof 端点；含 finding-trigger（高 GC / goroutine 泄漏）与 unreachable（进程不存在 / pprof 端口不通）fixture。

### 完整 manifest 示例（`go.goroutines`）

```yaml
name: go.goroutines
version: 1.0.0
description: >
  Count live goroutines from a Go service's pprof endpoint. Requires the
  target process to expose net/http/pprof at pprof_endpoint. A high or
  monotonically growing count signals a goroutine leak.
tags: [go, runtime]
targets: [local, ssh]
requires_capabilities: [shell]
requires_binaries: [curl]
privilege: none

# parameters 必须是完整 JSON Schema（type: object + properties + additionalProperties:
# false）——loader 的 `_param_type_lookup` 只从 `properties` 建参数表，缺 wrapper 会让
# 参数对 loader 不可见、`| sh` 注入门与 pattern 约束双双静默失效。
parameters:
  type: object
  properties:
    pprof_endpoint:
      type: string
      pattern: "^[A-Za-z0-9._-]+:[0-9]{1,5}$"   # host:port, 收紧取值域
      default: "127.0.0.1:6060"
    threshold:
      type: integer
      default: 10000
  additionalProperties: false

collect:
  # debug=1 的 goroutine profile 首行 "goroutine profile: total N" 含计数。
  # pprof_endpoint 是 string 参数，必经 `| sh` 引用（loader 对 string 参数强制，
  # pattern 不豁免）——charset 已被 pattern 收紧，shlex.quote 对合法值是恒等变换。
  # FAIL-LOUD（POSIX/dash-safe，沿用既有 builtin 约定——禁 `set -o pipefail`，
  # 它非 POSIX、dash(/bin/sh on Debian/Ubuntu)会整脚本 abort exit 2）：先
  # raw-capture curl 输出并在 curl 上判退出码（管道前），再单独喂 awk；末行
  # `[ -n "$total" ] || exit 1` 二道门兜「curl exit 0 但无 total 行」。
  command: |
    raw=$(curl -fsS --max-time 5 "http://{{ pprof_endpoint | sh }}/debug/pprof/goroutine?debug=1" 2>/dev/null) || exit 1
    total=$(printf '%s\n' "$raw" | awk '/^goroutine profile: total/ { print $4; exit }')
    [ -n "$total" ] || exit 1
    printf '{"goroutines":%s}' "$total"
  timeout_seconds: 10

parse:
  format: json

output_schema:
  type: object
  properties:
    goroutines: { type: integer }
  required: [goroutines]
  additionalProperties: false

findings:
  - when: "goroutines > threshold"
    severity: warning
    message: "goroutine 数超过阈值，疑似泄漏"
```

## 功能 (Capabilities)

### 新增功能
- `runtime-inspector-suite`: 语言运行时 inspector 套件的**覆盖契约与质量门** —— 规定本套件必须覆盖 JVM 与 Go 两个运行时域、JVM ≥3 / Go ≥2 inspector，以及每个 inspector 的质量门（纯 YAML 遵守《作者契约》+ 需运行时进程/端点参数化 + ReplayTarget fixture 含 finding-trigger 与 unreachable + snapshot + 矩阵勾选 + 零新基础设施）。**遵守 spike D-9**：不为单个 inspector 立行为 spec，规范性内容是套件层方法论。**另立 capability 而非塞进 os-shell-inspector-suite**：运行时 inspector 需「目标进程/端点在场」的前提与 OS-shell「零目标依赖」正交，混入会污染 os-shell 套件「零外部依赖」的不变量。

### 修改功能
- 无。`inspector-plugin-system` / `inspector-authoring-contract` / `inspector-fixture-recorder` 均不变、仅被引用遵守。

## 影响

- **新增代码**：`builtin/jvm/`（3 个 `.yaml`）+ `builtin/go/`（2 个 `.yaml`）。
- **新增测试**：5 个 inspector 的 snapshot + fixture（`tests/inspectors/fixtures/jvm/`、`fixtures/go/`）；扩 loader / capability-gate 断言。
- **文档**：勾选 `TODO.md` §M6 矩阵运行时单元格。
- **对外契约影响**：Inspector manifest schema 不变；registry 扩 5 个 builtin；Agent 工具数组不变；不涉及 MCP/Notifier/Schedule/CLI。
- **依赖**：不新增 Python 依赖。`jstat`/`jcmd`（JDK 自带）、`curl` 由 preflight 探测，缺失 → `requires_unmet`。

## 非目标（Non-Goals）

- **不做 APM / 持续 profiling / 火焰图** —— 只做「即时快照 + 阈值」巡检型，不抓 trace、不做连续采样存储。
- **不内嵌 JMX/agent** —— 只用运行时自带 CLI（jstat/jcmd）与已暴露的 pprof 端点，不要求目标加载 java agent 或改代码（pprof 端点是 Go 服务自身已暴露的前提，未暴露则 `requires_unmet`）。
- **不做 Python/Node/Ruby 运行时** —— 本期只 JVM + Go；其余运行时留后续 proposal。
- **`pprof_endpoint` 只支持 IPv4 `host:port`** —— `pattern` 收紧为 `^[A-Za-z0-9._-]+:[0-9]{1,5}$`，**不**支持 IPv6 字面量 `[::1]:port`（方括号/冒号超出 charset）；IPv6 端点留后续（需要时单独放宽 pattern 并补注入测试）。`localhost:6060` / `127.0.0.1:6060` 在支持范围。
- **不改 schema/capability/parse-format**。

## Failure Modes

1. **目标进程不存在 / PID 错** → `jstat <pid>` 失败 → collector fail-loud exit 1 → `status=exception`，不伪造 ok。
2. **pprof 端点未暴露 / 不通** → `curl -fsS` 非零退出 → exit 1 → `status=exception`。
3. **无 JDK（jstat 缺失）/ 无 curl** → preflight `requires_binaries` → `requires_unmet` skip。
4. **JVM `jstat` 输出列随 JDK 版本漂移** → collector 内 `awk` 按**表头列名**锚定（非 `parse.format: table` 列位）、抽成标量走 `parse.format: json`；fixture 录多 JDK 版本样本回归。
5. **JDK 8 `jstat` 读不到 hsperfdata 却 exit 0** → 不只信退出码，collector 管道后 `[ -n "$x" ] || exit 1` 非空二道门兜假阴（D-6.1）。
6. **fixture 与 runner 命令漂移** → 录制器强制（起真 JVM/真 pprof 录制），禁手写。

## Operational Limits

- **并发**：不引入新并发；单 inspector `collect.timeout_seconds` ≤10s。`jvm.gc` 双采差的 `timeout_seconds` **必须** > `sampling_window.duration_seconds` + 余量（建议 `duration_seconds` 默认 5、`timeout_seconds` ≥10，留 jstat 两次调用余量）；否则 sleep 窗口本身会先触发 timeout。
- **内存**：collector 输出小 JSON（堆/GC/goroutine 计数，<10KB）；pprof goroutine?debug=1 只取首行 total，不下整个 profile。

## Security & Secrets

- **不引入新密钥**：jstat/jcmd 读本机 JVM、curl 打本机 pprof 端点，无凭据。
- **攻击面**：`pprof_endpoint` 经 `pattern` 收紧为 `host:port`，不裸拼进 shell；`pid` 经整数校验。pprof 端点本身是目标进程已暴露的内部端点，inspector 不开新端口。
- **脱敏**：运行时计数无 PII；不涉敏感输出。

## Cost / Quota Impact

- **零 LLM token**：采集层，不调 LLM。`list_inspectors` +5 项元数据。

## Demo Path

```bash
pytest tests/inspectors/ -k "jvm or go" -v
# 验证点: 5 inspector 全部加载; go.goroutines 给 total=50000 的 pprof fixture + threshold=10000
#   → 检出泄漏(finding); jvm.heap 给进程不存在 fixture → status=exception(非假阴);
#   jstat 多 JDK 版本 fixture → awk 列名锚定(非 table parse)稳定
hostlens inspectors list | grep -E "jvm\.|go\."

# 真机 Demo Path（offline 不验的 collector-shell 行为级正确性）——必须在
# /bin/sh→dash 的 host(Debian/Ubuntu)上跑一次,确认 collector 不依赖 set -o pipefail
# (dash 会整脚本 abort) 且健康目标返 status=ok(非假 exception):
#   带真实 JDK: hostlens inspect <dash-host> --only jvm.heap --param pid=<live-jvm-pid>
#   带真实 Go pprof: hostlens inspect <dash-host> --only go.goroutines --param pprof_endpoint=127.0.0.1:6060
```
