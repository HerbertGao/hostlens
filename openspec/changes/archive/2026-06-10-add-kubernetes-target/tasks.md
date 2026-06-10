# Tasks：KubernetesTarget 只读执行 target

## 1. 依赖与配置层

- [x] 1.1 `pyproject.toml`：① 新增 optional extra `k8s = ["kubernetes-asyncio>=35"]`（**不**钉 `<36`——36.x 的 ws_client 与 35 逐字节同、撞 dependabot 无益，参考 memory `typer pin` 教训；如未来实测 major 不兼容再收）② **dev extra 自引用 `"hostlens[k8s]"`**（对称既有 `"hostlens[docker]"`，否则 CI 无 SDK，故障分类单测无法构造 `ApiException`、Demo Path「无 cluster mock」落空）③ `pytest` markers 加 `k8s_integration`
- [x] 1.2 `src/hostlens/targets/config.py`：新增 `K8sEntry`（`type: Literal["k8s"]` + `pod`(min_length=1) + `namespace="default"` + `container: str|None=None` + `kubeconfig: str|None=None` + `context: str|None=None`，`extra="forbid"`）；加入 `TargetEntry` 判别联合（discriminator="type"）
- [x] 1.3 `config.py` 占位校验：K8sEntry 全字段（pod/namespace/container/kubeconfig/context）均非 secret，含 `${...}` 时经既有 placeholder walker 拒为 `env_placeholder_not_allowed_here`（确认 walker 覆盖新字段名，不需特例）
- [x] 1.4 单测 `tests/targets/test_k8s_config.py`：type k8s 路由到 K8sEntry（默认值断言）/ 字段集严格（缺 pod raise、空 pod raise、extra raise、恰好 5 字段）/ 非 secret 字段占位 raise（对应 execution-target spec §场景:type k8s 路由 / k8s 字段集严格 / k8s 非 secret 字段占位被拒）

## 2. KubernetesTarget 核心（构造 + 只读约束 + capability）

- [x] 2.1 `src/hostlens/targets/kubernetes.py`：模块顶层 `try/except ImportError` 容错 import kubernetes-asyncio（未装仍可 import 模块）；`KubernetesTarget(name)` 构造器：`type="k8s"` 类常量、name 正则 enforce（不匹配 raise `invalid_target_name`）、`_entry`/`_client`/`_probed_caps` 初始化、`capabilities` 初值 `{SHELL, FILE_READ}`
- [x] 2.2 client 工厂（per-target 缓存、复用不重建）：**认证加载注意 async/sync 不对称**——`cfg = client.Configuration(); await config.load_kube_config(config_file=..., context=..., client_configuration=cfg)`（`load_kube_config` 是 **async** 且返回 KubeConfigLoader 非 Configuration、`client_configuration=None` 会设全局默认串 target，故必须显式传 cfg）；in-cluster 走 `config.load_incluster_config(client_configuration=cfg)`（**sync 不 await**）。用 cfg 构造**两个 client**：① `ApiClient(cfg)`→`CoreV1Api`（读 pod）② `WsApiClient(configuration=cfg)`→`CoreV1Api`（exec/read_file ws）；import `from kubernetes_asyncio.stream import WsApiClient`。构造/认证失败 → `k8s_unavailable`；未装 SDK → `k8s_sdk_unavailable`（消息含 `pip install "hostlens[k8s]"`）；退出 **`await client.close()`**（两 client 各 await，close 是 async；放 target 的 async 关闭路径）
- [x] 2.3 入口两道防线（顺序固定）：`exec`/`read_file` 顶部先 `_entry is None`→`k8s_no_entry`，再 `_entry.enabled is False`→`target_disabled`（均在任何 API 调用前）
- [x] 2.4 capability 懒探测：首次 exec 成功后用 `command -v systemctl`/`command -v docker` 探一次缓存到 `_probed_caps`；probe 失败不影响触发它的那次 exec 返回值
- [x] 2.5 pod 状态 / container 校验（**proactive 经常规 ApiClient `read_namespaced_pod(pod, namespace)`**，非 reactive 错误文案匹配）：不存在(404)→`pod_not_found`；`status.phase != "Running"`→`pod_not_running`(含 phase)；**先解析目标容器名**（`_entry.container` 非 None 用它，为 None → 默认容器 `spec.containers[0].name`）；指定 container 不在 `spec.containers[].name`→`container_not_found`(含可用容器名)；**None-safe running 态判定**：`status.container_statuses is None`(kubelet 未写竞态) / 该名不在列表 / 匹配项 `state.running is None`→`container_not_running`（**禁止裸迭代** `container_statuses`，None 会 `TypeError`）
- [x] 2.6 单测 `tests/targets/test_k8s_unit.py`（mock SDK，无 cluster）：type==k8s / 非法 name / disabled 不触发 API / standalone 无 _entry raise k8s_no_entry / client 复用 1 次 / capabilities 首次 exec 后才探（对应 kubernetes-execution-target spec 对应场景）

## 3. exec（stdin env 注入 + websocket + exit code）

- [x] 3.1 `KubernetesTarget.exec` 走 **WsApiClient 裸 websocket**：用 §2.2 的 `WsApiClient`-构造的 `CoreV1Api` 调 `connect_get_namespaced_pod_exec(..., command=["/bin/sh"], stdin=True, stdout=True, stderr=True, tty=False, container=_entry.container, _preload_content=False)`，返回 `_WSRequestContextManager`→`async with (await ...) as ws:` 进入拿 `aiohttp.ClientWebSocketResponse`。手工 channel framing：出站 `ws.send_bytes(b"\x00"+payload)`；入站每条 message **先判 `len(msg.data) < 1` 跳过**（空/keepalive/close 帧防 `IndexError`），再 `msg.data[0]` demux（1=stdout/2=stderr/3=error）。**禁止**写 `ws_client.exec`/假设可写 stdin handle/用普通 ApiClient 做 exec（都落不了地）
- [x] 3.2 **env 经 stdin 注入 + 确定性退出**：渲染 `export <K>='<single-quote-escaped V>'` 行 + cmd + **末尾 `\nexit $?\n`**（让 sh 读到 exit 即以 cmd 状态终止——v4 无 stdin half-close、`ws.close()` 后读不到 channel-3,故**不**靠 EOF/超时;sh 退出后 `async for msg in ws` 读到 channel-3 后 server 关连接循环结束），经 stdin（channel 0）写入；**禁止**进 command argv。① **value** 单引号转义（`'`→`'\''`）② **key 校验**：每个 env key 必须匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，否则 raise `invalid_env_key`（防 `export ; rm -rf /=...` 注入；与 docker `environment=` 不对称——k8s 经 shell 故须校验）
- [x] 3.3 exit code 经 **channel-3** 的 `v1.Status` 解析（`WsApiClient.parse_error_data` classmethod：Success→0；否则**盲取 `causes[0]["message"]` 转 int，不过滤 reason**——与 SDK 实测行为对齐，禁止自造「过滤 reason==ExitCode」分叉）；parse_error_data 缺 causes/message 会 raise，故 try/except 包裹，解析不出且非超时 → `exit_code=None`（不用魔数）；stdout/stderr UTF-8 errors=replace
- [x] 3.4 超时：外层 `asyncio.wait_for(timeout)` → `ExecResult(timed_out=True, exit_code=None)`；超时 best-effort 关 websocket
- [x] 3.5 故障：transport 级失败 raise TargetError；命令非零退出是正常 ExecResult 不 raise；无 `/bin/sh`→`exec_failed`（不归 k8s_unavailable）；**exec 阶段 TOCTOU**（读 pod 后 pod/container 消失）二次捕获→`pod_not_found`/`container_not_found`；**Running pod 内 container 非 running 态**→`container_not_running`
- [x] 3.6 单测（mock SDK）：env 经 stdin 不进 argv（断言 command==["/bin/sh"] 且不含 secret 子串、stdin 脚本含 `exit $?`）/ secret 不进 argv / **非法 env key→invalid_env_key** / **入站空帧(`b""`)/1-byte 帧被跳过不崩 IndexError** / 非零退出返 ExecResult(exit_code=3，经 channel-3 解析) / 超时返 timed_out / **container_not_running** / **TOCTOU pod 消失→pod_not_found**（对应 spec exec 场景）

## 4. read_file（tar-over-exec，复用 docker tar 语义）

- [x] 4.1 path 预校验（发请求前）：绝对路径 else `invalid_path`；NUL/换行 `invalid_path`；`..` 经 `posixpath.normpath` 折叠（对称 DockerTarget.read_file，可抽共享 helper 复用）
- [x] 4.2 经 exec 跑 `tar cf - <path>` 捕获 stdout tar 流；复用与 DockerTarget 一致的 tar 处理：单遍前向迭代、恰好一个 regular file、首个非 regular file→`not_a_file`、第二个 regular file→`not_a_file`、size>10MB（边读边累计 backstop）→`file_too_large`、边界 `>`、恰好 10MB 放行。**评估抽取 docker/k8s 共享 tar 解析 helper**避免两份漂移
- [x] 4.3 tar 退出码**复用 §exec channel-3 Status 解析**（不另造路径）；文件不存在判据 = **tar 非零退出 + stdout 无字节产出**（退出码为主、**不**靠英文 stderr 子串，防 locale 本地化失配）→ `FileNotFoundError`（与 exec_failed 区分：tar 能启动=非 exec_failed）；无 `tar`→`exec_failed`（消息提示需容器内 tar；cat fallback 是独立 follow-up 非本提案）
- [x] 4.4 单测（mock SDK 喂构造的 tar 字节流）：小文件 / 恰好 10MB 放行 / >10MB raise / 目录(多条目 dir typeflag)→not_a_file / **多条目超大归档优先 not_a_file（非 file_too_large）** / 相对路径→invalid_path / **NUL→invalid_path** / **换行→invalid_path** / **`..` normpath 折叠后读取** / 不存在→FileNotFoundError / 无 tar→exec_failed（对应 spec read_file 全部场景）

## 5. 故障分类 + scrub

- [x] 5.1 异常映射齐全：`k8s_sdk_unavailable` / `k8s_unavailable`(连接/401/403/config) / `pod_not_found`(404) / `pod_not_running`(含 phase) / `container_not_found`(含可用容器名) / `container_not_running`(running 态) / `invalid_env_key` / `exec_failed`；exec 阶段 TOCTOU 二次捕获（对应 spec §故障分类全部场景）
- [x] 5.2 异常包装前经 `scrub_exception_message`（显式 str 化 `exc.reason`/`str(exc)` 后喂）：脱敏 bearer token / `*_KEY=` / home 路径 / IP；保留 target name + kind
- [x] 5.3 单测：mock 各 ApiException → 验对应 kind；scrub 脱敏 bearer/home 路径（对应 spec §scrub 场景）

## 6. registry / doctor 接线

- [x] 6.1 `src/hostlens/targets/registry.py`：`build_registry_from_config` 加 `elif entry.type == "k8s": cast("ExecutionTarget", KubernetesTarget(name=entry.name))`（构造纯净不连 API，对称 docker 分支）
- [x] 6.2 `cli/_doctor_schema.py`：target row `type` Literal 加 `k8s`（纯枚举扩展；含 Rich markup 注意见 memory `project_rich_console_markup_eats_brackets`）
- [x] 6.3 doctor **不为 k8s 特判**（Decision 8，对称 docker）：走既有通用 `echo` 探针（`_probe_enabled_targets`），k8s target.exec 自然验 kubeconfig+API+pod+container；disabled→skipped；未装 SDK→exec 抛 `k8s_sdk_unavailable` 被 doctor 如实记录而非崩。**删除**「自定义 kubeconfig+version 探测」的旧设想（docker 没有、与既有机制不一致）
- [x] 6.4 **订正 stale K8S_EXEC forward-ref**（Decision 7，本提案决定不加 capability）：`src/hostlens/targets/base.py:34-35`（删「add-kubernetes-target will add K8S_EXEC」）、`src/hostlens/tools/schemas/list_targets.py:50`（删「M8 K8S_EXEC will」）、`tests/targets/test_capability.py:27`（删「M8 K8S_EXEC」）的注释/docstring 改为「k8s target 不引入新 capability」措辞
- [x] 6.5 单测：registry 构造 k8s target（无 cluster，仅类型/配置层）；**断言两个 client 各构造（读 pod 走 ApiClient、exec 走 WsApiClient），不只数认证工厂 1 次**；doctor k8s row 形态；`test_capability.py` 仍断言 Capability 恰好 5 成员（k8s 不增）

## 7. 集成测试（真 cluster，无则 skip）

- [x] 7.1 `tests/targets/test_k8s_integration.py`：`@pytest.mark.k8s_integration`；会话 fixture 检测 cluster 可达否则 `pytest.skip("k8s cluster unavailable")`；**禁止 mock kubernetes-asyncio**（grep guard：patch/mocker.patch/monkeypatch.setattr/patch.object + 同行 kubernetes 无命中）
- [x] 7.2 覆盖：成功 exec / 非零 exit / 超时取消(断返回值 timed_out=True,exit_code=None) / **env 经 stdin + 在 pod 内 `ps` 验证 secret 不出现在 argv** / **多容器 pod 选 container + 验证 cmd 不能读 stdin（已知限制）** / tar read_file 小文件 / 恰好 10MB 放行 / >10MB raise / 目录 not_a_file / 相对 invalid_path / 不存在 FileNotFoundError / pod_not_found / pod_not_running / container_not_found / capabilities lazy probe（grep guard 同时匹配 `kubernetes` 与 `kubernetes_asyncio` 别名，防 import-as 绕过）
- [x] 7.3 不列入集成覆盖（有意排除，附理由）：client 复用单次构造（归单测 mock 计数）；超时后 websocket 释放（async 固有限制，只断返回值不声称验证释放）
- [x] 7.4 cluster 用 session-scoped fixture（如 kind cluster + alpine pod）复用；用例间独立路径

## 8. 文档与收尾

- [x] 8.1 `docs/operations/`（targets 文档）：k8s target 配置示例（`type: k8s`/pod/namespace/container/kubeconfig/context）+ 认证说明（kubeconfig vs in-cluster）+ 所需 RBAC（get pods + create pods/exec）+ read_file 需 pod 内 tar 的已知限制
- [x] 8.2 `mypy --strict src/` 全绿：`pyproject.toml` 加 `[[tool.mypy.overrides]] module=["kubernetes_asyncio.*"]` + `ignore_missing_imports=true` + `follow_imports="skip"`（**无条件**，对称既有 `mcp.*` override：保证装/不装 `[k8s]` 两种环境 mypy 行为一致）；模块顶层 `try/except ImportError` 容错 import（未装仍过 mypy/可 import）
- [x] 8.3 **SDK 保真自检（可复算强锚，提案阶段已跑过 27/27 PASS @ kubernetes-asyncio 36.1.0）**：把对 SDK 形态的机械 introspection 固化为一个测试/脚本（`tests/targets/test_k8s_sdk_contract.py` 或 doctor 子检查）——断言 `WsApiClient` 可导入、channel 常量 STDIN/STDOUT/STDERR/ERROR=0/1/2/3、`parse_error_data` 是 classmethod 且盲取 causes[0]（无 reason 过滤）、`connect_get_namespaced_pod_exec`/`read_namespaced_pod` 调用返回 awaitable coroutine、`load_kube_config` async / `load_incluster_config` sync / `ApiClient.close` async、`load_kube_config` 接受 client_configuration/config_file/context、`V1PodStatus.container_statuses` 默认 None、`V1ContainerState.running`/`V1Pod.spec`/`.status` 字段存在、无 `CLOSE_CHANNEL` 常量。**SDK major 版本 bump 时此自检即回归门**（防 SDK 形态漂移静默破坏 exec/read_file）
- [x] 8.4 全量 pytest（py3.11/py3.12 心理预演，本地至少 py3.12）；k8s 集成测试无 cluster 自动 skip 不 fail
- [ ] 8.5 **exec websocket 最小 spike（实现期首验）**：在真 cluster（kind）上验 stdin-喂脚本→`exit $?`→channel-3 取 exit code 的端到端闭环（含非零退出、env 经 stdin 不进 ps），确认动态协议行为成立——这是 spec 阶段无法验、必须 M8 实现期真跑的部分
- [x] 8.6 `openspec-cn validate add-kubernetes-target --strict` 通过
- [x] 8.7 对本次变更跑对抗性 review（review-loop），triage + 修复到放行（含 src/ 运行时 + 新 target 契约 + 安全边界[stdin env 注入/凭据/只读]，必须 review）
- [x] 8.8 feature branch `feat/add-kubernetes-target` → PR → CI 绿 + Copilot/BugBot triage → squash merge → 归档
