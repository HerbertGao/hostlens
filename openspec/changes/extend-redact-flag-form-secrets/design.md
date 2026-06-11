## 上下文

`hostlens.core.redact.redact_text` 是运行时脱敏的唯一边界函数，纯函数、import 时编译 pattern、按顺序 `re.sub`：

```
out = _KEYWORD_ASSIGN.sub(...)   # key=value / key:value
out = _BEARER_HEADER.sub(...)    # Bearer <tok>
out = _JWT.sub(...)              # eyJ....
out = _SK_KEY.sub(...)           # sk-....
```

`redact_text` 的 runtime 契约此前**无 spec owner**——散落在 `report-data-model` / `remediation-execution-workflow` / `notifier-protocol` 等 spec 中被引用，SOT 仅在 `docs/OPERABILITY.md §7.2`。本提案新建 `text-secret-redaction` capability 正式 own 它。

设计经两轮对抗 review（Security Engineer + review-loop 三方）收敛。第二轮暴露了三处必修缺陷（mask 分级破坏既有契约、B head 检测 under-specified、shlex 重组 lossy）与一处可消除的泄露（A 引号值漏脱），下述决策是 review 后的最终结果。

## 目标 / 非目标

**目标：**
- 扩 `redact_text` 覆盖 flag 形密钥（A 空格长 flag / B 工具短 flag / C URL userinfo / D env 名）
- 零新依赖、零下游契约破坏、保持纯函数与亚毫秒预算

**非目标：**
- **不改既有 4 类的 mask 强度**（mask 分级 = 独立 follow-up，见决策 2）
- 不动 `CASSETTE_SENSITIVE_PATTERNS` / `detect_sensitive_text`（detection 层独立）
- 不抓 base64 / 云 CLI profile（误报陷阱）
- 不引入可配置自定义正则；不翻转 `cli/fix.py` root 拒绝纵深姿态

## 决策

### 决策 1：[A]+[B] 统一走 shell-token 正则（保留源 span），[C]/[D] 留 standalone 正则

**选择**：长 flag（[A]，tool-agnostic）与短 flag（[B]，tool-whitelisted）**都**在**拼接式 shell-word 正则** `(?:[^\s"']+|"[^"]*"|'[^']*')+`（`finditer`）分出的 token 流上处理——该 tokenizer 同时满足三个必要条件：**(a) 引号包裹的含空格值归单 token**、**(b) 每 token 给出精确源 span `m.start()/m.end()`**、**(c) 相邻「非引号 run + 引号段」拼接成一个 shell word**（`-p"my secret"` 归单 token）。URL（[C]）与 env（[D]）保持 standalone 正则。

**替代方案（否决）**：
- 裸正则 `--password\s+(\S+)`——**否决**：引号含空格值上 `\S+` 截断泄露（F8）。不满足 (a)。
- 非拼接 token 正则 `"[^"]*"|'[^']*'|\S+`——**否决**：满足 (a)(b) 但**不满足 (c)**。粘连引号 `-p"my secret"` 中 `\S+` 先吃 `-p"my`、在引号内空格截断，`secret"` 漏脱（第四轮 review-loop U2，实测 `mysql -p"my secret"` → `-p"my` + `secret"`）。`mysql -p"含空格密码"` 是标准写法，故必须用拼接式 `(?:...)+` 让 `-p` 与紧邻引号段拼成一 token。
- `shlex.split` / `shlex.shlex`——**否决**：满足 (a)(c) 但**不满足 (b)**。`shlex` 无公开 token 起始偏移 API，`tell()-len(token)` 在引号/多空格上算出**错位 span**，就地替换写错位置致真值残留（第三轮 review-loop L1，实测）。

**理由**：就地 span 替换（决策 4）要求 (b)，而 glued 引号值要求 (c)，唯有拼接式 shell-word 正则的 `finditer` 同时给 (a)(b)(c)；且 `(?:...)+` 各分支按首字符互斥、无灾难性回溯（实测病态输入亚毫秒）。**代价**：不处理 `\`-转义空格（`--password my\ pass` 切成 `my\` 与 `pass`，后段漏）——罕见、引号是标准写法，accepted best-effort 残留。

**作用域边界**：`redact_text` 输入不全是 shell 命令（还有 markdown 散文 / JSON）。先按 `;`/`|`/`&&`/`||`/单 `&`/换行粗切「疑似 command 段」，只对段做 token 化；C/D 不依赖 token 化。**畸形输入零成本降级**：未闭合引号处 `(?:...)+` 停在引号前、引号后内容另成 token（不抛、不解析其内凭据），无需 `shlex` 的 `ValueError` 处理。

### 决策 2：本提案不改 mask 强度；mask 分级留独立 follow-up

**选择**：新规则 A/B/C/D 与既有 4 类**统一**用既有 `_mask`（前 4 后 4 / ≤8 全 `****`）。**不**实现「凭据类全 mask」分级。

**替代方案（否决，但记为 follow-up）**：Security review 的 mask 分级——凭据类（含既有 `key=value`/`Bearer`）改全 `****`、`sk-`/JWT 保留前 4 后 4。

**理由**：mask 分级要把既有 `key=value`/`Bearer` 从前 4 后 4 翻成全 mask，这**破坏 `report-data-model` scenario 506-507/511-512**（断言 `api_key=sk-abcd...7890` 形式）+ `test_redact.py` / `test_redaction_at_render_boundary.py` 至少 5 处既有断言，并需 MODIFY `report-data-model`。它是**独立于「flag 形覆盖」的关注点**，且为保持「全规则集 mask 策略一致」（避免 `password=X` 前 4 后 4 而 `--password X` 全 mask 的割裂），应当**整体**改、不在本提案半做。故剥离为独立 follow-up（其主体即 `report-data-model` MODIFY + 既有测试断言更新）。本提案因此 `report-data-model` 零破坏、`修改功能` 不含它。

**安全权衡**：本提案对 flag 形仍是前 4 后 4（低熵口令泄露约 67% 中段隐藏），strictly better than 现状（0% 脱敏）；进一步收紧由 follow-up 完成。redact 仍是 best-effort、非安全边界（root 门 + env 注入纵深不变）。

### 决策 3：精确命令头检测 + 仅命令头位置触发白名单

**选择**：命令头 = 跳过 `sudo`/`env <K=V>`/`docker exec <c>`/`ssh <host>`/`nice`/`time` 等 wrapper 前缀后的第一个 token；白名单工具名**只在命令头位置**触发，参数 / 散文位置的同名 token 不触发。

**替代方案（否决）**：「扫描段内任意 token == mysql」。

**理由**（第二轮 F2，head 检测 under-specified）：若退化为任意 token 扫描，散文 `the mysql -p flag` 会 FP、`echo mysql -psecret` 会误脱字面量。锚在命令头 + 有限 wrapper 穿透列表，使 FP（散文工具名）与 FN（`env`/`sudo` 前缀漏识别）都收敛到明确行为，并由场景钉死。

### 决策 4：原文就地 span 替换，禁 shlex 重组回填

**选择**：[A]/[B] 识别出凭据 token 后，在**原段文本**上对该 token 的字面 span 做就地子串替换；**禁止** `" ".join(shlex_tokens)` 重组整段。

**替代方案（否决）**：shlex 分词 → mask → join 回填。

**理由**（第二轮 F3）：任何「分词 → mask → join 重组」回填都会折叠空白 / 丢引号（`echo "a  b"` → `echo a b`），让**不含密钥的命令**也 lossy 改写，破坏负例 precision。就地 span 替换只动凭据 token、其余逐字符保留。

**实现必须用 token 正则源偏移、禁裸字符串搜索**（第二轮 N1 + 第三轮 L1）：定位待替换 span **必须**用 shell-word 正则 `finditer` 的 `m.start()/m.end()` 源偏移；**禁止**「在原文中 `str.find`/`str.replace` 该 token 字面」（suffix-glued flag 密钥值可能等于命令头，裸搜索命中命令头致真密钥全裸——`mysql -pmysql`），**也禁止** `shlex.shlex` 的 `tell()` 路线（实测算出错位 span，见决策 1）。suffix-glued flag 的替换范围限定在**该 flag token 自身源 span** 内 flag 字母之后的值区段。

**masking 按 shell 拼接取真值、含空格才包引号以守幂等**（第四轮 U1 + 第五轮 U-A）：保留 flag 前缀，对值部分 (1) 去**全部**引号字符得 shell 拼接真值（`"my secret"`→`my secret`、脏粘连 `"sec"tail`→`sectail`）(2) `_mask` (3) 产物含空白才包一对双引号。否则 `_mask` 含空格产物写回无引号会被二次 re-tokenize 再脱（破坏幂等）；而「只 strip 外层引号对」对脏粘连引号 `-p"sec"tail` 会残留孤立引号、同样破幂等。**去全部引号 + 含空格才包一对引号** 对干净引号 / 脏粘连引号 / 无引号三态都得 fixpoint（实测 `-p"sec"tail`→`-p****`、`-p"my secret"`→`-p"my s...cret"` 二次均==一次）。

### 决策 5：[D] 精确名 + `=`-锚定，无需 `_FILE` lookahead

**选择**：`\b(<白名单精确名>)=(\S+)`，不加 `_FILE` 前向否定。

**理由**（第二轮 F5）：`=` 紧跟白名单名，`MYSQL_PASSWORD_FILE=` 的 `=` 在 `_FILE` 后、不紧跟任何白名单名，天然不匹配；`PWD` 不在白名单。原设计的 `_FILE` lookahead 对 `=`-锚定是死代码，删去更简。

## 风险 / 权衡

- **[A] 散文 over-mask**（`the --password flag` → `the --password ****`）→ accepted：安全侧 over-mask，脱无害词优于漏真密钥；下一 token 以 `-` 起始则跳过。spec 显式场景锁定。
- **[B] 白名单未覆盖某客户端**（`myhack -p pw` 漏）→ `cli/fix.py` root 拒绝门 + Inspector env 注入纵深兜底；白名单可后续增量加行。
- **畸形引号输入** → shell-token 正则不抛（未闭合引号退化为 `\S+` 字面 token、不解析其内凭据），C/D 照常；单测锁定不抛 + best-effort 残留。
- **就地 span 替换实现复杂度**（同一 token 字面在段内多处出现时定位歧义）→ 实现按 `finditer` token 顺序逐个消费、用各自 `m.start()/m.end()` 替换对应 span，不做全局 `str.replace`；tasks 验收含「重复 token 不误替」。
- **`llm-cassette-testing` MODIFY 的正确性**（标题保留、只收窄正文 + 改一条场景）→ 在 temp 副本实测 `openspec-cn archive` 的 rebuild 校验通过后再合（见迁移计划）。

## 迁移计划

无数据迁移、无配置变更、无 API 破坏。部署即生效（纯函数行为增强）。回滚 = revert 单个 commit。

`llm-cassette-testing` 是 MODIFY（保留需求标题、收窄正文 + 改一条场景），归档前须在 temp 副本跑 `openspec-cn archive` 干跑确认 rebuild 校验过（中文标题 + 场景 4-井号 + 标题完全匹配），防 archive 阶段返工。

实现按 ROI 顺序分阶段（同一 PR 内、tasks 分组）：C 单段 token + D 白名单（standalone 正则，低风险）→ A/B 统一 shell-token 正则流 + 就地 span 替换 → 文档 / 注释同步。

## 待解决问题

- 暂无阻塞性未决项。`cli/fix.py` 注释更新（line 16/109/283-284 + 118）、`docs/OPERABILITY.md §7.2` 规则列表同步在 tasks 内处理。
