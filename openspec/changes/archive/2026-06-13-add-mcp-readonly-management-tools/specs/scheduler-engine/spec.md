## 修改需求

### 需求:runner 必须在 Report 持久化后派发 notify 并落地结果

当且仅当 job 体产出了 Report（`status in {ok, partial}`）时，runner 必须在 `ReportStore.save` 之后、构造终态 `Run` 之前，按 manifest 的 `notify` 路由把（已脱敏的）Report 发送到对应通道，并把每通道 `NotifyResult` 写入 `Run.notify_results`。无 Report 的状态（`failed_*` / `missed` / `skipped_due_to_running` / `budget_exhausted` / `daemon_stopped`）**禁止**派发 notify（无内容可推），`notify_results` 为 `[]`。（`budget_exhausted` 是 `RunStatus` enum 成员、在 job body 前裁定故列此；M4 runner 实际不构造它——pipeline 内 token 退化映射为 `partial`，与 proposal Failure Mode 1 正交不矛盾。）

runner 的触发入口（`trigger`）必须接受 keyword-only 参数 `dispatch_notify: bool = True`。默认 `True` 完整保留上述派发行为；daemon / `schedule run` / `schedule trigger` CLI 均**不传**该参数，行为零变更。当调用方显式传 `dispatch_notify=False`（如 `run_schedule_now` MCP 工具）时，runner 必须在产出并持久化 Report 后**跳过** notify 派发整段（连同 `only_if` 路由求值），`Run.notify_results` 必须为 `[]`，而 Run 的其余留痕（`status` / `report_id` / targets / inspectors / report_hash）必须与 `dispatch_notify=True` 路径一致。`dispatch_notify=False` **禁止**改变 `RunStatus` 裁定。

**参数穿透契约（实现完整性，非可选）**：实际的 notify 派发点不在 `trigger`，而在 job body 内部的报告映射阶段——调用链为 `trigger → _run_job → _finalize_outcome → _map_outcome`，由 `_map_outcome` 调 `_dispatch_notify`（即抑制的目标语句）。因此 `dispatch_notify` 这个 keyword-only 参数必须**逐层穿透** `_run_job` / `_finalize_outcome` / `_map_outcome` 直到 `_dispatch_notify` 调用点，且**每一层的默认值都必须为 `True`**。这一不变量直接决定 timer 路径零变更：定时触发注册的 job body 是 `_run_job`（**不经 `trigger`**），它以 `_run_job(name)` 形式被调用、不传 `dispatch_notify`，唯有每层默认 `True` 才保证 timer / daemon 行为字节不变。**禁止**只在 `trigger` 加该参数而不向下穿透（那样抑制语句永不被触及，`dispatch_notify=False` 形同失效）。

notify 阶段必须**失败隔离**：任何通道在**路由（`only_if` 求值）/ 渲染 / 发送**任一环节的异常**禁止**冒泡出 job 体（Report 已留痕），仅记为 `NotifyResult(status="failed", error=...)`；notify 整体不得改变已裁定的 `RunStatus`。隔离面**含 `only_if` 求值的任何运行期异常**（含但不限于 `TypeError` / `NameNotDefined` / `TimeoutError` / `simpleeval.InvalidExpression` 等，详见 notify-routing「`only_if` 运行期求值异常」需求），不限于渲染/发送。多通道发送必须并发执行（`asyncio.gather` + 并发上限，默认 4），通道间互不阻塞；`gather` 必须以「单通道异常不取消其它通道」的方式收集（如 `return_exceptions=True` 或每通道独立 try）。`channel_registry` 必须经 runner 构造器注入（与既有 `RunStore` / `ReportStore` / `backend_factory` 同列，无 module-level singleton），daemon / `schedule run` / `trigger` 共用同一装配。

`Run.notify_results` 持久化/反序列化沿用既有 RunStore 的 `model_validate_json` 路径。**已知可接受弱化（F15）**：`notify_results` 从 M4 的 `list[object]`（宽松）收紧为 `list[NotifyResult]`（严格）后，理论上新增「单条畸形 NotifyResult 记录拖累 `schedule status` 整表查询」的反序列化失败面——但 M5 起 `notify_results` 仅由本 runner 用强类型 `NotifyResult` 写入，正常运行不产畸形记录；单记录读取隔离继承 baseline RunStore 查询契约（本 delta 不收紧、不新增该保证），跨期 schema 演进的兼容性留后续里程碑，非本提案目标。

#### 场景:有 Report 的触发派发并记录每通道结果

- **当** job 体产出 `ok` Report，manifest 配两个通道（一个 `only_if` 真、一个假）
- **那么** `Run.notify_results` 必须含两条：真的记 `sent`（或 `failed`），假的记 `skipped`；`RunStatus` 仍为 `ok`

#### 场景:通道发送异常不改变 RunStatus 且不冒泡

- **当** 某通道 `send` 持续失败耗尽重试
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，job 体不抛异常，`Run.status` 维持 `ok`/`partial`

#### 场景:only_if 运行期求值异常不改变 RunStatus 且不冒泡

- **当** 某通道 `only_if` 运行期抛异常（如类型不匹配 / 拼错名 / 求值超时）
- **那么** 该通道记 `NotifyResult(status="failed", error=...)`，job 体不抛异常，`Run.status` 维持 `ok`/`partial`，其它通道照常派发

#### 场景:无 Report 状态不派发 notify

- **当** 触发结果为 `failed_api_unavailable`（无 Report）
- **那么** 必须不发生任何通道发送，`Run.notify_results == []`

#### 场景:dispatch_notify=False 抑制派发但仍持久化 Report 与留痕

- **当** 以 `trigger(name, dispatch_notify=False)` 触发一个**配了 notify 通道**且 job 体产出 `ok` Report 的 schedule
- **那么** Report 必须照常持久化、`Run.report_id` 非空、`Run.status` 为 `ok`，且 `Run.notify_results == []`；测试必须以 spy/mock 断言该通道的 `only_if` 求值与 `send` **调用计数均为 0**（仅凭 `notify_results == []` 不足以证明抑制真生效——空列表在「无通道配置」时也成立；故场景刻意要求**配了通道**且断言 send 从未被调，避免 vacuous 验收）
