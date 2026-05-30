## ADDED Requirements

### 需求:`PlannerAgent.run` 透传 observer 到 AgentLoop

`PlannerAgent.run` 必须接受一个可选关键字参数 `observer`（`LoopObserver | None`，默认 `None`），并将其原样透传给内部 `AgentLoop.run(intent, observer=observer)`。`PlannerAgent` 禁止解释、过滤、包装或自行消费 `LoopEvent` —— observer 是调用方（CLI）与 loop 之间的直通通道，Planner 只装配与收敛，不介入事件流。

`observer=None`（默认）时，`PlannerAgent.run` 行为必须与引入 observer 前完全一致。

#### 场景:observer 透传给内部 loop
- **当** 以 `PlannerAgent.run(intent, observer=obs)` 调用，Agent 经多轮完成
- **那么** `obs` 必须收到由内部 `AgentLoop` 发出的完整事件序列（与直接对 loop 传同一 observer 等价），且 `PlannerResult` 收敛结果不受 observer 影响

#### 场景:默认无 observer 行为不变
- **当** 不传 observer 调用 `PlannerAgent.run(intent)`
- **那么** 行为与返回的 `PlannerResult` 必须与引入 observer 前一致，不发任何事件
