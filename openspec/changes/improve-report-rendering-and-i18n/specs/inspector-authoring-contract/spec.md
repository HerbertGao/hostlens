## 新增需求

### 需求:FindingRule message 必须是简短中文标签 + 注入关键数据，禁空指针 / 禁纯英文长句

每个 FindingRule 的 `message` **必须**:

- **(a)** 用**简短中文标签**描述问题类别（如「systemd 失败服务」「磁盘使用率超阈值」「内存不足」）。
- **(b)** 对**有可变数据**的发现,经 `str.format` 用 `{field}` **注入 collector 输出的关键数据**(哪个单元 / 什么值 / 什么阈值);被注入的 `{field}` **必须**是 `output_schema` 保证存在的字段(`required` 或有容错默认),避免 `KeyError`。
- **(c)** 被 `{field}` 注入的字段**必须渲染成干净人读串**——`str.format` 对数组 / 对象类输出会吐 **Python repr**(如 array-of-objects `[{'unit': 'foo.service'}]`),严禁直接注入。**collector 应额外 emit 一个串或已 join 的字段**(如把 `failed: [{unit:...}]` 旁配一个 `failed_names: "foo.service, bar.service"`),message 注入那个干净串字段、而非 raw object 数组。
- **(d)** **禁止** `see X for details` 这类**不含实际数据的空指针**——发现必须自带具体指向,不必跳别处查。
- **(e)** **禁止**纯英文长句叙述。

目的:报告里每条发现**自带具体指向 + 中文**。叙述性的根因分析归 Diagnostician（中文,见 diagnostician-agent),finding message 只做「简短中文标签 + 数据」。

**(a)–(e) 是全部内置 inspector message 的最终目标契约,但 crosscheck 机审是分阶段启用的（避免范围矛盾）**:本提案只交付契约 + crosscheck 框架 + `linux.systemd.failed_units` 旗舰样板,全量 72 个 inspector 的 message 改写是**分阶段多 PR 长尾**。若 crosscheck 上来就对**全量**断言「含中文」,71 个未改写的英文 message 会让它**一上线即全红、本提案归档即失败**。故 crosscheck 的 message-质量断言（中文 / 空指针 / 注入）**只施加在「已迁移 allowlist」**(初始 = `{linux.systemd.failed_units}`,各域长尾 PR 改写完把成员逐个加入);allowlist 外的未迁移 inspector **暂不**受这些断言约束。**防漂移**由独立断言保证「每个内置 inspector 恰在 allowlist 或 backlog 之一、二者并集 == 全部内置」——新增 inspector 未分类即 crosscheck 失败,使「全量 vs 长尾」边界**显式可见**、不靠「碰巧没人加新 inspector」。

**契约记录(finding id churn)**:`message` 是 `compute_finding_id(inspector_name, inspector_version, message)` 的输入(severity 被刻意排除以支持 `changed_severity`)。因此**改写 message 会改变同一问题的 finding id**——批量重写 message 的那次升级,**首跑 regression diff 会把旧 id 一次性报成 `resolved`、新 id 报成 `added`**(同一真实问题 id 被重置,**非真实状态变化**)。这是 message 改写的**已知一次性副作用**,认可且记录在案;作者改写 message 时须知晓,运维侧首跑 diff 的这次 resolved+added 噪声应被解读为 id 重置而非问题消失 / 新增。**适用范围限定**:该 churn 叙述**仅适用 agent 模式的 per-target regression diff**;fleet（deterministic）report **不做** per-target regression diff（见提案 B `add-deterministic-inspection-mode` 的 report-data-model「fleet 无 per-target diff」非目标),故 message 改写在 fleet-only 部署中**不产生** diff 噪声——纯 fleet 部署可忽略本 churn 提示。

#### 场景:message 注入干净串而非空指针 / repr
- **当** 一个列出 failed 单元的 inspector(输出 `failed: [{unit:...}]` 数组,并旁配 collector emit 的已 join 串字段 `failed_names`),其 FindingRule 在 `len(failed) > 0` 时触发
- **那么** 其 `message` **必须**形如 `"systemd 失败服务：{failed_names}"`(注入 `"foo.service, bar.service"` 类干净人读串),**禁止**注入 `{failed}` raw 数组(`str.format` 会吐 `[{'unit': 'foo.service'}]` repr),**也禁止**形如 `"One or more systemd units are in the failed state (see failed for details)"`

#### 场景:契约由 crosscheck 机审防漂移(静态检查,有边界,按已迁移 allowlist 范围)
- **当** 遍历**已迁移 allowlist**(初始 = `{linux.systemd.failed_units}`,随各域长尾 PR 改写完逐个加入)内 inspector 的 FindingRule `message`
- **那么** **必须**有测试断言:(d) 无 `see .* for details` 类空指针 pattern、(a) 含中文字符、(c) **若** message 含 `{field}` 注入,**则**被注入的字段必须在 `output_schema` 声明存在(防 `KeyError`)——(c) 是 **if-inject-then-declared 守卫**,**不**强制每条 message 必须注入(「有可变数据的发现必须注入」是 (a)–(e) 的**作者目标契约**,但其机械判定需「output_schema 哪些字段算可变数据」这类模糊谓词,故 crosscheck **不**机械强制注入、留作者把关 + review;genuinely 无可变数据的纯标签 inspector 无需注入、**也不需要任何豁免标记**)
- **且** crosscheck **不得**对 allowlist 外(未迁移)的 inspector 施加上述中文 / 注入断言——否则 71 个未改写英文 message 会让 crosscheck 一上线即全红、本提案归档即失败
- **且** crosscheck 是**静态**检查(**不**实例化 collector、**不**跑命令),其能力**仅限**上述「空指针 pattern + 含中文 + 注入字段已声明(if-inject-then-declared)」三项;它**不能**验证被注入的 `{field}` 在运行时是否真实存在、也**不能**验证注入值渲染是否干净(无 repr 泄漏)、**不**机械强制「该注入的有没有注入」——那几项靠 collector 单测 + 真机 demo + 人审兜,本契约不得宣称 crosscheck 覆盖运行时正确性

#### 场景:防漂移——每个内置 inspector 必在 allowlist 或 backlog 之一(无静默逃逸)
- **当** crosscheck 跑时
- **那么** **必须**断言:每个内置 inspector 恰好属于「已迁移 allowlist」或「待迁移 backlog」之一,且二者**并集 == 全部内置 inspector**——新增 inspector 若未分类(既不在 allowlist 也不在 backlog)则 crosscheck **失败**,强制作者把它纳入其一,使「全量 vs 长尾」边界显式可见、不靠「碰巧没人加新 inspector」逃逸防漂移门

#### 场景:message 改写导致 finding id 一次性重置
- **当** 批量改写内置 inspector 的 FindingRule `message`(中文化 + 注入数据)并升级
- **那么** 升级后**首跑** regression diff **必须**被理解为:同一真实问题的 finding id 因 `message` 变更而重置——旧 id `resolved` + 新 id `added` 各一次,**非真实状态变化**;此一次性 churn 是 message 改写的已知且认可的副作用
