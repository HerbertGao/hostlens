# 设计：KubernetesTarget 只读执行 target

## Context

`ExecutionTarget.type` Literal 早已锁 `["local","ssh","docker","k8s"]`（`execution-target` spec），DockerTarget（#81）+ docker inspector 放开（#82）已交付。本提案补 K8s 半边：实现 `KubernetesTarget`，并加 `K8sEntry` 配置 + registry k8s 分支。设计基线 = 既有 `docker-execution-target` spec（对称),但 K8s 与 Docker 在 SDK 形态、exec API、文件读取上有**三处关键不对称**,必须在实现里区别处理而非照抄。

约束:CLAUDE.md §6 async-first / mypy strict / 真 fixture(集成测试用真 cluster 不 mock SDK);只读诊断红线(无 pod 生命周期写)。

## Goals / Non-Goals

**Goals**:实现 `KubernetesTarget`(kubernetes-asyncio)只读 exec + read_file + capability 懒探测 + 故障分类;`K8sEntry` 配置 + 判别联合 + registry 分支;`hostlens[k8s]` optional extra;doctor 支持 k8s target;集成测试(真 cluster,无则 skip)+ 配置/故障单测(无 cluster)。

**Non-Goals**:不放开 inspector 的 k8s(targets/impersonate Literal 不加 k8s——独立 follow-up `enable-k8s-inspector-targets`,见 [[project_inspector_container_safety_classification]]);不做 pod 生命周期写;不支持明文凭据落配置;不做 multi-cluster/CRD;read_file 不做 kubectl-cp 全功能(单向只读单文件≤10MB)。

## Decisions

### Decision 1:kubernetes-asyncio 原生 async,**禁止** asyncio.to_thread

DockerTarget 用同步 docker-py,故所有阻塞调用包 `asyncio.to_thread`。kubernetes-asyncio 是**原生 async**——所有 API 直接 `await`,**禁止**再包 to_thread(多一层是反模式、且会把 async 调用错误地塞进线程池)。这是与 docker spec 的首要不对称,实现时不能照抄 docker.py 的 to_thread 包裹。

**备选(弃)**:用同步 `kubernetes` client + to_thread——弃,既然有官方 async SDK,async-first 项目应直接用,避免 to_thread 开销与心智负担。

### Decision 2:exec 走裸 websocket + stdin 喂 shell exports,**不**进 argv

**关键发现 A —— SDK 形态(review 下载 SDK wheel 源码核查纠正)**:kubernetes-asyncio **没有**同步 `kubernetes` client 的高层 `stream()` helper(无 `write_stdin()` / `peek_stdout()` / `returncode`)。要写 stdin 必须走裸 websocket,具体:

- **必须用 `WsApiClient` 构造 exec 用的 `CoreV1Api`,且 per-target 需两个 client**(review B1,实测确认):`connect_get_namespaced_pod_exec(..., _preload_content=False)` **仅当** `CoreV1Api(api_client=WsApiClient(configuration=...))` 时才返回 websocket;用普通 `ApiClient` 做的是普通 HTTP 请求、写不了 stdin。而**读 pod 状态**(`read_namespaced_pod`)必须用**普通 `ApiClient`**(`WsApiClient` 会对 GET 也试 ws 升级而坏)。故 KubernetesTarget **per-target 持两个 client**:常规 `ApiClient`→`CoreV1Api`(读 pod status)+ `WsApiClient`→`CoreV1Api`(exec/read_file 的 ws);两者均按 `_entry`(Decision 5 的 Configuration)各构造一次、复用不重建、退出各自 `close()`。import:`from kubernetes_asyncio.stream import WsApiClient`、`from kubernetes_asyncio.stream.ws_client import STDOUT_CHANNEL, STDERR_CHANNEL, ERROR_CHANNEL`
- **`_preload_content=False` 返回 `_WSRequestContextManager` 非裸 response**(review #8):须 `async with (await connect_...(...)) as ws:` 进入上下文管理器才拿到真正的 `aiohttp.ClientWebSocketResponse`(`ws`);**不能**直接对返回值 `.data` 索引
- **出站 stdin 帧** = `b"\x00" + payload`(channel 0 = stdin),经 `ws.send_bytes(...)`;大 payload 由 ws 传输层透明分片、不需 app 层 chunk
- **入站 demux**:每条 ws message 取 `msg.data`,**先判 `len(msg.data) < 1` 则跳过**(空 / keepalive / close 帧——SDK 自身 `_preload_content=True` 路径即有 `if len(msg) > 1` 守卫;裸路径必须复刻,否则 `b""[0]` 抛 `IndexError`),再用首字节 `msg.data[0]` 作 channel:`1`=stdout、`2`=stderr、`3`=error(exit status)
- **exit code**:channel 3 的 payload 是 `v1.Status` JSON,经 `WsApiClient.parse_error_data`(**classmethod**,非模块级函数)解析;`status=="Success"`→0,否则 **SDK 盲取 `details.causes[0]["message"]` 转 int**(实测——SDK **不**按 `reason=="ExitCode"` 过滤;**禁止**实现者自造「过滤 reason」分叉解析)。解析须 try/except——无 channel-3(ws 异常断)/ Status 缺 causes/message 致 parse_error_data raise → `exit_code=None`(契约合法,不崩)
- **进程退出机制(review B1 纠正——v4 协议无 stdin-CLOSE 通道,不能靠 EOF/超时)**:v4.channel.k8s.io **无**单方向 stdin half-close(CLOSE_CHANNEL=255 是 v5 才有),且 `ws.close()` 后无法再读 channel-3。故 `/bin/sh` 必须靠**脚本自身显式退出**:渲染的 stdin 脚本**末尾追加 `\nexit $?\n`**(或等价),使 sh 读到 `exit` 即以前一命令状态终止——**不**依赖 stdin EOF、**不**依赖超时。终止后 server 发 channel-3 status → client 在 `async for msg in ws` 循环里读到 → server 关连接循环自然结束 → 再取 exit code。**禁止**把「正常 exec 退出」写成「靠 `asyncio.wait_for` 超时关 ws」(那会让每次 `echo`/`exit 3` 都撞超时返回 `timed_out=True`、`exit_code=3` 场景无法实现)。`asyncio.wait_for` 仅作**真卡死**(命令 hang)的兜底,非正常退出路径

**禁止**在 design/spec 里写 `ws_client.exec` / 抽象「可写 stdin handle」/「一个 ApiClient 同时干 exec 和读 pod」——都是错误形态,照写实现落不了地。

**关键发现 B —— env 注入(K8s exec API 无 `environment=` 参数,与 docker exec_run 不对称)**:要安全注入 env(secret 不进 pod `ps`/process-list),`command=["/bin/sh"]`(无参,从 stdin 读),把 `export <K>=<quoted-V>\n...\n<cmd>\n` 经 **stdin** 写入。

**为何不用别的**:
- ❌ `command=["env","K=V","sh","-c",cmd]` —— `env` 进程 argv 含 `K=V`,secret 进 process list。
- ❌ `command=["/bin/sh","-c","export K=V; "+cmd]` —— export 进 sh 的 argv,secret 进 process list。
- ✅ stdin 喂入:argv 仅 `["/bin/sh"]`,env+cmd 走 stdin,不进 process list,符合 docs/ARCHITECTURE.md §4 命令渲染安全规则。

**env value 与 key 都要约束**(review 抓出):
- **value** 经 shell 单引号转义(`'`→`'\''`),含换行 / `$()` / 反引号在单引号内不展开
- **key** 经 shell `export` 必须是**合法 shell 标识符**——实现必须校验每个 env key 匹配 `^[A-Za-z_][A-Za-z0-9_]*$`,否则 raise(否则 `{"; rm -rf /":"x"}` 渲染成 `export ; rm -rf /=...` 注入)。**与 DockerTarget 的不对称**:docker `environment=`dict 不经 shell 故 key 天然安全;k8s 经 shell export 故 key 必须校验。env key 实际来自受控 inspector 参数,此校验是 defense-in-depth

**已知语义损失(review F1,写入 spec 与 docs)**:因 cmd 本身经 stdin 喂给 `/bin/sh`,**pod 内 cmd 不能再从 stdin 读外部输入**(stdin 已被 export+cmd 脚本占用并 EOF)。与 docker `environment=`(cmd 的 stdin 空闲)/ LocalTarget/SSHTarget **不完全等价**。inspector collector 几乎不读 stdin,影响面小,但 spec 必须显式声明此限制、不能笼统声称「shell 语义与 local/ssh 一致」。

### Decision 7:本提案**不**新增 Capability,订正 stale 的 K8S_EXEC forward-ref

KubernetesTarget 用既有 `{SHELL, FILE_READ}` + 懒探测既有 `SYSTEMD`/`DOCKER_CLI`,**不**引入新 Capability。理由:Capability 是 target-agnostic 的「inspector 需要什么能力」(SHELL/FILE_READ/...),inspector 不会声明「我要 K8S_EXEC」而是「我要 SHELL」;k8s target 提供 SHELL+FILE_READ(与 docker 同),无需 k8s 专属 cap。

**但 in-repo 有 stale 承诺需订正**(review F7/CR#3):`src/hostlens/targets/base.py:34-35`、`tools/schemas/list_targets.py:50`、`tests/targets/test_capability.py:27` 注释 + `execution-target` spec 行 52 均写「M8 `add-kubernetes-target` will add `K8S_EXEC`」。docker 当初也没加(用既有 DOCKER_CLI),k8s 同样不加——这些 forward-ref 已 stale。本提案订正它们(删除 K8S_EXEC 承诺措辞),作为 tasks 一项;`execution-target` spec 行 52 经本提案已 MODIFY 该 spec 顺带订正。

### Decision 8:doctor 不为 k8s 特判,走既有通用 echo 探针(对称 docker)

既有 doctor 对所有 enabled 非-replay target 跑通用 `echo` 探针(`_probe_enabled_targets`),docker 未特判即走这条。k8s target 首次 `exec`(echo)本就会验 kubeconfig+API+pod+container,echo 探针**够用且对称 docker**——故 **k8s 不特判**,删除原 design/tasks 里「验 kubeconfig+API version 的自定义探测」(那是 docker 没有的新代码路径、与既有机制不一致)。`_doctor_schema.py` 的 target row `type` Literal 仍需加 `k8s`(纯枚举扩展);disabled→skipped、未装 SDK→`exec` 抛 `k8s_sdk_unavailable` 被 doctor 如实记录而非崩。

### Decision 3:read_file 经 exec `tar`-over-stdout,复用 docker tar 语义

K8s 无 docker `get_archive`(daemon-side tar)的等价物。`read_file` 经 exec 跑 `tar cf - <path>`、捕获 stdout tar 流,然后**复用与 DockerTarget 完全一致的 tar 处理**:单遍前向迭代、恰好一个 regular file、首个非 regular file → `not_a_file`、size>10MB(边读边累计 backstop)→ `file_too_large`、边界 `>`。这是 `kubectl cp` 的同款机制。

**代价/限制**:`tar` 必须在 pod 内(docker get_archive 是 daemon-side 不需容器内有 tar)。distroless 无 tar 的 pod **不支持 read_file** → `exec_failed` 带提示。这是 K8s 与 Docker 的不对称限制,写入本 Open Questions + docs。**抽象层面**:多数 inspector 经 `exec` 采集(shell 命令),`read_file` 用得少,故 tar 依赖影响面有限。

**备选(弃)**:`cat`+`stat`/`wc` 两步(查类型+size 再读)——需 `stat`/`wc`,同样有容器工具依赖,且 not_a_file/size 判定要自己拼,不如复用 docker 已验证的 tar 单文件逻辑统一。tar 方案最大化与 docker 的代码/语义复用。

### Decision 4:exit code 经 exec error channel 的 Status 解析

docker exec_run 直接给 `ExitCode`。K8s exec 协议在 stream 关闭时经 **error channel** 返回 `v1.Status`:成功 `status=="Success"`(exit 0),否则 `WsApiClient.parse_error_data` **盲取 `details.causes[0]["message"]` 转 int**(SDK 实测**不**按 `reason=="ExitCode"` 过滤)。解析须 try/except——解析不出且非超时 → `ExecResult(exit_code=None, timed_out=False)`(契约允许,不用魔数)。

### Decision 5:认证 = kubeconfig(路径/context)或 in-cluster,凭据不落配置

`K8sEntry` 只存 `kubeconfig`(路径)+ `context`(名),凭据在文件内容 / in-cluster ServiceAccount token。**认证加载的 async/sync 不对称(review B2 实测纠正)**:`config.load_kube_config` 是 **`async def` 且返回 `KubeConfigLoader` 不返回 Configuration**(`client_configuration=None` 时它设全局默认,会让多 target 串认证)。故必须**显式建 Configuration 并传入**:`cfg = client.Configuration(); await config.load_kube_config(config_file=<kubeconfig>, context=<context>, client_configuration=cfg)`;in-cluster(`KUBERNETES_SERVICE_HOST` 存在)走 `config.load_incluster_config(client_configuration=cfg)`(**sync,不 await**——与 load_kube_config 不对称);失败 → `k8s_unavailable`。**该 `cfg` 同时喂 Decision 2 两个 client**:`ApiClient(cfg)`(读 pod)与 `WsApiClient(configuration=cfg)`(exec ws),保证同一认证上下文且 per-target 隔离(不污染全局默认)。两 client 的 `close()` 也是 **`async def`,须 `await client.close()`**(放 target 的 async 关闭路径)。

### Decision 6:container 选择语义与 DockerEntry 不同

`DockerEntry.container` 必填(容器引用)。`K8sEntry.container` 可选(pod 内多容器选择器,缺省走默认容器)。指定但 pod 内不存在 → `container_not_found`(含可用容器名)。这是配置层有意的语义差异,spec 已点明。

## Risks / Trade-offs

- **[tar 依赖]** distroless pod read_file 不可用 → 缓解:`exec_failed` 带明确提示 + docs 记已知限制;exec 采集不受影响(主路径)。
- **[stdin env 注入复杂度]** websocket stdin 写入 + 关闭时序比 docker `environment=` 复杂,易出 bug(写完没 flush/close 导致 sh 挂起)→ 缓解:集成测试在 pod 内 `ps` 验证 secret 不进 argv + 验证 env 生效 + 超时兜底。
- **[exec error channel 解析]** k8s exec 的 channel-3 `v1.Status` 解析依赖协议/SDK 细节,版本差异可能变 → 缓解:解析不出 exit code 走 `exit_code=None`(契约合法降级,不崩);集成测试覆盖非零退出断言。
- **[busybox vs GNU tar 格式差异]**(review F5) read_file 的 tar 流由**容器自带 tar**生成(alpine=busybox / GNU / bsdtar header 格式不同),不像 docker daemon-side `get_archive` 格式稳定 → 缓解:Python `tarfile` `r|*` 自动探测覆盖主流格式;符号链接经 `member.isreg()` False→not_a_file 对各实现一致;集成测试用 alpine(busybox tar)恰好暴露边缘格式。已知残余:超长 path / sparse file 的极端编码差异不保证,记为已知限制。
- **[read_file 取 tar 退出码 + locale stderr]**(review F6) read_file 判「文件不存在」须拿 tar 退出码——复用 §exec 的 channel-3 Status 解析(非零退出 + stderr 含路径错误)。**退出码为主判据、stderr 子串为辅**:非英文 locale 下 tar 错误文案本地化,**不**单靠英文 "No such file" 子串;退出码非零且无字节产出 → `FileNotFoundError`,退出码非零但能拿到错误信号无法区分时偏保守报 `FileNotFoundError`(tar 进程能启动=非 exec_failed,只有 tar 不存在/无法启动才 exec_failed)。
- **[exec 阶段 TOCTOU]**(review F10) 读 pod(Running)与建 exec stream 之间 pod/container 消失 → exec API 返回 404/错误,实现必须**二次捕获**映射 `pod_not_found`/`container_not_found`(对称 docker.py 在 `exec_run` 阶段二次捕获 NotFound),不可只在读 pod 阶段处理。
- **[Running pod 内 container 非 running 态]**(review F11) pod phase==Running 但目标 container 在 CrashLoopBackOff/not-ready → exec 失败,映射 `container_not_running`(新增 kind,区别于「container 名不在 pod spec」的 `container_not_found`);spec §故障分类补此 kind+场景。
- **[RBAC 权限]** ServiceAccount 缺 `pods/exec` 权限 → `k8s_unavailable`(403);docs 说明所需 RBAC(`get pods` + `create pods/exec`)。
- **[超时后 websocket 释放不可证]** async wait_for 取消后底层 ws stream 关闭时机测试内不可证;集成只断言超时**返回值**,不声称验证释放(避免伪验收)。
- **[k8s target inert 精确范围]**(review F5/gp#5) KubernetesTarget 落地后**仅对 inspector 巡检路径 inert**(双 gate:manifest `targets` Literal 不含 k8s + runner preflight `requires_unmet`);但对 **doctor echo 探针 / `hostlens target test <name>`** 是 **live**(会真连 cluster,与 docker 一致)。非「全系统 inert」。

## Migration Plan

- **无破坏性**:纯加法(新 target 类型 + 新 optional extra)。现存配置不受影响;未装 `[k8s]` extra 的环境正常(模块可 import,用到才 raise `k8s_sdk_unavailable`)。
- **回滚**:revert PR(删 kubernetes.py + K8sEntry + registry 分支 + extra),无状态残留。
- 部署:feature branch `feat/add-kubernetes-target` → PR → CI 绿 → squash merge → 归档。

## Open Questions

- **read_file tar 依赖是否可接受为长期方案?** 倾向是(exec 是主采集路径,read_file 少用;tar 几乎所有非 distroless 镜像都有)。若未来需 distroless read_file,可加 `cat`+size-guard 回退路径,作独立提案,不在本范围。
- **exec stdin env 注入 vs 未来 ephemeral debug container?** 本提案只对已存在 pod 的已存在 container exec;ephemeral container(`kubectl debug`)注入是独立能力,非目标。
- **~~doctor 的 k8s 连通性检查深度?~~**(已由 Decision 8 收敛)不特判,走既有通用 echo 探针(对称 docker),不写自定义 version 探测。
