# 任务：放开 Inspector 的 K8s target 支持

## 1. Literal 取值域放开（三处对齐）

- [x] 1.1 `src/hostlens/inspectors/schema.py:586` `targets` Literal 加 `"k8s"`，同步更新字段 docstring/注释中的取值域描述；**翻转既有拒绝断言**：`tests/inspectors/test_schema.py:151` 参数化把 `"k8s"` 移出拒绝集、`:186` `test_empty_or_k8s_targets_rejected` 的 `["k8s"]` 用例移入接受参数化（对齐 `test_docker_target_kind_accepted` 形态），:170-171 的 stale 注释（「KubernetesTarget 未实现」）同步改写。验收：`python -c` 加载 `targets: [k8s]` manifest 成功、`targets: [kubernetes]` / `targets: [replay]` 抛 `ValidationError`；`pytest tests/inspectors/test_schema.py -q` 绿
- [x] 1.2 `src/hostlens/targets/replay.py` 两处 Literal 加 `"k8s"`：`_Fixture.impersonate` 字段（:87）与实例属性 `self.type`（:141），模块 docstring（:16-18）的取值域描述同步；**翻转既有拒绝断言**：`tests/targets/test_replay.py:184` 参数化把 `"k8s"` 移出拒绝集、移入接受用例。验收：`mypy --strict src/hostlens/targets/replay.py` 过；fixture `impersonate: k8s` 加载成功、`impersonate: kubernetes` 加载 raise（`pytest tests/targets/test_replay.py -q`）
- [x] 1.3 `src/hostlens/inspectors/recorder.py` 透传 gate 元组（:292，raise 块至 :299）、报错文案 `"only local/ssh/docker fixtures are supported"`（:295）与 `RecordedFixture.impersonate` Literal（:118、:299 注解）加 `"k8s"`；`"kubernetes"` / `"replay"` 等未知类型继续 `recorder_unsupported_target_type` fail-loud；**改写既有 fail-loud 用例**：`tests/inspectors/test_recorder.py:415-440` 现以 `k8s` 当 unsupported 反例，须换成 `"kubernetes"`（或等价未实现值），另增「k8s 透传」正例。验收：`pytest tests/inspectors/test_recorder.py -q` 绿

## 2. Cohort manifest 机械追加

- [x] 2.1 给 28 个容器 cohort manifest（`test_docker_target_cohort_guard.py` 的 `_INCLUDE` 冻结名单）的 `targets:` 行机械追加 `k8s`（collector 命令零改动；逐文件 diff 确认只有 `targets:` 行变化）。验收：`hostlens inspectors list --json` 加载 65 个无错误；`git diff --stat` 仅 28 个 yaml 各 1 行
- [x] 2.2 **与 2.1 同 commit** 放宽三处既有上界断言（cohort 追加 `k8s` 后必红，不与 2.1 同步则中间态 CI 红）：`tests/inspectors/test_service_contract_crosscheck.py:630`、`tests/inspectors/test_replication_contract_crosscheck.py:289`、`tests/inspectors/test_incident_pack_manifests.py:83` 的 `assert set(manifest.targets) <= {"local", "ssh", "docker"}` 各扩为含 `"k8s"`，三处邻接注释（现引用 enable-docker-inspector-targets）同步容器类化。验收：`pytest tests/inspectors/test_service_contract_crosscheck.py tests/inspectors/test_replication_contract_crosscheck.py tests/inspectors/test_incident_pack_manifests.py -q` 绿

## 3. Guard 测试升级（奇偶不变量）

- [x] 3.1 `tests/inspectors/test_docker_target_cohort_guard.py` 新增奇偶不变量断言：对全部 builtin manifest 断言 `("docker" in targets) == ("k8s" in targets)`；既有 `_INCLUDE`/`_EXCLUDE` 冻结名单与 28/37/65 计数断言不动。验收：`pytest tests/inspectors/test_docker_target_cohort_guard.py -q` 绿；手工临时移除任一 manifest 的 `k8s`（保留 `docker`）应使奇偶断言红，验后还原
- [x] 3.2 内容式 meta-guard 断言由「禁 docker」扩成「既不含 docker 也不含 k8s」（5 个 host 全局 marker 列表不变）。验收：手工临时给 `linux/memory_pressure` manifest 加 `k8s` 应使 meta-guard 红，验后还原
- [x] 3.3 模块 docstring 与测试名去 docker 专属措辞（容器类化），docstring 写明奇偶 escape hatch（k8s-only 读取源类 inspector 须同步修改 authoring-contract 判据 + 本断言）；文件名不动（保 git blame）。验收：docstring 含 escape hatch 描述；`pytest tests/inspectors/test_docker_target_cohort_guard.py -q` 绿

## 4. k8s 派发路径回放测试

- [x] 4.1 新增 `tests/inspectors/test_k8s_target_dispatch_replay.py`：复用既有 `tests/inspectors/test_docker_dispatch_replay.py` 的 fixture 翻转策略改为 `impersonate: k8s`，覆盖「应用服务 / 语言运行时 / 进程级 / 网络」4 个代表类，断言 `InspectorResult.status == "ok"`、`misses == []`（strict-consumption）且 snapshot 匹配（snapshot 比对 `.rstrip("\n")` 容忍 pre-commit 尾换行）；另加全 cohort `{name: status}` snapshot 用例（对齐 docker 版 `test_docker_dispatch_cohort_snapshot`）。验收：`pytest tests/inspectors/test_k8s_target_dispatch_replay.py -q` 绿
- [x] 4.2 新增 capability gate 兜底用例：`ReplayTarget(impersonate="k8s")` 声明受限 capabilities（无 `ssh`/`systemd`），对要求这些 capability 的 inspector 断言 `requires_unmet`（KubernetesTarget 与 DockerTarget capability 集逐位相同的兜底行为）。验收：同文件 pytest 绿

## 5. 文档

- [x] 5.1 `docs/operations/inspectors.md`：修正 stale 陈述（:39 targets 表行仍写「KubernetesTarget 未实现」）；既有「Docker target 上可跑的 inspector」段（约 :152-222）容器类化——meta-guard 描述句扩为「不含 docker 也不含 k8s」、修正 :165 指向已归档路径 `openspec/changes/enable-docker-inspector-targets/design.md` 的死链（改指 archive 路径）；fixture 编写指南的 `impersonate` 取值域枚举（:436-438，「`local`/`ssh`/`docker`」与「`impersonate: docker` 用于离线验证 docker 派发路径」句）扩为含 `k8s` 并容器类化；新增 k8s target 段——多容器 pod 强烈建议显式配 `container:`（默认 `spec.containers[0]`，istio `holdApplicationUntilProxyStarts` 会 exec 进 envoy 致 `critical_alive` 误报）/ `net.listening_ports` 需把 sidecar 端口（15001/15006/15090）加进 `allowed_ports` / `net.connections` close_wait 为 pod 聚合（含 envoy 连接池）/ `nginx.error_rate` 分母被 kube-probe 流量稀释 / net.* 为 pod netns 视角。验收：文档 build/lint 过（pre-commit），内容覆盖上述 5 点 + 既有段三处更新（:39 / :152-222 含 :165 / :436-438）

## 6. 全量回归与收尾

- [x] 6.1 全量测试 + 类型 + lint：`pytest -q`、`mypy --strict src/`、pre-commit 全过（py3.11 与 py3.12 由 CI 矩阵覆盖）。验收：本地全绿
- [x] 6.2 真 cluster 人工 Demo Path（可选，需 kind/minikube + `pip install "hostlens[k8s]"`，缺 extra 会撞 `k8s_sdk_unavailable`）：`kind create cluster && kubectl run hl-redis --image=redis:7`，`targets.yaml` 写 `type: k8s / pod: hl-redis`，非 root 用户跑 `hostlens inspect hl-redis --inspector redis.memory_usage` 输出真实采集。验收：命令成功输出 `status: ok`；无 cluster 时跳过并在 PR 注明（已按验收跳过：本机无 cluster / 无 kind，PR 描述注明）
- [x] 6.3 归档预检：在 temp 副本实测 `openspec-cn archive enable-k8s-inspector-targets --yes`（RENAMED delta 的 rebuild 校验，提案期已验一次，实现期 delta 若有改动须重验）。验收：temp 副本 archive exit 0 且主 spec 重命名后标题正确
