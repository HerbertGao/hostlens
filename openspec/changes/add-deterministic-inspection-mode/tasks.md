## 1. manifest schema 与加载

- [ ] 1.1 `ScheduleManifest` 加 `mode: Literal["agent","deterministic"] = "agent"`（默认 agent、向后兼容）。
- [ ] 1.2 loader 的 target 基数校验按 mode:`agent` 恰好 1、`deterministic` ≥1;成员未注册仍 fail-loud。
- [ ] 1.3 测试:无 mode → 默认 agent;agent 多 target fail-loud;deterministic 多 target 加载;单 target 两 mode 均加载。

## 2. 内置健康默认集

- [ ] 2.1 定义 `DEFAULT_HEALTH_INSPECTORS`（覆盖 cpu / 内存 / 磁盘 / inode / 负载 / systemd / 日志 / 网络域，取现有 registry inspector name）。
- [ ] 2.2 测试:`DEFAULT_HEALTH_INSPECTORS` 成员全部存在于 inspector registry（防 curated 集漂移）。

## 2.5 report-data-model（Finding.target_name + 多 target 组装）

- [ ] 2.5.1 `Finding` 加 add-only 字段 `target_name: str | None = None`（`reporting/models.py`，`extra="forbid"` / `frozen` 不变;旧构造 / 旧 JSON 零改动可加载）。
- [ ] 2.5.2 `compute_finding_id` **保持不变**:`target_name` **不**纳入指纹（指纹恒为 `sha256(name\x00version\x00message)[:16]`）;加测试钉「同 name/version/message 异 target_name → 同 id」。
- [ ] 2.5.3 多 target（fleet）Report 组装路径:接受跨多 target 的 inspector_results,组**一份** Report,`Report.target_name`=确定性 fleet 标签（有序 target 名 join,满足 `min_length=1`）,`meta.target_id`=确定性 fleet id（有序 target_id + `schedule_name` 派生,避免不同 fleet 撞 store key）,每条 flatten 出的 finding 盖来源 `InspectorResult.target_name`;既有单 target `from_inspector_results` 行为不变。
- [ ] 2.5.4 测试:多 target 组装产一份 Report、findings 带来源 target_name、fleet target_id/标签确定性（同输入同输出、不同 fleet 不撞 key）、legacy 无 target_name 的 Finding dict 可加载。

## 3. 确定性采集路径

- [ ] 3.1 `run_deterministic_inspection`:逐 `target × inspector 集` 经 `InspectorRunner` 跑（复用 `run_inspector` 的解析 + capability 门;不满足记 `skipped`）;信号量限流;单项失败隔离;**采集阶段不注入 LLMBackend**（守 §4.2 / ADR-008）。
- [ ] 3.2 inspector 集解析:`deterministic` 无 `inspectors:` → 默认集;有 → 权威集（不叠加）。
- [ ] 3.3 deterministic 组装的 report status / severity 派生把 `requires_unmet`（capability 不匹配）排除出降级触发集:**不计入** severity 聚合、**不**降级为 `partial`（显式传 override status 或调用支持该语义的组装路径）;`timeout`(全 timeout) / `exception` / `target_unreachable` 仍按既有语义降级。
- [ ] 3.4 测试:固定集逐 target 跑不漫游（不跑集外 / targets 外）;capability 不满足记 skipped 不计 severity;`requires_unmet` 不降级（其余 ok → 报告 status=ok）;真失败（target_unreachable / exception）仍降级 partial;并发限流;单项失败隔离不崩批。

## 4. narrate-only 装配 + 多 target 报告

- [ ] 4.1 采集结果 → **多 target（fleet）组装路径**（见 2.5.3）组装**一份**多 target `Report`（findings 跨 target、每条带来源 `Finding.target_name`;`Report.target_name` 为确定性 fleet 标签、`meta.target_id` 为确定性 fleet id）。
- [ ] 4.2 narrate-only 装配路径（`tools/diagnostician_tools.py`，新函数或现有装配函数的新参数）:**只注册 `correlate_findings`（复用 `_build_correlate_findings_spec`）、禁注册 `request_more_inspection` / `list_inspectors` / `list_targets`**;既有全装配 `register_diagnostician_tools`（三件）不变;`LLMBackend` 注入 `AgentLoop`（非 ToolContext）。
- [ ] 4.3 测试:narrate-only 装配的注册表只含 `correlate_findings`（无 `request_more_inspection` / `list_inspectors` / `list_targets`）;全装配路径不受影响（仍三件）;多 target 聚合 severity;VCR cassette 回放 narrate LLM。

## 5. runner 路由 + RunStatus 映射

- [ ] 5.1 job body 按 `mode` 路由:`agent` → `run_diagnosis_pipeline`（零改动）;`deterministic` → `run_deterministic_inspection`。
- [ ] 5.2 共享 RunStatus 映射;`deterministic` 全无结果 → `Run(status=failed, error="deterministic inspection produced no inspector results")`,不产 `failed_api_unavailable`。
- [ ] 5.3 测试:agent 行为不变;deterministic 多 target Report 落 `Run(ok/partial)`;全无结果落 `failed`。

## 6. notify、文档、收尾

- [ ] 6.1 多 target 报告经既有 routing / notify 派发（`aggregate_severity` 全队聚合 + `only_if`）+ 测试。
- [ ] 6.2 docs schedule manifest:`mode` / 多 target / 默认健康集说明 + Demo Path（tizi 6 台 deterministic fleet）。
- [ ] 6.3 ts.mac-mini 收尾:`daily-health-fleet.yaml` 改 `mode: deterministic` + `targets: [全 6 台]`;`schedule trigger` 验证逐台确定性覆盖;再 `launchctl load` daemon 上线（**这是用户「先不上、做确定性模式」后的真正上线点**）。
- [ ] 6.4 `openspec-cn validate --strict` + temp 副本实测 archive（含 schedule-manifest / scheduler-engine 的 RENAME + MODIFY、report-data-model 的 Finding 需求 MODIFY rebuild、diagnostician-agent / report-data-model 新增需求合入主 spec 的校验，[[project_openspec_modified_rename_archive]]）+ feature branch + PR + CI 绿 + 对抗性 review;merge 后归档。
