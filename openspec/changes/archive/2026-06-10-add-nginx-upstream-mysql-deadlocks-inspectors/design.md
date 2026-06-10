## 上下文

`service-inspector-suite` 经多个 wave 增量交付,wave-2b 归档时把 nginx.upstream 与 mysql.deadlocks 这两个累积/时间窗口单元格显式推后(见 `add-replication-lag-inspectors` 等归档 change 的 wave-2b 冻结清单)。本 change 是 wave-2b 尾批,追加这两个 inspector。

约束(均来自既有契约,本设计不松动):
- 守 `service-inspector-contract`(三态 status / fail-loud / secret 前缀 / 输出键区分聚合与列表)。
- 守 `os-shell-inspector` 同族 collector 形态(`LC_ALL=C` 防 locale、collector 内坍缩成标量、Finding DSL 只阈值比较)。
- wave-2b「确定性录制」硬条款:窗口/持续态聚合**必须**在采样时刻于目标机内算成最终标量并冻结进输出,ReplayTarget 回放原样返回,**禁止**回吐需 `now()` 重聚合的带时间戳明细。
- 追加式:**不** MODIFY 旧 wave 需求/清单,新增一个 sibling ADDED 覆盖需求。

## 目标 / 非目标

**目标:**
- nginx.upstream / mysql.deadlocks 两个 manifest 干净注册(registry errors==[]),各附可证检出的 semantic-abnormal snapshot + ReplayTarget fixture。
- 两者进容器 cohort INCLUDE(28→30),docker⇔k8s 奇偶不变量 + 内容式 meta-guard 同步。
- `test_service_contract_crosscheck.py` 硬编码结构(计数 / _PROBE_TEST_SOURCES / _NO_EXCEPTION_SNAPSHOT)同步。

**非目标:**
- 不做 nginx-plus API、不做 mysql 累积死锁计数、不做死锁现场全文解析(见 proposal Non-Goals)。
- 不改 service-inspector-contract / os-shell-inspector spec。

## 决策

### D1:nginx.upstream 走 error.log 事件计数,不走 stub_status

**选 X**:单遍 awk 扫 `/var/log/nginx/error.log`,按字符串匹配分类 upstream 故障事件计数。
**弃 Y(stub_status)**:`stub_status` 只给 active/reading/writing/waiting 连接数,**无 per-upstream 健康**;nginx-plus 的 `/api` 才有 upstream 状态但是付费特性。OSS 场景 error.log 是唯一能拿 upstream 故障信号的源。
**与 nginx.error_rate 对齐**:同样静态路径 + `requires_files` 预检 + `LC_ALL=C` + END{} 零对象,降低认知负担、复用 D-4「日志轮转即窗口边界」。
**事件分类**:`total` 用一条合并正则避免重复计数(一行只可能命中一类 upstream 错误);分桶计数(timed_out/no_live/connect/premature)是诊断辅助,不参与 finding 阈值(finding 只看 `upstream_error_count`)。

### D2:mysql.deadlocks 在 collector 内把死锁时间戳坍缩成 age 标量

**选 X**:`SHOW ENGINE INNODB STATUS` → collector parse「LATEST DETECTED DEADLOCK」段首行时间戳 → **在目标机采样时刻**算 `deadlock_age_seconds = now - 死锁时间戳` → 冻结进输出。
**为什么不回吐时间戳明细**:wave-2b「确定性录制」硬条款禁止回放时按 `now()` 重聚合——若 collector 吐死锁时间戳字符串、由 ReplayTarget 回放时再算 age,则 age 随回放时刻漂移,snapshot 非确定。故 age 必须在采样时算死、冻结。
**输出形态(拍板,消除二义)**:恒发两键 `deadlock_detected`(bool)+ `deadlock_age_seconds`(int),`output_schema.required` **两键都进**;**无死锁时 collector END 恒发哨兵 `deadlock_age_seconds: -1`**(与 mysql.slow_queries 无慢查询恒发 `slow_query_count: 0` 同款),**禁止省略键**——runner 用 `jsonschema.validate(output, output_schema)`,required 含一个无死锁时会缺失的键会令校验失败→status=exception,与「无死锁→ok」直接矛盾。finding:`deadlock_detected and deadlock_age_seconds <= lookback_seconds`(`deadlock_detected==False` 时 `and` 短路,-1 哨兵不会误触)。
**时间戳来源(修订:只声明 ISO 形 + 段布局)**:INNODB STATUS 的「LATEST DETECTED DEADLOCK」标记**夹在 `------` 分隔线之间**,时间戳是标记**之后第一条 ISO 日期行**(标记的紧邻下一行是闭合分隔线 `------`,**不是**时间戳)。故 collector 不能 `getline` 一次就取——必须 flag-on-marker 后匹配 `^[0-9]{4}-` 跳过分隔线取首条 ISO 行(见 proposal collector 示例)。现代 MySQL 5.7/8.0 段头是 ISO 形 `YYYY-MM-DD HH:MM:SS`,GNU `date -d` 可直接解析。**不**声称兼容紧凑 `YYMMDD HH:MM:SS` 形——GNU `date -d` 无法把 `240102` 当 YY-MM-DD(会当异常 token 拒解析),原「兼容两式」是 overclaim。collector 只解析 ISO 形,非 ISO/解析失败 → fail-loud exit 1(不吐半成品,status=exception 不静默报健康);目标 MySQL/MariaDB 版本的实际段头格式由真机 Demo Path 验证(若目标用非 ISO 形,作为 follow-up 扩 parse,不在本期)。**D-7 风险点**:semantic-abnormal fixture 是作者编 stdout,若编成「标记紧邻下一行即时间戳」会掩盖此 off-by-one——故 fixture **必须**包含闭合分隔线行,且 awk getline 偏移由真机 Demo Path 验证(offline 不执行 awk)。
**secret**:`HOSTLENS_MYSQL_PWD` → `MYSQL_PWD`,与 mysql.slow_queries 逐字对齐(从不内联 argv)。

### D3:fixture 走捕获式 ReplayTarget,collector shell 正确性靠命令串级锁 + 真机 Demo Path

ReplayTarget fixture 由作者编写 collector stdout(死锁段文本 / error.log 行),snapshot 断言 finding 输出。**ReplayTarget fixture 本身不锁 collector shell 正确性**——回放不执行 awk/mysql。但 shell 逻辑(awk getline 偏移、date 解析、age 算法、合并正则计数)**已经过独立的可执行强锚验证**(把 collector 的 awk/date 片段对真实形态的 `SHOW ENGINE INNODB STATUS\G` 死锁段 + nginx error.log 样本在 shell 里直接跑,确定性确认:awk 跳过 `------` 分隔线取到 ISO 时间戳、GNU `date -d` 解析 ISO + age 算术正确、无死锁走 -1 哨兵、合并正则 total 唯一行计数排除无关行、空日志零对象;`date -d` 与套件既有 tls_cert_expiry/pods_stuck_pending/fs_logrotate 同为 GNU date 假设)。实现时把该可执行检查固化进 Demo Path(见 proposal)即为 collector-shell 类目的强对账锚,**不**依赖真机才能验证基本 shell 正确性;真机 Demo 仅补验目标 MySQL 版本的实际段头格式(ISO vs 罕见非 ISO)。这是 D-7 os-shell fixture 约定的延续 + 可执行强锚加固。

### D4:容器 cohort 归类

两者都 INCLUDE:nginx.upstream 读 `/var/log/nginx/error.log`(**service 本地日志**,非 host 全局如 /proc/sys/journalctl);mysql.deadlocks 走 mysql client **服务端** STATUS(不读 host)。按「看 collector 读取源不看域名」判据(inspector 容器安全分类 memory),二者均不读 host 全局源 → INCLUDE。计数 28→30,奇偶仍偶(+2),docker⇔k8s 对称不变量保持。

## 风险 / 权衡

- [nginx error_log 路径非默认(自定义 `error_log` 指令到别处)] → 静态路径扫到空/旧文件,误报 0。**缓解**:本期固定 `/var/log/nginx/error.log`(主流默认),路径参数化留 follow-up,Failure Modes 表 + Demo Path 文档显式提示。与 nginx.error_rate 同款取舍,一致性优先。
- [INNODB STATUS 时间戳格式跨 MySQL/MariaDB 版本差异] → date 解析失败。**缓解**:collector **只解析 ISO 形** `YYYY-MM-DD HH:MM:SS`(现代 MySQL 5.7/8.0 默认,与 D2 一致——**不**兼容紧凑 `YYMMDD`,那是 D2 判定的 overclaim);非 ISO/无法解析 → fail-loud exit 1 status=exception(不静默报健康);snapshot 覆盖 ISO 形,目标版本实际段头格式由真机 Demo 验证(非 ISO 形扩 parse 留 follow-up)。
- [死锁是低频事件,semantic-abnormal fixture 需真造死锁] → 录制成本。**缓解**:fixture 用作者编写的真实 INNODB STATUS 死锁段文本(从真机两事务交叉锁行采得),snapshot 断言默认阈值产 warning;符合 wave-2b「真造累积/持续异常而非低阈值凑」硬条款。
- [crosscheck 测试硬编码计数遗漏更新] → validate/archive 过但 pytest 红(service inspector crosscheck memory 的经典坑)。**缓解**:tasks 显式列出 crosscheck 三处结构 + cohort guard 上界,0-error pytest 跑全量兜 grep 盲点。

## Migration Plan

- 纯追加,无数据迁移、无 schema 破坏。两个 manifest 经既有 registry 自动加载。
- 回滚:删两个 yaml + 还原 crosscheck/cohort 测试断言即可,无状态残留。

## Open Questions

- nginx error_log 路径参数化是否值得本期做?**暂定不做**(与 error_rate 一致,降低 surface);若 reviewer 认为路径假设过强可在 tasks 阶段升级为参数(default 保持 `/var/log/nginx/error.log`)。
