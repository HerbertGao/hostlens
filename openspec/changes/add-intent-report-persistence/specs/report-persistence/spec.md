## 修改需求

### 需求:`hostlens inspect --persist` 必须把机械巡检报告落盘

`hostlens inspect <target> --persist` 必须在产出 `Report` 后调 `ReportStore.save(report)` 落盘，便于 `reports list/show/diff` 消费。（标题「机械」二字沿用原始 capability 的 verbatim 标识符以避免归档 MODIFIED 匹配失败；本提案把 body 拓宽到也覆盖 `--intent` Agent 路径。）两条路径都产出可持久化的 `Report`：
- `--inspector <name>`（机械路径）：经 `from_inspector_results`（本就是该路径产物）。
- `--intent <自然语言>`（Agent 路径）：经 `agent-report-assembly` 能力——编排层用 per-run collector 收集 loop 的 `InspectorResult`、`from_inspector_results` 组装忠实 `Report`（带 `meta`，含诊断师投影进 `Report.hypotheses` 的根因假设）。

`--persist` 默认关闭；写**本地** store 不改变远端状态，**不需** `--yes`/审批（非远端写操作）。落 orphan 时（`SaveResult.stored_as_orphan`）退出码非 0 提示但报告不丢。

> **范围说明**：`hostlens demo run` 仍走 Planner-only 路径、暂不接 Diagnostician/忠实 Report 组装（需重录 demo cassette + 改 `demo-cli-command` 契约），故 `demo run` 继续**不**接受 `--persist`（拆为独立 follow-up）。`--intent` 路径在本提案已可持久化（不再 fabricate；经真 `InspectorResult` 组装）。

#### 场景:--inspector --persist 后报告可被 reports list 看到

- **当** `hostlens inspect local-host --inspector hello.echo --persist` 跑两次（机械路径，确定性输出）
- **那么** `hostlens reports list local-host` 必须列出至少 2 条 run，且每条可被 `reports show <run_id>` 取出

#### 场景:--intent --persist 后报告可被 reports show 取回、hypotheses 字段被保留

- **当** `hostlens inspect <target> --intent "..." --persist` 成功跑通（collector 非空；该测试 fixture 的诊断师产出 ≥1 条根因假设）
- **那么** 报告必须入 `ReportStore`，`hostlens reports show <run_id>` 取回的 `Report` 必须带 `meta`（真实 inspectors_used / token_usage），且 `hypotheses` 字段被**原样保留**（该 fixture 下非空——诊断师根因假设已持久化）。注：诊断师可合法地不产假设（`hypotheses` 为空也是有效持久化报告，不强制非空——见 diagnostician-agent）

#### 场景:--intent 全非 ok inspector 仍持久化为降级报告

- **当** `hostlens inspect <target> --intent "..." --persist`，Planner 跑的 inspector 全部非 ok（如 target_unreachable），但 collector 非空（带真 status 的 InspectorResult）
- **那么** 必须组装 Report（`meta.status` 为 `partial`/降级）并持久化（no-result 仅指 collector 真空，不指「无成功 inspector」）

#### 场景:--intent collector 真空时不持久化

- **当** `hostlens inspect <target> --intent "..." --persist` 但 Planner `failed_api_unavailable`、collector 为空（零 InspectorResult）
- **那么** 禁止产 Report、禁止 persist（无可信 meta），走 no-result 路径（stderr 降级 + exit 2）

#### 场景:demo run 不接受 --persist

- **当** 对 `hostlens demo run <s>` 试用 `--persist`
- **那么** 命令必须不暴露该 flag（或显式拒绝并说明 demo 路径持久化属后续提案）——**禁止**在 demo 的 Planner-only 路径 fabricate `Report` 落盘
