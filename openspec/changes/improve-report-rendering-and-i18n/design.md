## 上下文

报告渲染链:Report(`reporting/models.py`:Finding{severity/message/evidence/inspector_name/**target_name**}、RootCauseHypothesis{description/confidence/suggested_actions}、ReportMeta{inspectors_used/status/timestamp})→ `redact_report_for_render` → telegram `report.md.j2` / lark `report.card.j2`(Jinja，filters `mdv2_escape`/`sev_icon`)。**关键:`redact_report_for_render` 在渲染链中间**——`_redact.py:_redact_finding` 显式逐字段重构 Finding,本提案多 target 分节 / 四元组去重消费的是**脱敏拷贝**,故 `target_name` 能否到达模板**取决于提案 B 在 `_redact_finding` 透传 `target_name`**(B 任务 2.5.5)。B 漏透传 → 模板侧 `target_name` 全 None → 分节失效 + 跨主机误并;C 的多 target 快照测试须经真实 redact 路径以暴露此依赖、不喂 raw report 假绿。

现状:模板第 1 行 `report.intent` 当标题;逐条 `finding.message` 裸渲染(不去重);findings 的 message 是 inspector YAML 里**作者手写英文静态串、不注入数据**(FindingRule `message` 走 `.format`,但作者没用占位)。本提案的 prototype 已验证新布局可渲染(根因优先 + 去重 + 来源 + 中文 + 覆盖行)。

## 目标 / 非目标

**目标:** 见 proposal —— 模板重做 + finding 消息具体化 + 中文化。

**非目标:** 不引入 en/zh toggle;不改发送/签名/重试;不改 Finding/Report 模型结构;不改哪些 inspector 跑。

## 决策

1. **去重 / 排序在渲染层(Jinja filter),不动模型**。`Finding` 是 `frozen` 模型;dedup(键 = **四元组 `(target_name, inspector_name, message, severity)` 全字段相等才合并**,不能只 `inspector_name + message`——否则同 message 不同 severity / 不同 target 的独立发现会被误并)与 sort(severity rank `critical<warning<info`)做成 telegram + lark 共享的 Jinja filter,render-time 处理。新 filters 一并注册进两个 env:`sev_label`(critical→严重)、`conf_label`(high→高)、`coverage`(从 `meta.inspectors_used` 算 `ok/total·skipped`)、`fmt_time`、`dedup`、`sort_sev`、`group_by_target`。
2. **finding message = 简短中文标签 + `.format` 注入数据**。FindingRule `message`(既有走 `str.format`,非 Jinja,见 [[project_manifest_parameters_must_wrap_type_object]] 邻近约定)必须用 `{field}` 注入 collector 输出字段。契约:**必须中文标签** + **有可变数据的 finding 必须至少一个 `{field}` 注入** + **禁 `see X for details` 类空指针** + **被注入的 `{field}` 必须渲染成干净人读串**(数组 / 对象类输出 `str.format` 会吐 Python repr,故 collector 须**额外 emit 一个已 join 的串字段**)。例:`linux.systemd.failed_units` 的 `failed` 是 array-of-objects(`[{unit:...}]`),`{failed}` 会吐 `[{'unit':...}]` repr;故 collector 须 emit `failed_names`(join 的单元名串)、message 写 `"systemd 失败服务：{failed_names}"`,**禁** `{failed}`。
3. **i18n = zh-CN 硬编码、无 toggle**(YAGNI)。diagnostician 系统提示加一句「根因 `description` 与 `suggested_actions` 必须用简体中文」;inspector message 中文。语言设置 / message catalog 是**未来单独提案**——当前唯一场景中文,先把质量做对,过早抽象 i18n 是镀金。
4. **多 target 分节**。确定性模式产的多 target Report,模板按 `finding` 的 target 上下文**分主机节**渲染(每节:主机名 + 该主机 severity + 其 findings);单 target 退化为无分节(与 prototype 同)。依赖确定性模式(提案 B)的多 target 报告组装产出可分组的 findings;本提案模板侧先支持分组渲染,B 未落时单 target 路径不受影响。
5. **systematic 改写的防漂移验证（按已迁移 allowlist 范围，避免范围矛盾）**。加 inspector 契约 crosscheck 测试,**只对「已迁移 allowlist」**(初始 = `{linux.systemd.failed_units}`,各域长尾 PR 改写完逐个把 inspector 的 canonical `name` 加入)内 inspector 的 FindingRule message 断言 (a) 无 `see .* for details` 类空指针、(b) 含中文字符、(c) **若** message 含 `{field}` 注入**则**注入字段须在 `output_schema` 声明存在(if-inject-then-declared 守卫,**不**机械强制必注入、**无**豁免标记——「有可变数据必须注入」是 (a)–(e) 作者目标契约,机械判定模糊故留人审);**禁**对 allowlist 外未迁移 inspector 施加中文 / 注入断言(本提案只交付契约 + 框架 + 旗舰样板,若对全量施加中文断言,71 个英文 message 会让 crosscheck 一上线即全红、归档即失败)。另加**防漂移断言**:每个内置 inspector 恰在 allowlist 或 backlog 之一、二者并集 == 全部内置(新增未分类即失败)。把「质量规约」从人审变机审。

## 风险 / 权衡

- **finding id 一次性 churn**:`compute_finding_id` 含 `message`(`models.py:205`)。改 message → 同一问题的 finding id 变 → 升级后第一次 regression diff 会把旧 finding 报成 `resolved` + 新 `added` 一次。权衡:一次性 message 质量跃升值得这次升级时点的 diff 噪声;文档在 tasks 里说明「本次升级后首跑 diff 有一次性 id 重置」。
- **~72 inspector 体力活**:message 改写量大。分阶段:先模板(①,立竿见影)+ systematic 契约 + crosscheck(②框架),inspector message 逐域改(可多 PR)。
- **中文硬编码**:非中文用户读不了 —— 接受(当前场景);未来 toggle 单独提案。
- **`.format` 注入字段缺失 → KeyError**:message 引用的 `{field}` 必须在 output_schema 保证存在(required 或容错默认)。crosscheck (c) + 既有 collector 测试钉。
