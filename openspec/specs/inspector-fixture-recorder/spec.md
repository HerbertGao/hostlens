# inspector-fixture-recorder 规范

## 目的

定义 Inspector fixture 录制器契约——录制器对真实 target 执行完整采集序列并产出 ReplayTarget 兼容 fixture、产物必须脱敏 secret 且冻结非确定性采样、录制器是开发期工具禁止进入 Agent 能力面。

## 需求
### 需求:录制器必须对真实 target 执行完整采集序列并产出 ReplayTarget 兼容 fixture

fixture 录制器**必须**接受一个 inspector manifest 与一个真实 `ExecutionTarget`，渲染并执行该 inspector 在 runner 中的**完整采集序列**——即全部 preflight 探测命令（`requires_binaries` / `requires_files` 探测）**加**主 `collect.command`——并把每条命令的 stdout/stderr/退出码记录成 `ReplayTarget` 能逐字节匹配回放的 JSON fixture。fixture **必须**携带 `ReplayTarget` 所需的 capability 声明，使回放经过与真实运行一致的 preflight。（本期 3 个 spike inspector 仅声明 `requires_binaries`；`requires_files` 探测的录制由录制器对合成 manifest 的单测覆盖，不要求由某个 spike inspector 验证。）

#### 场景:录制含 preflight 探测

- **当** 对一个声明了 `requires_binaries: [psql]` 的 inspector 录制 fixture
- **那么** 产出的 fixture **必须**包含该 binary 探测命令的记录 + 主命令的记录；**禁止**只录主命令（否则回放时 preflight 找不到对应记录而失败）

#### 场景:回放与录制一致

- **当** 用录制好的 fixture 经 `ReplayTarget` 跑同一个 inspector
- **那么** runner 实际发送的每条命令**必须**与 fixture 中记录的命令字节级匹配并命中回放；命令漂移**必须**导致回放失败（不得静默放过）

### 需求:录制产物必须脱敏 secret 且冻结非确定性采样

secret 经 runner 的 `env=secrets_env` 注入、**不进** recorded 命令字符串，且 `ReplayTarget` 的命令匹配不含 env、fixture 不存 env——故真正的泄漏面是命令的 **stdout/stderr 回显**。录制器**必须**在写盘前对 fixture 的 stdout/stderr 做 secret 脱敏（一旦 secret 被命令回显，bot token / 密码 / webhook 等不得明文落入 fixture），并**必须**冻结非确定性输出（时间戳类、`sampling_window` 双采样 delta 等），使 snapshot 测试可重复、不因录制时刻而漂移。

#### 场景:回显的 secret 不落 fixture

- **当** 录制一个 inspector，其命令把经 `secrets_env` 注入的密码回显进 stdout/stderr
- **那么** 产出 fixture 的 stdout/stderr 中**禁止**出现明文密码 / token；相关片段**必须**被打码或剔除（fixture 本就不含 env，故无需脱敏命令字符串）

#### 场景:非确定性输出冻结可重复

- **当** 录制一个输出含非确定量的 inspector（如 SQL `now()` 派生列、docker `State.StartedAt` 时间戳）
- **那么** fixture **必须**冻结这些非确定值，使回放产出确定的 `InspectorResult`；snapshot **禁止**因录制时刻不同而变化。（`sampling_window` 类双采样 delta 的冻结——本期 3 个 inspector 不涉及——由录制器对合成 / 既有 manifest（如 `log.tail.error_burst`）的单测覆盖，非由 spike inspector 验证）

### 需求:录制器是开发期工具，禁止进入 Agent 能力面

录制器**必须**是开发期工具 / CLI，**禁止**注册为 Agent ToolSpec、**禁止**进入 Tool Registry 或塞入 `ToolContext`（对齐 CLAUDE.md §4.10：它不是 Agent 主动调用的能力，是为作者生成测试夹具的本地工具）。

#### 场景:录制器不暴露给 Agent

- **当** 装配默认 Tool Registry / Agent 工具集
- **那么** 录制器**禁止**出现在其中；Agent 可见工具集不因录制器存在而改变
