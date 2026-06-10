## 新增需求

### 需求:wave-2b 尾批必须覆盖 upstream 故障与 InnoDB 死锁单元格

wave-2b 尾批 cohort **必须**补齐归档时推后的两个累积/时间窗口 service inspector 单元格:**nginx.upstream**(Nginx upstream 故障)与 **mysql.deadlocks**(InnoDB 死锁)。这两格的 semantic-abnormal 异常态**不能**经有界、确定性 setup 即时采到,**必须**依赖时间窗口内累积的真实事件(error.log 中累积的 upstream 故障行 / InnoDB 检测到的真实死锁),因此归属 wave-2b。本 cohort 的具体 inspector(以本变更**归档时冻结**的 `proposal.md` / `tasks.md` 清单为准)**必须**全部以其声明 `name` 干净注册且 registry `errors == []`。

本需求是对 `service-inspector-suite` 的 `新增需求`(ADDED)sibling,**引用**套件已冻结的公共质量门(守 `service-inspector-contract` / 守作者契约且输出键区分 / 附 ReplayTarget fixture 与可证检出 snapshot / 禁引入新基础设施 / wave-2b「确定性录制」窗口聚合采样时坍缩成标量),**不**重述其细则;**禁止** `MODIFY` 已归档的 wave-2a / wave-2b 冻结覆盖需求,**禁止**改写或扩写其清单。

切片判据(与既有 wave-2b 覆盖需求一致):nginx.upstream 的异常态依赖 error.log 中**时间窗口累积**的 upstream 故障流量;mysql.deadlocks 的异常态依赖 InnoDB 在**时间窗口内**检测到的真实死锁(非确定性时序事件)——两者均**禁止**回流 wave-2a。两个 inspector 的窗口/时序聚合**必须**在**采样时刻**于**目标机内**算成**最终标量**(upstream 故障计数 / 死锁 age 秒)并冻结进 collector 输出,`ReplayTarget` 回放原样返回该冻结标量;**禁止**回吐需在回放时按 `now()` 重聚合的原始带时间戳明细。

#### 场景:wave-2b 尾批冻结清单全部干净注册

- **当** 本变更实现完成、运行 `build_registry_from_search_paths([], settings=Settings())`
- **那么** 本变更 proposal/tasks 列出的每个 wave-2b 尾批 inspector(`nginx.upstream` / `mysql.deadlocks`;以**归档时冻结**清单为准;后续 wave 另立 change 不回溯改本 spec)**必须**以其声明 `name` 出现在 registry 中,且 registry `errors == []`

#### 场景:窗口/时序聚合在采样时坍缩成标量、回放确定

- **当** nginx.upstream 采集 error.log 累积 upstream 故障、或 mysql.deadlocks 采集 InnoDB 最近死锁
- **那么** 其聚合(upstream 故障计数 / 死锁检出布尔 + age 秒)**必须**在采样时刻于目标机内算成最终标量、冻结进输出 JSON,`ReplayTarget` 回放**必须**原样返回该冻结标量并产出与快照一致的确定性结果;**禁止** collector 回吐需在回放时按 `now()` 重聚合的原始死锁时间戳明细或带时间戳的 error.log 原始行

#### 场景:semantic-abnormal 须真造累积/时序异常而非低阈值凑

- **当** 评估 nginx.upstream / mysql.deadlocks(其 `findings` 非空)的 semantic-abnormal fixture
- **那么** 该 fixture **必须**对**真实**的累积/时序异常态录制(error.log 中真实累积的 upstream 故障行 / 真实 InnoDB 死锁段文本),且 snapshot 断言其在 manifest **默认阈值**下产出预期 severity + message;**禁止**以「健康态 + 人为低阈值」的 finding-trigger fixture 冒充(沿用 `service-inspector-contract` 双轨 fixture 硬条款)

#### 场景:两个尾批 inspector 进容器 cohort INCLUDE

- **当** 评估 nginx.upstream(读 `/var/log/nginx/error.log`)与 mysql.deadlocks(走 mysql client 服务端 `SHOW ENGINE INNODB STATUS`)的容器安全归类
- **那么** 两者**必须**归入容器安全 cohort 的 **INCLUDE**(均读 service 本地日志 / 服务端,**不**读 host 全局源如 /proc、/sys、journalctl),`targets` **必须**含 `docker` / `k8s` 全集;cohort INCLUDE 计数从 28 增至 30,docker⇔k8s 奇偶不变量保持
