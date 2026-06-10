# 设计：放开 Inspector 的 K8s target 支持

## Context

`KubernetesTarget`（`src/hostlens/targets/kubernetes.py`，#83）已实现完整 `ExecutionTarget` 协议：exec 经 pod exec API（`WsApiClient` 裸 websocket，整个脚本经 stdin 喂 `/bin/sh` + 尾部 `exit $?`）、`read_file` 经 exec tar-over-ws（≤10MB）、capability 懒探测（`{SHELL, FILE_READ}` 基线 + 运行时探测 `SYSTEMD`/`DOCKER_CLI`，`kubernetes.py:181`）、故障分类（`k8s_unavailable`/`pod_not_found`/`pod_not_running`/`container_not_found`/`container_not_running`/`exec_failed`/`file_too_large`）。

但 inspector 侧两道门把它锁在外面（有意的双重 gate，见 memory `project_new_target_type_inert_until_inspector_targets_widened`）：

1. **manifest 加载门**：`schema.py:586` `targets: Literal["local","ssh","docker"]`——声明 `k8s` 直接 Pydantic 报错。
2. **runner preflight 门**：`target.type not in manifest.targets` → `requires_unmet`。

本提案是 `add-kubernetes-target` 归档 proposal 非目标节显式预约的 follow-up，方法论 = 复用 `enable-docker-inspector-targets`（#82）的容器安全分类判据 + 内容式 meta-guard（见 memory `project_inspector_container_safety_classification`）。

约束：CLAUDE.md §4.2（Inspector 是 SOT）、§6（async-first / mypy strict / 真 fixture）、authoring-contract「零新 infra」纪律。

## Goals / Non-Goals

**Goals**：
- 把 `k8s` 加入 `InspectorManifest.targets`、`ReplayTarget.impersonate`（两处）与 recorder 透传集的 Literal 取值域（三处对齐）。
- authoring contract 的「容器适用性」判据由 docker 专属改写为容器类通用（docker / k8s 共用），附 pod 语义注记与 stdin 约束。
- 容器 cohort 28 个 manifest 追加 `k8s`（与 docker 同集，零增删），guard 升级为奇偶不变量。
- 用 `ReplayTarget(impersonate="k8s")` flip-impersonate 回放证明 k8s 派发路径端到端跑通。

**Non-Goals**：
- 不做 K8s 域 inspector（kubectl / API 视角，独立提案）。
- 不改 KubernetesTarget（含 default-container annotation，见 Open Questions）。
- 不改任何 collector / parse / output_schema / findings。
- 不做 ephemeral debug container / `target add --type k8s` CLI。

## Decisions

### Decision 1：取值域加 `k8s`，三处 Literal 对齐收口

`InspectorManifest.targets`（`schema.py:586`）、`ReplayTarget.impersonate`（`replay.py:87` 字段 + `:141` 实例属性 `self.type`，docker 版 review 抓过漏改后者 mypy 红）、recorder 透传 gate 元组与 `RecordedFixture.impersonate`（`recorder.py:292` gate、`:295` 报错文案、`:118` Literal、`:299` 注解）三处同步加 `k8s`。manifest `targets` 取值域至此与 `ExecutionTarget.type` 锁定全集 `["local","ssh","docker","k8s"]` 对齐收口；`"kubernetes"`、`"replay"` 等任意其他字符串继续被拒（「拒绝未知值」场景仍有测试价值）。

**recorder 选透传而非保持 fail-loud 拒 k8s**：docker 版把 recorder 收紧为「透传合法 impersonate 值、未知类型 fail-loud」——k8s 现在是合法值（KubernetesTarget 真实存在，对真 pod 录 fixture 语义成立），透传成本一行 + 一个测试，且「三处 Literal 对齐」比「两处对齐一处例外」更不易漂移。本提案的派发测试不依赖真录制（见 Decision 5），透传只是消除将来录 k8s fixture 时撞 `recorder_unsupported_target_type` 还翻不到决策记录的坑。

**备选（弃）**：recorder 对 k8s 继续 raise、登记非目标——省下的工作量趋零，换来一处人为不对称，弃。

**注**：recorder 的 impersonate 透传域无 spec 锁定（`inspector-fixture-recorder` spec 不含取值域需求）——docker 版同样未 spec 化，属继承的 baseline 现状而非本提案缺口；若未来 recorder 契约扩展，透传域应一并补进该 spec。

### Decision 2：k8s cohort 与 docker cohort 完全同集（INCLUDE 28 / EXCLUDE 37，零增删）

判据「collector 实际读取源是否容器隔离」是 target-agnostic 的；KubernetesTarget 与 DockerTarget 同为「exec 进单个容器内跑 shell 命令」，capability 集逐位相同。docker 版 Decision 4 的逐项评审全表（INCLUDE 28 / EXCLUDE 37）直接平移，**同集论证靠以下四支柱**（缺一即是「正确结论配不完整论证」）：

1. **判据 target-agnostic + capability 逐位相同**：preflight 兜底行为（ssh/systemd capability gate）在两种 target 上逐位一致。
2. **执行传输语义差异已逐个核查零违例**：docker exec 是 `["/bin/sh","-c",cmd]`、stdin 不接；k8s exec 把整个脚本经 stdin 喂 `/bin/sh` 且 v4 协议无 stdin half-close。任何**裸读 stdin** 的 collector（无参 `cat`、`awk -f -`）在 docker 上诚实失败（EOF），在 k8s 上会吞掉脚本尾部 `exit $?` 后阻塞到 timeout。已逐个核查 28 个 INCLUDE collector：全部是 `$(...)` 捕获 + 显式子命令、无 heredoc、零裸读 stdin。此差异固化为 authoring contract 的正文约束（见 Decision 4），防 6 个月后新 collector 写裸管道「docker 绿、k8s 全 timeout」。
3. **pod 语义逐维核查**：
   - **netns 是 pod 级共享**（docker 是容器级）：`net.*` 6 个在 pod 内看到 pod netns 含 sidecar socket——视角更宽不是误归因（pod IP 即诊断对象），仍 INCLUDE；docs 措辞与 docker 区分。
   - **`shareProcessNamespace: true`**：`linux.process.{zombies,critical_alive}` 看到 pause + 兄弟容器——pod-scope 非 host-scope，安全。
   - **EXCLUDE 类在 k8s 上理由更强**：容器内读 `/proc/meminfo`、`/proc/sys/*` 拿到的是 **node** 全局值，用户连 node 是哪台都未必知道，误归因比 docker 更隐蔽。
   - **read_file tar 依赖不对称是 moot**：65 个 manifest 无一声明 `file_read`；`nginx.error_rate` 的 `requires_files` 探测走 `exec("[ -r ]")` 不触 `read_file`。
   - **distroless 更普遍**：无 `/bin/sh` → `exec_failed` → `target_unreachable` 失败隔离；缺二进制 → `requires_unmet`，既有兜底原样覆盖。
4. **wrong-container 风险 docs 化**（见 Risks）。

**备选（弃）**：对 28 个逐项重新开一轮独立评审产新表——判据与读取源都没变，重评只会产出同一张表；新增的 pod 维度已在上面逐维核查，弃。

### Decision 3：guard 升级为容器类奇偶不变量，不复制第二份名单

`tests/inspectors/test_docker_target_cohort_guard.py` 升级三点：

1. **奇偶不变量**：对全部 builtin manifest 断言 `("docker" in targets) == ("k8s" in targets)`——容器安全是**一个属性不是两个**。既有 INCLUDE/EXCLUDE 冻结名单与 28/37/65 计数断言不变（奇偶断言下「INCLUDE 含 docker」自动蕴含「含 k8s」）。
2. **内容式 meta-guard 扩 k8s**：host 全局 marker（`/proc/sys/`、`/proc/meminfo`、`journalctl`、`/proc/loadavg`、`/proc/uptime`）命中的 manifest 由「禁 docker」扩成「禁 docker 且禁 k8s」。marker 列表不加新项——5 个 marker 在 pod 内读到的同样是 node 全局值/不存在的 journald，语义完全平移。
3. **docstring 容器类化 + escape hatch**：模块 docstring 与测试名去 docker 专属措辞；写明合法打破奇偶的出口——未来 **k8s-only 读取源** inspector（如读 `/var/run/secrets/kubernetes.io/serviceaccount/token` 做到期检查，docker 容器内无此文件）声明 `[k8s]` 不声明 `[docker]` 属合法，届时须同时修改 authoring-contract 判据 + 本断言，是一次显式决定。文件名保持不变（重命名会断 git blame，docstring 已澄清职责）。

**「冻得太死」不成立**：本文件本就全量冻结 65 个 manifest 分区（`test_rosters_partition_all_builtins_with_frozen_counts`），任何新 inspector 都必然要动它——奇偶断言零增量冻结成本，只是把「当下是一个属性」显式化。能打破奇偶的另一方向（docker-only）在本仓结构下不存在：依赖 docker socket 的管控类 inspector（`docker.*`）本就是 `[local, ssh]` + host 上的 CLI，不进容器 cohort。

**备选（弃）**：复制一份 `_INCLUDE_K8S` 冻结名单——两份名单必然漂移，且表达不了「同一个属性」的语义，弃。

### Decision 4：authoring contract 走 RENAMED + MODIFIED 容器类化，不 ADD 平行 k8s 需求

「容器适用性——inspector 声明 docker target 的判据」需求**重命名**为「容器适用性——inspector 声明容器类 target（docker / k8s）的判据」，正文容器类化（判据本体不变），新增：pod 语义注记（netns pod 共享 / shareProcessNamespace / node 值误归因）、**collector 禁裸读 stdin** 约束（写正文由作者评审执行，**不**加机械 guard——`cat {{log_path}}` 带参合法、`cat` 在管道中间合法，token pattern 匹配假阳太高，机械化不可靠的规则进 guard 只会成为噪声源）、奇偶不变量场景、k8s 回放验证场景。

**ADD 平行需求被硬证据否决**：现行 spec 文本与提案目标直接矛盾——`replay-execution-target` 的「impersonate 取值域限定」场景显式断言 `k8s` **必须 raise**；`inspector-plugin-system` 的 targets 字段写死「`k8s` 不在取值域内——KubernetesTarget 尚未实现」（理由在 #83 合并后已过期）。这些只能 MODIFIED 翻转；authoring contract 的判据若 ADD 平行需求，docker 版与 k8s 版两份判据文本必然漂移。

**RENAMED 的 archive 风险**按项目 SOP 处理（memory `project_openspec_modified_rename_archive`）：delta 写完后在 temp 副本实测 `openspec-cn archive` 的 rebuild 校验再 commit——`validate --strict` 过不代表 archive 过。

### Decision 5：k8s 派发用 flip-impersonate 回放证明，不录真 pod fixture

完全复用 docker 版 Decision 3 的策略与理由：collector 命令与 target 类型正交（同一条命令经 KubernetesTarget 还是 SSHTarget 派发，命令串与 parse 逻辑相同），collector 正确性已被既有 fixture 锁定。k8s 路径新增的风险只有：

- (a) schema 接受 `k8s` / 拒 `kubernetes`——单元测试
- (b) preflight `target.type in manifest.targets` 对 k8s 匹配——回放覆盖
- (c) KubernetesTarget capability gate 对 host-only inspector 判 `requires_unmet`——单元测试（ReplayTarget 声明受限 capabilities 模拟）
- (d) 端到端：k8s-typed target → `InspectorResult ok`——`tests/inspectors/test_k8s_target_dispatch_replay.py`，复用既有 docker 派发测试的 fixture 翻转 `impersonate: k8s`，覆盖「应用服务 / 语言运行时 / 进程级 / 网络」4 个代表类，断言 `status == "ok"`、`misses == []`（strict-consumption）且 snapshot 匹配

真 KubernetesTarget 的 exec/read_file 行为由 `kubernetes-execution-target` spec 与 `test_k8s_sdk_contract.py` 回归门锁定（见 memory `reference_kubernetes_asyncio_exec_sdk_shape`），不属本提案职责；Demo Path 第 4 步保留 kind 真 pod 人工兜底。

## Risks / Trade-offs

- **[wrong-container：多容器 pod 未配 `container:`]** KubernetesTarget 默认容器取 `spec.containers[0]`（`kubernetes-execution-target` spec 锁定），不尊重 `kubectl.kubernetes.io/default-container` annotation。istio 开 `holdApplicationUntilProxyStarts` 时 istio-proxy 是首容器 → exec 进 envoy。分层后果：`net.*` 仍正确（pod 共享 netns，从哪个容器看都一样）；服务/运行时类 `requires_binaries` unmet（fail-visible）；**最坏**是 `linux.process.critical_alive` 在 envoy 自有 PID ns 里 `pgrep` 不到目标进程 → critical 级误报——是噪声型假警报（方向上可见）非静默误归因，不构成 EXCLUDE 理由 → 缓解：docs 点名此场景 + 强烈建议多容器 pod 显式配 `container:`；根治（尊重 annotation）登记 Open Questions 为未来独立提案。
- **[误归因类漏判]** 同集平移若 docker 版评审有漏，k8s 继承 → 缓解：docker 表已经一轮提案 review + 内容式 meta-guard 实测；本提案奇偶不变量 + marker 扩 k8s 让两侧互相锁定。
- **[sidecar 噪声]**（不构成 EXCLUDE，docs 必须覆盖否则会收 false-positive issue）：`net.listening_ports` 对 envoy 的 0.0.0.0:15001/15006/15090 wildcard 监听逐个报 warning——operator 须把 sidecar 端口加进 `allowed_ports`；`net.connections` 的 `close_wait` 阈值度量的是 pod 聚合（含 envoy 连接池），finding 文案的「application is leaking」归因在 sidecar 场景含糊；`nginx.error_rate` 分母被 kube-probe 流量（liveness+readiness 每 5-10s）稀释，可能掩盖低频 5xx burst → 缓解：docs k8s 段三条注记，不改 manifest（改阈值/文案属 collector 变更，越界）。
- **[k8s exec `exit_code=None` 的 preflight 误判]** channel-3 解析失败（ws 异常断）时 `exit_code=None`，preflight 的 `exit_code != 0` 会把 None 归 `requires_unmet(["bin:xxx"])` 而非 unreachable——degenerate path，误判方向是保守跳过非误报 → 接受：记录于此不修（修 preflight 判定属 runner 行为变更，越界且收益趋零）。
- **[回放证明的局限]** flip-impersonate 不验证真 KubernetesTarget 的 ws exec 行为 → 接受：职责边界同 docker 版——target 行为由 `kubernetes-execution-target` spec + SDK contract 回归门锁定，Demo Path 真 pod 人工兜底。

## Migration Plan

- **无破坏性变更**：纯加法——Literal 各加一个允许值 + 28 个 manifest 追加 `k8s`。现存 manifest / fixture（`impersonate: local/ssh/docker`）全部不受影响。
- **回滚**：revert PR（移除三处 Literal 的 k8s + 各 manifest 的 k8s + 新增测试），无状态残留。
- 部署：feature branch `feat/enable-k8s-inspector-targets` → PR → CI 绿 → squash merge → 归档（RENAMED delta 先在 temp 副本实测 archive）。

## Open Questions

- **KubernetesTarget 是否尊重 `kubectl.kubernetes.io/default-container` annotation？** 本提案不动（spec 已锁 `containers[0]` fallback，改它要 MODIFY `kubernetes-execution-target` spec + 改 target 代码，范围蔓延）。登记为未来独立提案候选；当前以 docs「多容器 pod 强烈建议显式配 `container:`」缓解。
- **k8s-only 读取源 inspector（serviceaccount token 到期类）何时出现？** 出现时合法打破奇偶不变量（声明 `[k8s]` 不声明 `[docker]`），须同时修改 authoring-contract 判据 + guard 断言——escape hatch 已写进 guard docstring，属 K8s 域 inspector 提案的范围。
