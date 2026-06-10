# 提案：KubernetesTarget 只读执行 target（M8 K8s 半边）

## Why

M8 target 抽象的 Docker 半边已交付（`DockerTarget` #81 + `enable-docker-inspector-targets` #82）。剩下 K8s 半边：`ExecutionTarget` 的 `type` Literal 早已锁定 `["local","ssh","docker","k8s"]`（`execution-target` spec），但 **没有 `k8s` target 实现**——配置里写 `type: k8s` 会在 loader 阶段失败（`TargetEntry` 判别联合无 `K8sEntry`）。本提案实现 `KubernetesTarget`（基于官方 `kubernetes-asyncio`），对称于 `DockerTarget`：只读 `exec`（经 pod exec API）+ `read_file`（经 exec `tar`）、capability 懒探测、故障分类，让 Hostlens 能巡检 K8s pod 内的容器。

## What Changes

- **NEW** `hostlens.targets.kubernetes.KubernetesTarget` —— 实现 `ExecutionTarget` Protocol，`type == "k8s"`，基于 `kubernetes-asyncio`（**原生 async SDK，不需 `asyncio.to_thread`**，与同步的 docker-py 关键不对称）。只对**已存在且 Running** 的 pod 内指定 container 做只读 `exec` / `read_file`，禁止任何 pod 生命周期操作。
- **NEW** `K8sEntry` 配置模型（`type: k8s`）：字段 `pod`（必填非空）、`namespace`（默认 `"default"`）、`container`（可选——多容器 pod 选容器，缺省走 pod 默认容器）、`kubeconfig`（可选路径）、`context`（可选 kubeconfig context）。加入 `TargetEntry` 判别联合。
- **NEW** `kubernetes-asyncio` 作为 **optional extra** `hostlens[k8s]`（对称 `hostlens[docker]`）；模块顶层 import 容错，未装时 `hostlens.targets.kubernetes` 仍可 import（mypy / registry 分支），只在实际构造/使用时 raise `k8s_sdk_unavailable`。
- **MODIFY** `build_registry_from_config`：加 `elif entry.type == "k8s": KubernetesTarget(name=entry.name)` 分支。
- **MODIFY** `hostlens doctor` 的 target 连通性检查 + `_doctor_schema.py` 的 target row `type` Literal 加 `k8s`（对称 docker）。
- **NEW** 集成测试 `tests/targets/test_k8s_integration.py`（`@pytest.mark.k8s_integration`，无 cluster 时 skip）+ 配置/registry 单元测试 `test_k8s_config.py`（无 cluster 可跑）。

### 非目标（Non-Goals）

- **不放开 inspector 的 k8s 支持** —— `InspectorManifest.targets` / `ReplayTarget.impersonate` 的 Literal **不加** `k8s`。本提案落地后 KubernetesTarget 对 inspector 仍 inert（与 docker 当初一致）；放开是独立 follow-up `enable-k8s-inspector-targets`（复用 docker 的容器安全分类判据 + 内容式 meta-guard，见 [[project_inspector_container_safety_classification]]）。**理由**：避免「manifest 声明了 type 但 inspector 尚未逐项评审 pod 容器安全」的半成品状态，且 pod 多容器 / sidecar / ephemeral 语义需独立评审。
- **不做写操作 / pod 生命周期**（create/delete/scale/exec 写命令）—— 只读诊断，对称 DockerTarget。
- **不支持远程 kubeconfig 凭据明文落配置** —— 凭据走标准 kubeconfig 文件 / in-cluster ServiceAccount，配置只存路径/context 引用。
- **不实现 `kubectl cp` 全功能 / 双向拷贝** —— `read_file` 单向只读、单文件、≤10MB，对称 DockerTarget。
- **不做 multi-cluster 联邦 / CRD 巡检** —— 单 cluster、pod-exec 粒度。

## Capabilities

### 新增功能（New Capabilities）

- **`kubernetes-execution-target`** —— `KubernetesTarget` 的完整契约（构造 / 只读约束 / exec websocket 语义 / read_file tar 语义 / 故障分类 / 集成测试），对称既有 `docker-execution-target` spec。

### 修改功能（Modified Capabilities）

- **`execution-target`** —— `TargetsConfig` / `TargetEntry` 增加 `K8sEntry`（`type: k8s` 字段集 `{pod, namespace, container, kubeconfig, context}`，`extra="forbid"`）；`build_registry_from_config` 增 k8s 分支。`type` Literal 本已含 `k8s`，无需改。

## Impact

- **代码**：`src/hostlens/targets/kubernetes.py`（新）、`config.py`（K8sEntry + 判别联合）、`registry.py`（k8s 分支）、`cli/_doctor_schema.py`（target row type 加 k8s）、`cli/doctor` 连通性。
- **依赖**：新增 optional `kubernetes-asyncio`（仅 `hostlens[k8s]` extra，核心安装不拉入）。
- **测试**：`test_k8s_integration.py`（真 cluster，无则 skip）+ `test_k8s_config.py`（无 cluster）。
- **文档**：`docs/operations/targets.md`（或等价）增 k8s target 配置示例 + 认证说明。
- **对外契约**：`TargetsConfig` schema（加 `type: k8s`）；doctor `--json` target row（type 加 k8s）。Inspector / Agent tool / MCP / Notifier / Schedule 契约**不变**。

## Failure Modes

1. **kubernetes-asyncio 未安装**（未装 `[k8s]` extra）：构造/首用时 raise `TargetError(kind="k8s_sdk_unavailable")`，消息含 `pip install "hostlens[k8s]"`；裸 `ImportError` 不冒泡。模块仍可 import。
2. **API server 不可达 / 认证失败 / RBAC 无 exec 权限**：raise `TargetError(kind="k8s_unavailable")`（含连接/认证类 `ApiException`），异常经 `scrub_exception_message` 脱敏（token / bearer / home 路径）。
3. **pod 不存在 / 非 Running / container 名不在 pod / container 非 running 态 / 非法 env key**：分别 `pod_not_found` / `pod_not_running`（含 phase）/ `container_not_found`（含可用容器名）/ `container_not_running`（CrashLoop 等，proactive 读 pod status 判定）/ `invalid_env_key`（env key 非 shell 标识符，防 export 注入），单 target 失败隔离不毁整轮。
4. **pod 内无 `/bin/sh`（distroless）**：exec API 报 OCI runtime 错 → `exec_failed`（**不**误归 `k8s_unavailable`——API/pod 正常,仅命令无法启动），对称 docker。
5. **exec websocket 中途断 / 超时**：`asyncio.wait_for` 超时 → `ExecResult(timed_out=True, exit_code=None)`（满足 ExecResult 不变量）；残留进程由 kubelet 回收，hostlens 不做进程组 kill（对称 SSH/docker）。

## Operational Limits

- **并发**：不变（派发并发由 orchestration 控制）；KubernetesTarget 持 per-target **两个 client**（常规 `ApiClient` 读 pod + `WsApiClient` exec ws），复用不重建。每次 exec/read_file 前 proactive `read_namespaced_pod` 多一次 GET（换取 locale-robust 的 pod/container 状态判定，有意权衡）。
- **内存**：`read_file` 10MB 上限——边读 tar 边累计、`> 10MB` 立即中止 raise（无条件 backstop,不先全读进内存),对称 docker。
- **超时**：exec 经 `asyncio.wait_for(timeout)`；capability probe（`command -v`）10s。

## Security & Secrets

- **凭据**：走标准 kubeconfig 文件 / in-cluster ServiceAccount token，**不**在 `targets.yaml` 存明文凭据（只存 kubeconfig 路径 + context 名）。
- **env 注入**：`exec` 的 `env` 经 pod exec API 的进程环境注入（**不**拼 `export VAR=val; cmd`,secret 不进 pod process list / 命令串),对称 docker/ssh。
- **脱敏**：API 异常经既有 `scrub_exception_message`（bearer token / `*_KEY=` / home 路径 / IP）；K8s API server URL 可能含主机名——异常里出现属可接受诊断信息（非凭据），不做额外承诺。
- **攻击面**：只读 exec,无生命周期写；只对 Running pod 操作；path 校验（绝对路径 + NUL/换行拒 + `..` 经 `posixpath.normpath` 折叠）对称 docker。

## Cost / Quota Impact

- **零 LLM 影响**：纯 target 执行层,不调 LLM、不改 Agent loop / prompt cache。token 0、Anthropic 配额无影响。

## Demo Path

5 分钟内、无真 cluster / 无付费 API 的路径优先：

1. **配置/registry 单测**：`pytest tests/targets/test_k8s_config.py -q` —— K8sEntry 解析（`type: k8s` 字段集严格、extra 拒）+ registry k8s 分支构造（不连 API）+ `k8s_sdk_unavailable` 路径，全部无 cluster 通过。
2. **故障分类单测**：mock `kubernetes-asyncio` ApiException 验 `pod_not_found` / `pod_not_running` / `container_not_found` / `exec_failed` 映射。
3. **（可选,需 cluster）集成**：`kind create cluster` + `kubectl run hl-redis --image=redis:7` 后在 `targets.yaml` 写 `type: k8s` / `pod: hl-redis` / `namespace: default`,`pytest tests/targets/test_k8s_integration.py`（无 cluster 自动 skip 不 fail）。
