# 提案：放开 Inspector 的 K8s target 支持

## 为什么

M8 的 `KubernetesTarget`（`targets/kubernetes.py`，#83）已落地，但它对 Inspector **当前是 inert 的**：`InspectorManifest.targets` 仍是 `Literal["local", "ssh", "docker"]`，任何 inspector 声明 `k8s` 会在 manifest 加载期被 Pydantic 拒掉；即便绕过，runner preflight 第一步 `target.type not in manifest.targets` 也会判 `requires_unmet`。结果是「有一个能 exec 进 pod 容器的 target，却没有一个 inspector 能跑在上面」。本提案是 `add-kubernetes-target` 归档时显式预约的 follow-up（其 proposal 非目标节点名了 `enable-k8s-inspector-targets`），把 KubernetesTarget 从 inert 变可用——这是 M8 的 K8s 半边收尾，与 docker 半边的 `enable-docker-inspector-targets`（#82）完全对称。

## 变更内容

- **MODIFY** `InspectorManifest.targets` 的 Literal 取值域：`["local", "ssh", "docker"]` → `["local", "ssh", "docker", "k8s"]`——至此与 `ExecutionTarget.type` 的锁定全集对齐，取值域收口；任意其他字符串（`"kubernetes"` / `"replay"` 等）仍被拒。
- **MODIFY**（authoring contract 增量）「容器适用性」需求由 docker 专属**重命名 + 改写为容器类通用**（docker / k8s 共用同一份判据）：判据本体不变（按 collector 实际读取源逐项判定容器隔离性，禁整域通配），新增两点：
  - **pod 语义注记**：k8s 的 netns 是 **pod 级共享**（含 sidecar socket，视角更宽不是误归因）；`shareProcessNamespace: true` 时进程类 inspector 看到 pause + 兄弟容器（仍 pod-scope，安全）；EXCLUDE 类在 pod 内读到的是 **node** 全局值，误归因比 docker 更隐蔽，排除理由更强。
  - **collector 禁止裸读 stdin**（如无参 `cat`、`awk -f -`）：KubernetesTarget 的 exec 把整个脚本经 stdin 喂给 `/bin/sh` 且 v4 协议无 stdin half-close，裸读 stdin 的 collector 在 docker 上诚实失败（EOF），在 k8s 上会吞掉脚本尾部的 `exit $?` 然后阻塞到 timeout。现有 28 个容器 cohort collector 已逐个核查**零违例**；此约束写进 spec 正文由作者评审执行，**不**加机械 guard（`cat {{log_path}}` 带参合法，token pattern 匹配假阳太高）。
- **MODIFY** `ReplayTarget.impersonate` 的 Literal：`["local", "ssh", "docker"]` → 加 `k8s`（**两处**：fixture 字段与实例属性 `self.type`），使 fixture 可以以 `impersonate: k8s` 回放、透明通过 runner preflight，对「k8s 派发路径」做离线 snapshot 测试；`recorder` 的透传集与 `RecordedFixture.impersonate` Literal 同步加 `k8s`（三处 Literal 对齐，未知类型继续 fail-loud）。
- **机械改动**：容器 cohort 的 28 个 manifest `targets:` 追加 `k8s`（collector 命令**不动**）。**cohort 与 docker 完全同集（INCLUDE 28 / EXCLUDE 37，零增删）**——判据是 target-agnostic 的（读取源是否容器隔离），KubernetesTarget 与 DockerTarget 同为 exec 进单个容器、capability 集逐位相同（`{SHELL, FILE_READ}` + 懒探测 SYSTEMD/DOCKER_CLI）；design 给同集论证与 pod 语义逐维核查。
- **测试**：cohort guard 升级为**容器类奇偶不变量**——`"docker" in targets ⇔ "k8s" in targets`（容器安全是一个属性不是两个），内容式 meta-guard 的 host 全局 marker 断言由「禁 docker」扩成「禁 docker+k8s」，28/37/65 计数断言不变；docstring 写明合法打破奇偶的 escape hatch（未来 k8s-only 读取源，如 serviceaccount token 类）。新增 k8s 派发路径的 flip-impersonate 回放测试（复用 docker 版策略，4 个代表类 + cohort snapshot）。
- **文档**：`docs/operations/inspectors.md` 修正 stale 陈述（现仍写「KubernetesTarget 未实现」），新增 k8s target 段：多容器 pod **强烈建议显式配 `container:`**（默认取 `spec.containers[0]`，istio `holdApplicationUntilProxyStarts` 场景会 exec 进 envoy）；istio sidecar 下 `net.listening_ports` 需把 envoy 端口（15001/15006/15090）加进 `allowed_ports`；`net.connections` 的 close_wait 度量为 pod 聚合（含 envoy 连接池）；`nginx.error_rate` 分母会被 kube-probe 流量稀释。

### 非目标（Non-Goals）

- **不做 K8s 域 inspector**（pod restart loop / OOMKilled / 事件巡检等）——那需要 kubectl / API 视角而非 pod-exec 视角，pod 内没有 kubectl，性质等同 docker.* 当初以 DinD 理由被排除；是 M6 剩余域的独立提案。
- **不改 KubernetesTarget 本身**——含「尊重 `kubectl.kubernetes.io/default-container` annotation」（`kubernetes-execution-target` spec 已锁定 `spec.containers[0]` fallback；改它属范围蔓延，登记 design Open Questions 为未来独立提案候选）。
- **不改任何 collector 命令 / parse 逻辑 / output_schema / findings**——本提案只动 `targets` 字段与三处 Literal 取值域。
- **不新增 manifest 字段、不 enable `hook.py`、不加 capability 值、不加 parse format**（沿用 authoring-contract「零新 infra」纪律）。
- **不做 ephemeral debug container**（`kubectl debug` 注入）与 `hostlens target add --type k8s` CLI 写入（与 docker 一致经 yaml 配置，CLI 留 follow-up）。

## 功能 (Capabilities)

### 新增功能

无——K8s 执行能力由既有 `kubernetes-execution-target` spec 提供；本提案只放开 inspector 侧的接入门。

### 修改功能

- `inspector-plugin-system`: `InspectorManifest.targets` 字段的 Literal 取值域与「targets 必须非空且仅含允许值」场景——`k8s` 由「必须 raise」改为「必须接受」，原拒绝理由（KubernetesTarget 未实现）已过期；取值域至此收口为 `ExecutionTarget.type` 全集。
- `inspector-authoring-contract`: 「容器适用性——inspector 声明 docker target 的判据」需求**重命名**为容器类通用（docker / k8s），判据正文容器类化 + pod 语义注记 + collector 禁裸读 stdin 约束；「内容式 meta-guard」与「派发路径代表性回放验证」场景扩展覆盖 k8s。
- `replay-execution-target`: `impersonate` 取值域加 `k8s`（现行「`k8s` / `kubernetes` 必须 raise」场景翻转为接受 `k8s`、仍拒 `kubernetes`）；recorder 透传集与 `RecordedFixture.impersonate` 同步。

## 影响

- **代码**：
  - `src/hostlens/inspectors/schema.py:586` —— `targets` Literal 加 `k8s`。
  - `src/hostlens/targets/replay.py` —— **两处** Literal 都要加 `k8s`：fixture 字段 `impersonate`（:87）**与**实例属性 `self.type`（:141）；docker 版 review 抓过「漏改后者则 mypy 红」。
  - `src/hostlens/inspectors/recorder.py` —— 透传 gate 元组（:292，raise 块至 :299）、报错文案（:295）与 `RecordedFixture.impersonate` Literal（:118）加 `k8s`，未知类型（`"kubernetes"` / `"replay"`）继续 `recorder_unsupported_target_type` fail-loud。
  - 容器 cohort 28 个 manifest 的 `targets:` 行（机械追加 `k8s`）。
- **测试**：`tests/inspectors/test_docker_target_cohort_guard.py` 升级（奇偶不变量 + marker 断言扩 k8s + docstring 容器类化）；新增 k8s flip-impersonate 派发回放测试；recorder k8s 透传测试；**既有测试同步点**——三处上界断言放宽（`test_service_contract_crosscheck.py:630` / `test_replication_contract_crosscheck.py:289` / `test_incident_pack_manifests.py:83` 的 `targets ⊆ {local,ssh,docker}` 扩为含 `k8s`，cohort 追加后必红）+ 四处「k8s 被拒」断言翻转（`test_schema.py:151/:186`、`test_replay.py:184`、`test_recorder.py:415-440` 反例换值）。
- **依赖**：无新增依赖（`kubernetes-asyncio` 已是 `hostlens[k8s]` optional extra，本提案不触碰）。
- **文档**：`docs/operations/inspectors.md`（修 stale：:39 targets 表行、:436-438 fixture 指南 `impersonate` 取值域、:165 归档死链；既有 docker 段容器类化 + 新增 k8s 段）。
- **对外契约**：Inspector manifest schema（`targets` 枚举加 `k8s`）与 replay fixture schema（`impersonate` 枚举加 `k8s`）放宽，纯加法向后兼容；Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / CLI 命令**全部不变**。

## Failure Modes

1. **pod 容器无 `/bin/sh`（distroless）**：exec API 报 OCI runtime 错 → KubernetesTarget 既有契约归 `exec_failed` → runner preflight 译为 `target_unreachable`，单 inspector 失败隔离不毁整轮。k8s 上 distroless 比 docker 更普遍，此降级路径预期高频。
2. **容器缺采集所需二进制**（alpine 无 `ss`/`mysql` client 等）：preflight `requires_binaries`（`command -v` 经 exec）非 0 → `requires_unmet(["bin:xxx"])`，不执行主命令、不误报。
3. **多容器 pod 未配 `container:` 且首容器是 sidecar**（istio `holdApplicationUntilProxyStarts`）：exec 进 envoy——net.* 仍正确（pod 共享 netns）；服务类 inspector `requires_binaries` unmet（fail-visible）；**最坏**是 `linux.process.critical_alive` 在 envoy 容器 PID ns 里 `pgrep` 不到目标进程 → critical 级**误报**（噪声型假警报，非静默误归因）。缓解：docs 点名此场景 + 强烈建议显式 `container:`。
4. **误把 host-only inspector 声明成 k8s**（评审漏判）：容器内读 host 全局源静默报 **node** 值，最危险因不报错。缓解：同集复用 docker 已评审的 INCLUDE/EXCLUDE 全表 + 奇偶不变量 + 内容式 meta-guard 扩 k8s 机械拦截。
5. **k8s exec channel-3 解析失败致 `exit_code=None`**：preflight 的 `exit_code != 0` 判定会把 None 归 `requires_unmet` 而非 unreachable——degenerate path（ws 异常断才触发），误判方向是保守跳过不是误报，本提案记录不修。

## Operational Limits

- **并发预算**：不变——派发并发由 orchestration pipeline 控制，target 类型不影响并发模型。
- **内存预算**：不变；KubernetesTarget `read_file` 10MB 上限已由其 spec 锁定（且 65 个 manifest 无一使用 `file_read`，preflight 文件探测走 `exec("[ -r ]")` 不触 read_file，tar 依赖不对称对本提案 moot）。
- **超时**：沿用 preflight 既有超时（`command -v` 10s、`[ -r ]` 5s）+ inspector `collect.timeout`；KubernetesTarget exec 经 `asyncio.wait_for` 同一约束。

## Security & Secrets

- **不引入新密钥**：服务类 inspector 的 `HOSTLENS_*` secret 注入沿用既有契约——KubernetesTarget 的 env 经 stdin 喂 `export`（单引号转义 + key 标识符校验，secret 不进 pod process list），由 `kubernetes-execution-target` spec 已锁定。
- **攻击面**：`targets` / `impersonate` Literal 各多一个合法值 `k8s`，不放宽任何注入防线（shlex.quote 三件套、字段正则、path component 校验全部不变）。kubeconfig 凭据处理不变（本提案不触碰 target 配置层）。
- **不扩大暴露**：MCP surface 不变——`run_inspector` MCP 工具的 schema / 敏感性声明不动。

## Cost / Quota Impact

- **零 LLM 影响**：纯 inspector/target 接入层，不调用 LLM、不改 Agent loop、不影响 prompt cache。token 消耗 0、API 调用频次不变、对 Anthropic 配额无影响。

## Demo Path

5 分钟内、无真 cluster / 无付费 API 的回放路径优先：

1. **schema 门**：`python -c` 加载一个 `targets: [local, ssh, docker, k8s]` 的 manifest → 成功；改成 `targets: [kubernetes]` → `ValidationError`。
2. **离线派发**：`pytest tests/inspectors/test_k8s_target_dispatch_replay.py -q` —— `ReplayTarget(impersonate="k8s")` 回放代表性 inspector，断言 `InspectorResult.status == "ok"`、`misses == []` 且 snapshot 匹配。
3. **guard**：`pytest tests/inspectors/test_docker_target_cohort_guard.py -q` —— 奇偶不变量 + marker 扩展断言全绿。
4. **（可选，需 kind/minikube 与 `pip install "hostlens[k8s]"`）真 pod**：`kind create cluster && kubectl run hl-redis --image=redis:7`，`targets.yaml` 写 `type: k8s / pod: hl-redis / namespace: default`，`hostlens inspect hl-redis --inspector redis.memory_usage` → 输出 pod 内 redis 的真实采集（缺 extra 时报 `k8s_sdk_unavailable`，属预期）。
