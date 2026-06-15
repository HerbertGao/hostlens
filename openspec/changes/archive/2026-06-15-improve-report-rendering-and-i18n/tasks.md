> **范围**:本提案交付 ①模板重做 + ③中文根因叙述 + ②契约 & crosscheck 框架 + `linux.systemd.failed_units` 旗舰样板。全量 72 个 inspector 的 message 改写是**分阶段多 PR 长尾**(任务 2.2),**不必原子合入本提案**。多 target 分节渲染依赖提案 B(`report-data-model` MODIFY 提供 add-only `Finding.target_name`);B 未落时单 target 路径不受影响。**关键依赖（redaction 边界）**:notifier 渲染入口先 `redact_report_for_render` 再喂模板,而 `_redact.py:_redact_finding` 显式逐字段重构 Finding——多 target 分节 / 四元组去重要在脱敏拷贝上拿到真实 `target_name`,**依赖提案 B 在 `_redact_finding` 透传 `target_name`**（B 任务 2.5.5 + report-data-model 脱敏需求 MODIFY）。C 的多 target 快照测试**必须**经真实 `render()`（含 redact）或 `redact_report_for_render` 喂报告、**不得**直接喂未脱敏 raw report,否则 B 漏透传时 C 测试假绿而真实 notify 链假红。

## 1. 通知模板重做（telegram + lark，立竿见影）

- [x] 1.1 新 Jinja filters 注册进 telegram + lark env:`sev_label`(critical→严重)、`conf_label`(high→高)、`coverage`(从 `meta.inspectors_used` 算 `ok/total · skipped · failed`:`ok`→ok、`requires_unmet`→skipped、`timeout`/`target_unreachable`/`exception`→failed,不变量 `ok+skipped+failed==total`,`failed>0` 才渲染失败子句)、`fmt_time`、`dedup`(去重键 = `(target_name, inspector_name, message, severity)` **四元组全字段相等**,**不**只 inspector_name+message)、`sort_sev`(critical<warning<info)、`group_by_target`(按 `finding.target_name` 分组;单主机退化为无分节)。
- [x] 1.2 `telegram/report.md.j2` 重做:抬头(非 intent)/ 覆盖行 / 根因分析置顶(+`↳` suggested_actions)/ 发现(四元组去重+排序+来源)/ 健康态 / 多 target 按 `finding.target_name` 分节(依赖提案 B;单主机无分节)。
- [x] 1.3 `lark/report.card.j2` 同构重做(卡片形态;去重键 + 分组逻辑与 telegram 一致)。
- [x] 1.4 测试:两通道渲染快照覆盖场景(抬头非 intent、覆盖行、**覆盖行计入失败状态**:`ok+skipped+failed==total`、`timeout`/`target_unreachable`/`exception` 计入 failed 不漏计、`failed==0` 时省略失败子句、根因置顶、四元组去重、同 message 不同 severity 不去重、按 severity 排序、带来源、健康态、多 target 分节、单主机退化无分节、**去重×分节组合**:跨主机同 message 因 target_name 不同不合并 / 主机内四元组全等合并);多 target 用例**必须经真实 `render()`（含 redact）或 `redact_report_for_render` 喂报告**(验证脱敏边界透传 `target_name`,不喂 raw report 假绿);MarkdownV2 转义不回归。

## 2. finding message 具体化 + 中文契约

- [x] 2.1 crosscheck 测试(机审,**按已迁移 allowlist 范围**):对**已迁移 allowlist**(初始 = `{linux.systemd.failed_units}`,各域长尾 PR 改写完逐个加入)内 inspector 断言 (a) 无 `see .* for details` 类空指针、(b) 含中文、(c) **若** message 含 `{field}` 注入**则**注入字段须在 `output_schema` 声明存在(if-inject-then-declared 守卫,**不**强制每条必注入、**无**豁免标记);**禁**对 allowlist 外未迁移 inspector 施加这些断言(否则 71 个英文 message 让 crosscheck 一上线即全红)。另加**防漂移断言**:每个内置 inspector 恰在 allowlist 或 backlog 之一、二者并集 == 全部内置(新增未分类即失败)。
- [x] 2.2 systematic 改写 ~72 个 inspector 的 `message` 为「简短中文标签 + `{field}` 注入数据」:先 `linux/systemd_failed_units.yaml` 做样板——其 `failed` 是 array-of-objects、`{failed}` 会吐 repr,故 **collector 须额外 emit `failed_names`(join 的单元名串)+ 扩 `output_schema`(`failed_names: {type: string}` 并加入 `required`)**,message 写 `systemd 失败服务：{failed_names}`(**禁** `{failed}`);再按域(计算/内存/磁盘/网络/服务…)分批(可多 PR)。**凡注入数组/对象类字段的 message 都须配套 emit 干净 join 串字段。**(旗舰样板已落;其余 71 个长尾改写留后续多 PR)
- [x] 2.3 既有 service-inspector / fixture crosscheck 硬编码结构若含 message 断言,同步更新([[project_service_inspector_crosscheck_frozen_structures]])。(经核查 `tests/inspectors/` 下既有 crosscheck 无 `systemd_failed_units` 的 message 断言,旗舰改写不波及——`test_service_contract_crosscheck.py` 零 `.message` 断言、`test_incident_pack_manifests.py` 仅 load/register 断言;受影响的是 `tests/incidents/` snapshot + cassette 与 `tests/demo/` cassette,见 issues)

## 3. 中文根因叙述

- [x] 3.1 Diagnostician 系统提示加「`description` / `suggested_actions` 必须简体中文」约束(写进系统提示**常量**,保 byte-stable + prompt cache 命中)。
- [x] 3.2 测试:cassette 回放确认产出的 `description` / `suggested_actions` 为中文;`confidence` 仍枚举。

## 4. 文档与收尾

- [x] 4.1 docs:inspector-authoring `message` 规约(中文标签 + 注入数据 + 禁空指针)+ 报告渲染示例(本提案 prototype 的渲染)。(message 规约落 `docs/operations/inspector-authoring-contract.md` 规则 10 + 速查清单 + 参考;报告渲染新布局示例落 `docs/operations/notify.md` §报告渲染示例:抬头/覆盖行/根因置顶/四元组去重/多 target 分节/健康态,Telegram + Lark 同构,severity 图标 🔴/⚠️/ℹ️ 与代码一致)
- [x] 4.2 升级说明:message 改写改变 `compute_finding_id`(含 message)→ 升级后**首跑 regression diff 有一次性 `resolved` + `added`**(同一问题 id 重置),非真实变化,文档点明。(落 `docs/MIGRATION.md` §message 改写与 finding id 一次性 churn:现象 + 适用范围限定「仅 agent per-target diff,fleet-only 部署当前不受影响,未来给 fleet 加 diff 须同步修订免责」)
- [ ] 4.3 ts.mac-mini:模板 + message + 中文叙述生效后,重跑 `schedule trigger` 看真实新报告(替换本提案的 prototype)。
- [ ] 4.4 `openspec-cn validate --strict` + temp 副本实测 archive + feature branch `feat/improve-report-rendering-and-i18n` + PR + CI 绿 + 对抗性 review;merge 后归档。
