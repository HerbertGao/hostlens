# kubernetes-execution-target 规范增量

## ADDED Requirements

### 需求:`KubernetesTarget` 必须基于 kubernetes-asyncio 实现且只对 Running pod 做只读操作

`hostlens.targets.kubernetes.KubernetesTarget` 必须：

- `type == "k8s"`（class-level 常量 / 只读属性，**不是**构造器参数——构造器签名 `__init__(name: str)` 与 LocalTarget/SSHTarget/DockerTarget 一致；pod/namespace/container 引用从 `_entry: TargetEntry` 拿，由 `TargetRegistry.register` 注入）。**禁止**把 type 写成 `"kubernetes"`——`execution-target` spec 锁定为 `"k8s"`。
- 结构化满足 `ExecutionTarget` Protocol（恰好 `name` / `type` / `capabilities` 属性 + `exec` / `read_file` 异步方法）
- 基于官方 `kubernetes-asyncio`（**原生 async SDK**）：所有 API 调用直接 `await`，**禁止**用 `asyncio.to_thread` 包裹（与同步的 docker-py 的关键不对称——多包一层 to_thread 是反模式）
- 持有 **per-target 两个 client**（**必须区分——实测 SDK 约束**）：① 常规 `ApiClient`→`CoreV1Api` 用于**读 pod 状态**（`read_namespaced_pod`）；② `WsApiClient`→`CoreV1Api` 用于 **exec / read_file 的 websocket**（`connect_get_namespaced_pod_exec(_preload_content=False)` 仅在 `WsApiClient` 下返回 websocket；普通 `ApiClient` 做普通 HTTP 写不了 stdin，而 `WsApiClient` 又会对 GET 试 ws 升级而坏读 pod）。两者首次需要时按 `_entry`（同一 Configuration，见认证）各构造一次并缓存、后续复用；**禁止**每次调用重建。两 client 生命周期由 target 持有，进程退出时各自 best-effort `close()`
- 只对**已存在且 phase == "Running"** 的 pod 内指定 container 做**只读** `exec` / `read_file`；**禁止**任何 pod 生命周期操作（create / delete / patch / scale / cp-write）
- `name` 必须匹配正则 `^[a-z][a-z0-9_\-]{0,63}$`，构造器内（赋值 `self.name` 前 `re.fullmatch`，不匹配 raise `TargetError(kind="invalid_target_name", target=name)`）enforce，与其它 target 同一道防线
- **container 选择 + running 态判定（proactive，经读 pod 而非 reactive 字符串匹配）**：实现经常规 `ApiClient` 的 `read_namespaced_pod(pod, namespace)` 拿 pod 对象后**主动判定**（**不**靠 exec 失败的错误文案区分——spec 禁 locale 脆弱的 string 匹配）：**先确定目标容器名**:`_entry.container` 非 None → 用它;为 None → 解析为**默认容器** = `spec.containers[0].name`(k8s exec `container=None` 即第一个容器,proactive 判定须把它解析成具体名以查 status)。然后对该名:不在 `spec.containers[].name` 内(仅具名情形可能)→ `TargetError(kind="container_not_found")`(含可用容器名);该名 running 态未就绪 → `TargetError(kind="container_not_running")`。**默认容器与具名容器走同一 None-safe running 判定**(都按解析出的名查 `status.container_statuses`)。**running 态判定必须 None-safe**(实测 `V1PodStatus.container_statuses` 默认 `None`——pod 刚转 Running 但 kubelet 尚未写 status 的竞态窗口):`status.container_statuses is None`、或该名不在 `container_statuses` 列表、或匹配项 `state.running is None` → 一律 `container_not_running`(**禁止**裸迭代 `container_statuses[]`,会 `TypeError`)。即 `container_not_found`(看 `spec.containers`)与 `container_not_running`(看 `status.container_statuses[].state.running`,含 None)取自 pod 对象**不同子树**,判据机械、不依赖 exec 错误归类。**init / ephemeral container 非目标**:它们不在 `spec.containers` / `status.container_statuses`(分属 `spec.init_containers` / `status.ephemeral_container_statuses`),指定其名按 `container_not_found` 处理(本提案只 exec 普通容器)
- `capabilities` 初始值 `{Capability.SHELL, Capability.FILE_READ}`；首次 `exec` 成功后 lazy probe 一次并缓存到 `_probed_caps`：`command -v systemctl` 成功加 `SYSTEMD`、`command -v docker` 成功加 `DOCKER_CLI`。**probe 必须用 POSIX `command -v`、不用 `which`**（pod 大量面对 distroless / busybox 极简镜像，`which` 非 POSIX 不可靠）。`__init__` 内**禁止**做任何 probe。probe 自身失败时 `_probed_caps` 设为已成功探到的子集、标记已探不 re-probe，**不影响**触发本次 probe 的那次 `exec` 返回值（旁路增强，对称 DockerTarget）
- **disabled gate**（继承 `execution-target` spec disabled 约定）：`exec` / `read_file` 必须在**任何 k8s API 调用之前**（构造 client / 读 pod / exec stream 之前）检查 `self._entry.enabled`；`enabled is False` raise `TargetError(kind="target_disabled", target=self.name)`，**不构造 client、不连 API server**
- **`_entry` 缺失防线**：pod 引用从 `self._entry` 拿；`exec` / `read_file` 在 `_entry is None`（未经 registry 注入）时被调用必须 raise `TargetError(kind="k8s_no_entry", target=self.name)`，**不**崩出裸 `TypeError`（对称 `docker_no_entry` / `ssh_no_entry`）
- **两道入口防线顺序固定**：`exec` / `read_file` 顶部**先**检查 `_entry is None`（→ `k8s_no_entry`），**再**检查 `_entry.enabled`（→ `target_disabled`）；`_entry is None` 时直接 raise，不触碰 `.enabled`

#### 场景:KubernetesTarget type 为 k8s

- **当** 实例化 `KubernetesTarget(name="x")` 并检查 `target.type`
- **那么** `target.type` 必须为 `"k8s"`（类常量，不接受 `type` kwarg，不为 `"kubernetes"`）

#### 场景:非法 name 构造 raise invalid_target_name

- **当** 实例化 `KubernetesTarget(name="Prod-Pod")`（含大写）或 `KubernetesTarget(name="1pod")`（数字开头）
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_target_name"`

#### 场景:disabled k8s target exec 不触发 API server

- **当** k8s target 的 `_entry.enabled is False`；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"target_disabled"`；且**未构造任何 client**（ApiClient 与 WsApiClient 都不构造）、**未**连 API server

#### 场景:standalone 构造（无 _entry）exec raise k8s_no_entry

- **当** `KubernetesTarget(name="x")` 未经 registry 注入 `_entry`（`_entry is None`）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"k8s_no_entry"`（不崩裸 `TypeError`/`AttributeError`）；不构造任何 client

#### 场景:KubernetesTarget 复用两个 client（单元测试，允许 mock 计数）

- **当** 在单元测试 mock 认证加载 + 两 client 工厂，对同一 `KubernetesTarget` 实例连续 `await target.exec(...)` 3 次
- **那么** 认证加载与**每个** client 工厂（`ApiClient` / `WsApiClient`）各**只被调用 1 次**（后续复用）；3 次都正常返回 ExecResult；且断言 exec 走 `WsApiClient`、读 pod 走常规 `ApiClient`（两 client 各司其职）

#### 场景:KubernetesTarget capabilities 首次 exec 后才探测

- **当** 构造后、首次 `exec` 前检查 `target.capabilities`
- **那么** 必须仅含 `{SHELL, FILE_READ}`；首次 `exec` 成功后才反映探测结果

#### 场景:指定 container 不在 pod 内 raise container_not_found

- **当** `_entry.container` 指向 pod spec 内不存在的容器名；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"container_not_found"`，含 pod 内可用容器名

### 需求:`KubernetesTarget.exec` 必须经 stdin 注入 env，禁止 argv 拼接，且区分 timed_out 与 exit_code

K8s pod exec API（`connect_get_namespaced_pod_exec`）**无 `environment=` 参数**（与 docker exec_run 的关键不对称）。`KubernetesTarget.exec(cmd, *, timeout, env=None)` 必须：

- **必须经 `WsApiClient` 走裸 websocket（kubernetes-asyncio 无同步 client 的 `stream()` helper）**：exec 用的 `CoreV1Api` 必须以 `CoreV1Api(api_client=WsApiClient(configuration))` 构造（普通 `ApiClient` 下 `connect_get_namespaced_pod_exec` 做普通 HTTP、写不了 stdin）。调 `connect_get_namespaced_pod_exec(..., command=["/bin/sh"], stdin=True, stdout=True, stderr=True, tty=False, container=<_entry.container 或 None>, _preload_content=False)`——返回的是 **`_WSRequestContextManager`(非裸 response)**,须 `async with (await connect_...(...)) as ws:` 进入才拿到 `aiohttp.ClientWebSocketResponse`(`ws`),**不能**直接对返回值 `.data` 索引。**禁止**写 `ws_client.exec` / 假设「可写 stdin handle」高层 helper（同步 client 形态、本 SDK 不存在）。手工 k8s exec channel 协议：**出站 stdin 帧** = `b"\x00" + payload`（channel 0），经 `ws.send_bytes(...)`;**入站 demux**:每条 message 取 `msg.data`,**先判 `len(msg.data) < 1` 跳过**(空帧——防 `b""[0]` 抛 `IndexError`;这是有意阈值,**仅**为消除 IndexError,不声称与 SDK `if len(msg)>1` 严格等价:len==1 的「仅 channel 字节、空 payload」帧会被处理为该 channel 的空数据/空 Status,由下游 try/except→None 安全吸收),再用首字节 `msg.data[0]`:`1`=stdout、`2`=stderr、`3`=error(aiohttp `async for` 自动处理 PING/PONG、CLOSE 终止迭代,故循环内只见数据帧,无需额外判 `msg.type`——与 SDK 同口径)
- **env 经 stdin 喂 shell 注入，禁止进 argv / 命令串**：把 `env` 渲染为 `export <K>=<shell-quoted-V>` 行序列 + 用户 `cmd` + **末尾 `\nexit $?\n`**（使 `/bin/sh` 读到 `exit` 即以 cmd 状态确定性终止——v4 协议无 stdin half-close、`ws.close()` 后读不到 channel-3,故必须靠脚本内 `exit` 让 sh 退出,**不**靠 stdin EOF/超时;sh 退出后 server 发 channel-3,client 在 `async for msg in ws` 里读到、server 关连接循环自然结束），整体经 **stdin（channel 0）** 写入 `/bin/sh`（如 `export MY_VAR='x'\n<cmd>\nexit $?\n`）。**禁止**用 `command=["env","K=V","sh","-c",cmd]` 或 `command=["/bin/sh","-c","export K=V; "+cmd]`——前者 env 进 `env` 进程 argv、后者 export 进 sh argv，都会出现在 pod 内 `ps`/process list，与 docs/ARCHITECTURE.md §4 命令渲染安全规则冲突。stdin 喂入的 export 由 shell 解释、**不**出现在任何进程 argv
  - **env value 转义**：经 shell 单引号转义（`'` → `'\''`）防注入（含换行 / `$()` / 反引号在单引号内不展开）
  - **env key 校验**（与 docker 不对称——docker `environment=`dict 不经 shell，k8s 经 shell `export` 故 key 必须校验）：每个 env key **必须**匹配 `^[A-Za-z_][A-Za-z0-9_]*$`，否则 raise `TargetError(kind="invalid_env_key", target=self.name)`——否则 `{"; rm -rf /":"x"}` 渲染成 `export ; rm -rf /=...` 构成注入。env key 实际来自受控 inspector 参数，此校验是 defense-in-depth
- **stdin 脚本先发后收**：stdin 内容为 `export...+cmd+exit $?`,本提案约束为 KB 级(env value 来自受控 inspector 参数;**不**对大 stdin 输入做保证)。该约束内「先发完 stdin、再 `async for` 收 stdout/stderr/channel-3」不死锁。**界未在加载期强制**,故超界(如注入数十 KB PEM 撑爆 pipe buffer 致 send 阻塞)的兜底是 §超时 `asyncio.wait_for` → `ExecResult(timed_out=True)`(而非真挂死)——即超界退化为「误报超时」而非死锁;若未来要支持大 stdin,须改并发收发
- `cmd` 是 shell-evaluated（经 stdin 交给 `/bin/sh` 解释，支持管道 / 重定向 / `$VAR` 展开）。**已知限制（与 docker/ssh 不对称，必须声明）**：因 cmd 本身经 stdin 喂入并随后 EOF，**pod 内 cmd 不能再从 stdin 读外部输入**（stdin 已被 export+cmd 脚本占用）；DockerTarget(`environment=`) / LocalTarget / SSHTarget 的 cmd stdin 空闲，k8s **不**等价。inspector collector 几乎不读 stdin，影响面小，但本契约**不**声称「k8s exec 的 stdin 语义与 local/ssh 一致」
- **exit code 经 error channel（channel 3）的 `v1.Status` 获取**：channel-3 payload 是 `v1.Status` JSON，经 `WsApiClient.parse_error_data`（**classmethod**，非 `ws_client` 模块级函数）取——`status == "Success"`→ exit 0，否则**实测 SDK 盲取 `details.causes[0]["message"]` 转 int**（注意:SDK **不**按 `reason == "ExitCode"` 过滤,直接取第一个 cause 的 message;**禁止**实现者照「过滤 reason」自造分叉解析,与 SDK 行为对齐即可）。`parse_error_data` 在 Status 缺 causes/message 时**会 raise**（`KeyError`/`ValueError`/`IndexError`/`JSONDecodeError`），故调用**必须 try/except 包裹**——**无 channel-3（ws 异常断）/ 解析 raise 且非超时** 时填 `ExecResult(exit_code=None, timed_out=False, ...)`（ExecResult 契约允许「拿不到 exit code 但未超时」），**禁止**用 `0` / `-1` 魔数顶替
- `stdout` / `stderr` 各自 UTF-8 解码（非 UTF-8 用 `errors="replace"`）；`duration_seconds` 记实际耗时
- **超时语义**：用外层 `asyncio.wait_for(<exec coroutine>, timeout)`；超时返回 `ExecResult(timed_out=True, exit_code=None, ...)`（满足 ExecResult 不变量 `timed_out ⇒ exit_code is None`）；超时后 best-effort 关闭 websocket，残留进程由 kubelet 回收，hostlens 不做进程组 kill（对称 SSH/docker）
- **pod 内无 `/bin/sh`（distroless）**：exec stream 报 OCI runtime 错（command not found / no such file）→ raise `TargetError(kind="exec_failed", target=self.name)`（**不**误归 `k8s_unavailable`——API/pod 正常，仅命令无法启动）
- **exec 阶段 TOCTOU**：读 pod（确认 Running）与建 exec stream 之间 pod/container 可能消失——exec API 此时返回 404/错误，实现**必须二次捕获**映射 `pod_not_found` / `container_not_found`（对称 DockerTarget 在 `exec_run` 阶段二次捕获 `NotFound`），**不**只在读 pod 阶段处理
- **Running pod 内 container 非 running 态**：判定走 **proactive 读 pod**（构造需求 §container 选择 + running 态判定：读 `status.container_statuses[].state.running is None`），**在发起 exec 前** raise `TargetError(kind="container_not_running", target=self.name)`——**不**靠 exec 失败的错误文案 reactive 区分（错误文案 locale 脆弱、与多种 exec 错误同形不可判）；**区别于** `container_not_found`「container 名不在 `spec.containers`」——此处 container 存在但未在运行
- 仅在 transport 级失败（API 不可达 / pod 不存在 / pod 非 Running / container 不存在或非 running）raise `TargetError`；命令非零退出 / signal-killed 是**正常** ExecResult，**不** raise

#### 场景:exec 经 stdin 注入 env 且不在 argv 泄露

- **当** 调用 `await target.exec("echo $MY_VAR", timeout=5, env={"MY_VAR": "x"})`
- **那么** 实现传给 exec API 的 `command` 必须是 `["/bin/sh"]`（不含 `MY_VAR` / `x` / `export`），env 经 stdin 以 `export MY_VAR='x'` 形式喂入；stdout 含 `"x"`

#### 场景:exec secret 不出现在 command argv

- **当** 调用 `await target.exec("ps auxw", timeout=5, env={"SECRET_TOKEN": "abc"})`
- **那么** 传给 exec API 的 `command` 必须严格为 `["/bin/sh"]`（不含 `"SECRET_TOKEN"` / `"abc"` / `"ps auxw"` 子串——cmd 与 env 都经 stdin）；secret 仅经 stdin 传

#### 场景:exec 非零退出返回 ExecResult 不 raise

- **当** 调用 `await target.exec("exit 3", timeout=5)`
- **那么** 必须返回 `ExecResult(exit_code=3, timed_out=False, ...)`（从 exec error channel 的 Status 解析），**不** raise

#### 场景:exec 超时返回 timed_out 且 exit_code 为 None

- **当** 调用 `await target.exec("sleep 60", timeout=2)` 且 pod Running
- **那么** 必须在 ~2s 后返回 `ExecResult(timed_out=True, exit_code=None)`；不抛异常

#### 场景:pod 内无 /bin/sh raise exec_failed

- **当** 目标是无 `/bin/sh` 的 distroless 容器（pod Running 正常）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"exec_failed"`（**不**是 `k8s_unavailable`）

#### 场景:exec 非法 env key raise invalid_env_key

- **当** 调用 `await target.exec("echo hi", timeout=5, env={"; rm -rf /": "x"})`（env key 非合法 shell 标识符）
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_env_key"`，**不**渲染出 `export ; rm -rf /=...` 注入；合法 key（如 `MY_VAR`）正常通过

#### 场景:exec 阶段 pod 消失 raise pod_not_found（TOCTOU）

- **当** 读 pod 时 pod Running，但建 exec stream 时 pod 已被删除（exec API 返回 404）
- **那么** 必须 raise `TargetError`，kind 为 `"pod_not_found"`（exec 阶段二次捕获，**不**崩出裸 `ApiException`）

#### 场景:Running pod 内 container 非 running 态 raise container_not_running

- **当** pod phase==Running 但目标 container 在 CrashLoopBackOff / not-ready（container 名在 pod spec 内**存在**但未运行）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"container_not_running"`（**区别于** `container_not_found`——后者是 container 名不在 pod spec）

### 需求:`KubernetesTarget.read_file` 必须经 exec tar 读取，尊重 10MB 上限

K8s 无 docker `get_archive` 的等价物；`read_file(path)` 必须经 exec `tar` 实现（与 `kubectl cp` 同机制），并复用与 DockerTarget 一致的 tar 单文件 / not_a_file / 10MB 语义：

- **path 预校验（发请求前）**：只接受绝对路径（以 `/` 开头），相对路径 raise `TargetError(kind="invalid_path", target=self.name)`；含 NUL 字节 / 换行的路径同样 raise `invalid_path`，**不发起 exec**；绝对路径含 `..` 用 `posixpath.normpath` 折叠后再用（对称 DockerTarget read_file path 规则）
- **proactive 读 pod（对称 §exec，复用常规 `ApiClient`）**：read_file 在发起 tar exec 前**同样**经 `read_namespaced_pod` 做 §container 选择+running 态判定——pod 不存在→`pod_not_found`、phase!=Running→`pod_not_running`、container 名不在 spec→`container_not_found`、container 非 running 态→`container_not_running`（与 DockerTarget `read_file` 复用 `_resolve_container` 同语义）。即 read_file 也需两个 client（常规 ApiClient 读 pod + WsApiClient 跑 tar），**不**把 pod 消失/非运行静默暴露成 `exec_failed`/裸 `ApiException`
- 经 exec 跑 `tar cf - <normalized-path>`（`command=["tar","cf","-",<path>]`，stdout=True、stderr=True、stdin=False，经 **WsApiClient** ws），捕获 stdout 的 tar 字节流。**tar 退出码经与 §exec 同款的 channel-3 `v1.Status` 解析获取**（read_file 复用 exec 的 websocket channel demux + `parse_error_data`，**不**另造退出码获取路径；read_file 不发出站 stdin 帧）
- **解 tar 与 size 判断固定顺序**（对称 DockerTarget，消除 not_a_file 与 file_too_large 判定竞争）：
  1. 逐条迭代 tar 条目（单遍前向、不预缓存全量、忽略 PAX/global header）：必须解析为**恰好一个 regular file 条目**；首个非 regular file 条目（目录 / 符号链接 / FIFO / 设备）立即 raise `TargetError(kind="not_a_file", target=self.name, path=path)`；再遇第二个 regular file 同样 raise `not_a_file`（不跟随符号链接、不返回 link target 字节）
  2. 对该唯一 regular file 条目判 size：**超过 10 MB**（`size > 10*1024*1024`，恰好 10MB 放行，边界用 `>`）raise `TargetError(kind="file_too_large", target=self.name, path=path, size=size)`。**累计读取是无条件 backstop**：边读边累计、累计 `> 10MB` 立即中止 raise（无论 tar header 是否给 size，禁止先全读进内存）
- **文件不存在**：tar **非零退出**（channel-3 Status 取退出码）**且 stdout 无 tar 字节产出** → raise `FileNotFoundError`（标准库异常，不包装）。**退出码为主判据、stderr 子串为辅**：不同 tar 实现 + 非英文 locale 下错误文案不同（busybox `can't open '/x': No such file`、GNU `Cannot stat: No such file`、本地化语言各异），故**禁止**单靠英文 `"No such file"` 子串判定；以「tar 非零退出 + 无字节产出」为准。**与 exec_failed 区分**：tar 进程**能启动**（拿到了退出码、有 channel-3 Status）= 非 exec_failed；只有 tar 二进制不存在 / 无法启动才 exec_failed
- **pod 内无 `tar`**：exec 报 command not found（OCI runtime / `/bin/sh: tar: not found`）→ raise `TargetError(kind="exec_failed", target=self.name)`，错误消息提示「read_file on k8s target requires `tar` in the container」。**已知限制**（写入 docs）：distroless 无 tar 的 pod 不支持 read_file；`cat`+size-guard 回退路径是**独立 follow-up（非本提案）**——不在本提案就上，以保持与 DockerTarget 单文件 tar 逻辑统一

#### 场景:read_file 读小文件

- **当** pod 内 `/tmp/hello.txt` 内容为 `b"hello"`；调用 `await target.read_file("/tmp/hello.txt")`
- **那么** 必须返回 `b"hello"`（经 exec `tar` 解流得到）

#### 场景:read_file 超过 10MB raise

- **当** pod 内 `/tmp/big.bin` 为 11 MB；调用 `await target.read_file("/tmp/big.bin")`
- **那么** 必须 raise `TargetError`，kind 为 `"file_too_large"`，含 path 与 size；不返回任何字节

#### 场景:read_file 恰好 10MB 放行

- **当** pod 内 `/tmp/exact.bin` 恰好 `10*1024*1024` 字节；调用 `await target.read_file("/tmp/exact.bin")`
- **那么** 必须成功返回全部字节（边界用 `>`），**不** raise

#### 场景:read_file 路径指向目录或符号链接 raise not_a_file

- **当** path 指向 pod 内目录（如 `/etc`）或符号链接（tar typeflag 非 regular file）
- **那么** 必须 raise `TargetError`，kind 为 `"not_a_file"`，含 path

#### 场景:read_file 多条目超大归档优先报 not_a_file

- **当** path 指向 pod 内**目录**且该目录含一个 >10MB 的文件（tar 返回多条目，首条目即目录元条目 DIRTYPE，其中某 regular file >10MB）；调用 `await target.read_file(path)`
- **那么** 必须 raise `TargetError`，kind 为 `"not_a_file"`（文件性判定优先于 size——目录条目先于内含文件命中），**不**报 `"file_too_large"`

#### 场景:read_file 相对路径 raise invalid_path

- **当** 调用 `await target.read_file("tmp/x")`（不以 `/` 开头）
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_path"`；不发起 exec

#### 场景:read_file 路径含 NUL 字节 raise invalid_path

- **当** 调用 `await target.read_file("/tmp/x\x00.txt")`
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_path"`；不发起 exec

#### 场景:read_file 路径含换行 raise invalid_path

- **当** 调用 `await target.read_file("/tmp/x\n.txt")`（含换行符）
- **那么** 必须 raise `TargetError`，kind 为 `"invalid_path"`；不发起 exec

#### 场景:read_file 绝对路径含 .. 规范化后读取

- **当** 调用 `await target.read_file("/a/../b/c.txt")`，pod 内 `/b/c.txt` 存在
- **那么** 实现必须先用 `posixpath.normpath` 折叠为 `/b/c.txt` 再传 `tar`（容器 namespace 内操作、无宿主逃逸面），成功返回 `/b/c.txt` 内容；**不**用 `PurePosixPath`（它不折叠 `..`）

#### 场景:read_file 不存在 raise FileNotFoundError

- **当** 调用 `await target.read_file("/nonexistent")` pod 内无此文件（tar 非零退出 + 无字节产出，退出码为主判据、不靠英文 stderr 子串）
- **那么** 必须 raise `FileNotFoundError`（不是 `TargetError`）

#### 场景:read_file pod 内无 tar raise exec_failed

- **当** 目标是无 `tar` 的 distroless 容器；调用 `await target.read_file("/tmp/x")`
- **那么** 必须 raise `TargetError`，kind 为 `"exec_failed"`，消息提示需容器内有 `tar`

### 需求:`KubernetesTarget` 必须把 SDK / API / pod 层故障分类为 TargetError

`KubernetesTarget` 在以下故障必须 raise 带明确 `kind` 的 `TargetError`（只在 transport 边界 raise，不吞异常）：

- **kubernetes-asyncio 未安装**（未装 `[k8s]` extra）：构造或首次使用时 import 失败 → raise `TargetError(kind="k8s_sdk_unavailable", target=self.name)`，消息含 `pip install "hostlens[k8s]"`；裸 `ImportError` / `ModuleNotFoundError` **禁止**冒泡。模块顶层 import 必须容错（`try/except ImportError` 置标志位或方法内延迟 import），未装 `[k8s]` 的环境**仍能 import** `hostlens.targets.kubernetes`（用于 mypy / registry 分支注册）
- **kubeconfig 加载失败 / API server 不可达 / 认证失败 / RBAC 无 pods/exec 权限**：抛 kubernetes-asyncio 的 `ApiException`（401/403/连接类）或 config 加载异常 → raise `TargetError(kind="k8s_unavailable", target=self.name)`
- **目标 pod 不存在**：读 pod 抛 `ApiException(status=404)` → raise `TargetError(kind="pod_not_found", target=self.name)`
- **目标 pod 存在但非 Running**（`phase != "Running"`，含 Pending / Succeeded / Failed / Unknown）→ raise `TargetError(kind="pod_not_running", target=self.name)`，含当前 phase
- **指定 container 不在 pod spec 内** → raise `TargetError(kind="container_not_found", target=self.name)`（含可用容器名）
- **目标 container 存在于 pod spec 但非 running 态**（CrashLoopBackOff / not-ready，pod phase 可能仍 Running）→ raise `TargetError(kind="container_not_running", target=self.name)`（区别于 `container_not_found`）
- **exec 阶段 TOCTOU**：读 pod 后、建 exec stream 时 pod/container 消失（exec API 404）→ 二次捕获映射 `pod_not_found` / `container_not_found`（不崩裸 `ApiException`）
- **非法 env key**（env key 不匹配 `^[A-Za-z_][A-Za-z0-9_]*$`）→ raise `TargetError(kind="invalid_env_key", target=self.name)`（在发起 exec 前校验，防 shell export 注入）
- **pod 内命令无法启动**（无 `/bin/sh` / 无 `tar`）→ raise `TargetError(kind="exec_failed", target=self.name)`，**不**归 `k8s_unavailable`
- 异常包装前必须经既有 `scrub_exception_message`（来自 agent-tool-adapter spec）清洗：实现把 k8s 异常**显式提取为字符串**后再喂 scrub（取 `exc.reason` / `str(exc)`），脱敏偶然夹带的 bearer token / `*_KEY=` / home 路径 / IP（复用既有 `_SCRUB_PATTERNS`）

#### 场景:k8s SDK 未安装 raise 带安装提示

- **当** 环境未装 `kubernetes-asyncio`；构造 `KubernetesTarget` 并首次 `exec`
- **那么** 必须 raise `TargetError`，kind 为 `"k8s_sdk_unavailable"`，消息含 `pip install "hostlens[k8s]"`；**不** raise 裸 `ImportError`

#### 场景:pod 不存在 raise pod_not_found

- **当** `_entry.pod` 指向不存在的 pod；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"pod_not_found"`，含 target name

#### 场景:pod 非 Running raise pod_not_running

- **当** 目标 pod `phase == "Pending"`（或 Succeeded/Failed）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"pod_not_running"`，含当前 phase

#### 场景:API server 不可达 raise k8s_unavailable

- **当** API server 不可达（连接被拒）或认证失败（401/403）；调用 `await target.exec("echo hi", timeout=5)`
- **那么** 必须 raise `TargetError`，kind 为 `"k8s_unavailable"`（不是裸 `ApiException`）

#### 场景:transport 异常经 scrub 脱敏偶然夹带的凭据

- **当** k8s 异常消息里偶然夹带 bearer token（如 `Authorization: Bearer xxx`）或 home 路径（`/Users/alice/.kube/...`）；该异常被包装为 `TargetError`
- **那么** 最终 `TargetError.__str__` 与 structlog 输出中该 token / home 路径子串必须被 `scrub_exception_message` 脱敏；保留 target name + kind

### 需求:KubernetesTarget 集成测试必须用真实 cluster，无 cluster 时 skip

`tests/targets/test_k8s_integration.py` 必须：

- 用真实 k8s cluster 跑（如 `kind` / `minikube` 起的本地 cluster + 一个 `alpine` pod），**禁止** mock kubernetes-asyncio——不仅 `mock.patch`，也包括 `mocker.patch` / `monkeypatch.setattr` / `patch.object` 等任意把 k8s SDK 替换掉的写法（与 SSHTarget 真 sshd / DockerTarget 真 daemon 约定一致，CLAUDE.md §6）
- 用 `@pytest.mark.k8s_integration` 标记；会话 fixture 检测 cluster 是否可达（kubeconfig 有效 + API 通），不可达时 `pytest.skip("k8s cluster unavailable")`（CI 无 cluster 时整组 skip 不 fail）
- 覆盖：成功 exec / 非零 exit / 超时取消（断言 `timed_out=True`、`exit_code=None` 的**返回值**）/ env 经 stdin 注入 + secret 不进 argv（在 pod 内 `ps` 验证 secret 不出现）/ tar read_file 小文件 / read_file 恰好 10MB 放行 / read_file 超过 10MB raise / read_file 目录 raise not_a_file / read_file 相对路径 raise invalid_path / read_file 不存在 raise FileNotFoundError / pod 不存在 raise pod_not_found / pod 非 Running raise pod_not_running / 指定 container 不存在 raise container_not_found / capabilities lazy probe
- **不列入集成覆盖（有意排除，附理由）**：① client 复用单次构造——需 mock 计数，归单元测试；② 超时后底层 websocket/协程释放——`asyncio.wait_for` 取消后底层 stream 关闭时机在测试内不可证（async websocket 固有限制），集成只断言超时**返回值**，不声称验证释放（避免伪验收）
- K8sEntry 配置解析 + registry k8s 分支 + 故障分类（mock ApiException）的**单元测试**（`tests/targets/test_k8s_config.py` / `test_k8s_unit.py`）**不需要** cluster，必须在无 cluster 的 CI 上跑过

#### 场景:集成测试通过真实 cluster 跑 echo

- **当** 跑 `pytest tests/targets/test_k8s_integration.py::test_exec_echo`（cluster 可达）
- **那么** 必须在真实 pod 内跑 `echo hostlens-probe`，断言 `ExecResult.stdout` 含 `"hostlens-probe"`

#### 场景:无 cluster 时集成测试 skip 不 fail

- **当** CI 环境无 k8s cluster；跑 `pytest tests/targets/test_k8s_integration.py`
- **那么** 整组必须 `skip`（reason 含 `k8s cluster unavailable`），**不** fail / error

#### 场景:不允许 mock kubernetes-asyncio

- **当** 检查 `tests/targets/test_k8s_integration.py` 文件内容
- **那么** 必须不含对 k8s SDK 的任何 mock（grep「`patch` / `mocker.patch` / `monkeypatch.setattr` / `patch.object` 任一 + 同行 `kubernetes` 子串」宽匹配须无命中）

#### 场景:配置解析单元测试无需 cluster

- **当** 在无 cluster 的环境跑 `pytest tests/targets/test_k8s_config.py`
- **那么** 全部通过（K8sEntry 解析 + registry k8s 分支构造仅校验配置层 / 类型，不触发 API 连接）
