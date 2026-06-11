## 为什么

`hostlens.core.redact.redact_text` 是运行时脱敏的唯一边界函数——所有写入报告 / 日志 / Notifier payload / remediation plan 预览的字符串都过它。但它当前只覆盖 4 类**强锚点**密钥：`key=value` 赋值、`Bearer <tok>`、JWT、`sk-...`。**flag 形密钥全部漏脱**：

- `mysql -psecret` / `redis-cli -a secret`（粘连或空格短 flag）
- `--password secret` / `--token secret`（空格分隔长 flag）
- `redis://user:pw@host` / `git clone https://ghp_xxx@host`（URL userinfo）
- `PGPASSWORD=x` / `MYSQL_PWD=x`（词内嵌关键字 env，被 `\b` 卡死）

这不是假想风险：`remediation-execution-workflow` spec §EUID==0 拒绝必须最早 已明文写「plan 命令可能含 `redact_text` 漏脱的 flag 形密钥」，并因此把 root 拒绝门提前到任何 plan 渲染之前来兜底。真实来源是两类**外部输入**：(a) LLM 生成的 remediation plan command；(b) 用户手填命令漏进报告 / 日志。内置 Inspector 自身走 `HOSTLENS_*` env 注入、不内联凭据，不是来源。

## 变更内容

把 `redact_text` 的 runtime masking 规则集从 4 类扩到覆盖 flag 形密钥。**新规则复用既有 `_mask`（前 4 后 4 / ≤8 字符 `****`），既有 4 类 masking 强度/输出格式不变**（mask 强度分级是独立 follow-up，见非目标）。**唯一例外**：既有 `key=value` 规则的 value 匹配从裸 `(\S+)` 扩展为引号感知（同类引号含空格值泄露 `password="a b"`→`**** b"`，与 flag 形同源；无空格值 mask 输出逐字节不变、`report-data-model` 与既有测试全保持）：

- **新增 [A] 空格分隔长 flag**：`--password X` / `--token X` / `--api-key X`（`--password=X` 已被既有 `key=value` 规则覆盖，本条只补空格形）。**[A] 与 [B] 统一走拼接式 shell-word 正则流处理**（`(?:[^\s"']+|"[^"]*"|'[^']*')+` 的 `finditer`，不用裸 `\S+`、不用 `shlex`）：分词后 `--password`/`--secret`/`--token`/`--api-key` 这类长 flag 的**下一个 token**即值——引号包裹的含空格密码（`--password "my pass"`、粘连 `mysql -p"my pass"`）由拼接式正则归**单 token**、被整体脱敏，不漏；下一 token 以 `-` 起始（另一个 flag）则跳过。
- **新增 [B] 工具锚定短 flag**（同一 shell-token 正则流 + 工具白名单）：仅当命令头是已知客户端（`mysql`/`mariadb`/`redis-cli`/`mongosh`/`mongo`/`sshpass`/`curl`）时，对其**特有分隔语义**的凭据 flag 生效（`mysql -pX` 仅粘连；`redis-cli -a X` 空格或粘连；`sshpass -p X`；`curl -u user:X`）。未知工具不动。
- **新增 [C] URL userinfo**（standalone 正则）：`scheme://user:pw@host`（mask 密码段，保留 user）**与单段 `scheme://token@host`**（无冒号、token 直接接 `@`，覆盖 `ghp_`/`glpat-` PAT）
- **新增 [D] 已知 env 名白名单**（standalone 正则）：`PGPASSWORD` / `MYSQL_PWD` / `REDIS_PASSWORD` / `REDISCLI_AUTH` / `MONGODB_PASSWORD` 等；**精确名 + `=`-锚定**（`\b(name)=`）天然排除 `MYSQL_PASSWORD_FILE=` 这类 `_FILE` 路径形与 `PWD=` 工作目录（`PWD` 不在白名单），**不放宽到通用 `*PWD*`**。

实现优先级（tasks 据此排序，可增量落地）：C 单段 token 形 + D 白名单（高 ROI 低风险、standalone 正则）→ A/B 统一 shell-token 正则流 + 就地 span 替换（最该一次做对、避免裸 `\S+` 咬错引号 token、避免 `shlex` 无源偏移返工）。

## 功能 (Capabilities)

### 新增功能
- `text-secret-redaction`: 正式 own `hostlens.core.redact.redact_text` 的 runtime masking 契约（此前散落在 `report-data-model` / `remediation-execution-workflow` 等 spec 中被引用、SOT 仅在 `docs/OPERABILITY.md §7.2`），枚举完整规则集（既有 4 类 + 新增 A/B/C/D）、shell-token 正则分词对 [A]+[B] 的应用边界与精确命令头检测、用 `finditer` 源 span 的 in-place 子串替换（禁重组回填、禁裸搜索）、以及负例 precision 与幂等保证。

### 修改功能
- `llm-cassette-testing`: 该 capability 有一条需求正文写「**禁止扩大 `redact_text()` 的既有 runtime masking 语义**」。本提案专门扩 runtime secret-masking 规则集，与该绝对措辞**字面冲突**。MODIFY 把该条收窄到其真实意图——**禁止把 cassette 的非-secret detection 类别（HOME / 路径 / IPv4 / email / hostname-FQDN）引入 runtime masking**；runtime 的 **secret** 规则集由 `text-secret-redaction` capability 独立 own 并可扩展，非本约束冻结对象。「runtime 语义不变」场景同步从「与本提案前完全一致」改述为「路径 / HOME 等非-secret 类别仍不被 runtime masking 收紧」的不变量（本提案不动路径处理，该不变量保持）。
- `remediation-execution-workflow`: audit 需求正文与场景声明「`redact_text` **不覆盖** flag 形密钥（`mysql -p<pw>` / `redis-cli -a <pw>` / URL userinfo）——原样进永久 audit.log」。本提案使**已知工具** flag 形被脱敏，该绝对声明变假。MODIFY 收窄为「覆盖已知工具 flag 形（best-effort）；**未知工具**仍残留」，并**保留** env 注入指引与 root 门理由（未知工具仍漏、脱敏非安全边界）。同步修既有断言测试 `tests/remediation/test_audit.py`（flag 形 passthrough → 已知工具被脱敏）。
- `remediation-runbook`: 同源——runbook 渲染需求正文与场景同样声明「flag 形不覆盖」，MODIFY 同样收窄为「已知工具覆盖、未知残留」。

（`report-data-model` **不在**修改功能：其 :496「继承 OPERABILITY §7.2 + 保留前 4 后 4」与 scenario 506-512 在本提案下保持准确——既有 4 类 mask 输出不变、新规则同样用 `_mask` 前 4 后 4，scenario 全部仍过。）

## 影响

- **代码**：`src/hostlens/core/redact.py`（加 standalone 正则 C/D + 命令段粗切 + shell-token 正则分词辅助 + A/B token 流处理 + `finditer` 源 span in-place 替换）；`tests/core/test_redact.py`（既有 5 个测试类 → 9 个，新增 A/B/C/D 各类，重点补负例 precision + 幂等回归锚）。**既有 `test_redact.py` / `test_redaction_at_render_boundary.py` 断言不变**（mask 策略未动）。
- **文档 SOT**：`docs/OPERABILITY.md §7.2` 默认脱敏规则列表补 A/B/C/D。
- **spec delta**：本变更 `specs/text-secret-redaction/`（新）+ `specs/llm-cassette-testing/`（MODIFY 收窄冻结条款）+ `specs/remediation-execution-workflow/` 与 `specs/remediation-runbook/`（MODIFY 各一条需求，把「flag 形不覆盖」收窄为「已知工具覆盖、未知残留」）。
- **依赖**：零新增——只用标准库 `re`（shell-token 正则 / standalone 正则）。
- **下游契约**：无破坏。所有调 `redact_text` 的渲染边界（`reporting/_redact.py` / `notifiers/base.py` / `remediation/{audit,runbook}.py` / `cli/fix.py` / `scheduler/runner.py` 等）行为**只增不减**——既有命中输出格式不变，仅新增 flag 形命中被脱敏。
- **注释更新**：`cli/fix.py` 三处「`redact_text` 不覆盖 flag 形」措辞（line 16 / 109 / 283-284，+ line 118 hint 字符串）改为「仅覆盖已知工具 flag 形，best-effort」——root 拒绝门的理由依旧成立（未知工具仍漏）。
- **`remediation-execution-workflow` / `remediation-runbook` 收口**：两 spec 文字与场景称「flag 形（`mysql -p<pw>` / `redis-cli -a`）属已知残留不覆盖」，扩展后**已知工具**的这些形态会被覆盖——此声明变假。本变更 **MODIFY 收口**（见修改功能），把「不覆盖」收窄为「已知工具覆盖、未知残留」；root 门 + env 注入纵深仍成立（未知工具仍漏、脱敏非安全边界）。并同步修既有断言测试 `tests/remediation/test_audit.py::test_audit_flag_form_secret_is_known_residual_passthrough`（现 audit 会脱敏已知工具 flag 形）。

## 非目标 (Non-Goals)

- **不**改既有 4 类（`key=value` / `Bearer` / JWT / `sk-`）的 mask **强度**。Security review 指出前 4 后 4 对低熵口令有泄露、应改全 mask `****`——但该「mask 分级」改动会破坏 `report-data-model` scenario 506-512 + 多个既有测试断言，是独立于「flag 形覆盖」的关注点，**留独立 follow-up 提案**（其主体即 `report-data-model` MODIFY）。本提案新规则与既有一致用前 4 后 4，保证全规则集 mask 策略一致、零既有破坏。
- **不**改 `CASSETTE_SENSITIVE_PATTERNS` / `detect_sensitive_text`（cassette 提交门禁是 detection/reject 的独立机制；本提案只动 runtime masking 规则集，并 MODIFY cassette spec 一条需求文字以消字面冲突）。
- **不**抓 base64 编码凭据——正则无法判定一段 base64 是不是凭据，强抓 = 海量误报，属 detection / 熵检测层职责。
- **不**抓云 CLI（`aws --profile X` 的 X 是 profile 名不是密码 / `az` / `gcloud`）——是 false-positive 陷阱不是密钥来源。
- **不**翻转防御纵深姿态：`cli/fix.py` root 拒绝最早门、Inspector 走 env 注入不内联凭据，扩展后**保留**——未知工具 flag 形仍漏，redact 仍是 best-effort 不是安全边界。
- **不**做 `redact_text` 可配置自定义正则（OPERABILITY §7.2 提及但属独立能力）。

## 对外契约影响

- **CLI 命令 / Inspector schema / Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest**：均无变更。
- **`hostlens.core.redact` 模块公共 API**：`redact_text(s: str) -> str` 签名不变；`__all__` 不变。新增 pattern 与分词辅助为模块私有（`_` 前缀）。
- **`notifier-protocol`**：[C] URL userinfo 扩展使 redact_text 开始覆盖一部分 URL 内嵌凭据。非破坏（Notifier 的额外结构化擦除仍必需、path 段 `bot<token>` 仍不覆盖）；该 spec 「redact 不覆盖 URL」的事实陈述将部分过时，留 follow-up 顺手更新、不在本变更 MODIFY。
- **`llm-cassette-testing`**：见修改功能（MODIFY 一条需求收窄冻结条款）。

## Failure Modes

1. **畸形 / 未闭合引号命令**（[A]/[B] 输入是任意字符串，非保证合法 shell）→ shell-token 正则**不抛**：未闭合引号 token 退化为 `\S+` 字面 token（不解析其内凭据，best-effort 残留）；C/D standalone 正则照常，整个 `redact_text` 不因畸形命令失败。无需 `shlex` 的 `ValueError` 处理。
2. **[B] 工具白名单未覆盖某客户端**（`myhack -p pw`）→ flag 形漏脱。降级：由 `cli/fix.py` 既有 root 拒绝门 + env 注入纵深兜底；文档明示 best-effort 边界。
3. **[A] 长 flag 后跟非密码 token**（散文 `the --password flag is required`）→ `--password` 的下一 token `flag` 会被脱敏（误脱非密钥词）。**accepted 取舍：安全侧 over-mask**（脱掉一个无害词 vs 漏一个真密钥，取前者）；下一 token 以 `-` 起始（另一 flag）则跳过。spec 列此为显式接受场景。
4. **整段重组致非凭据 token 失真**（`echo "a  b"` → `echo a b`）→ 防御：**禁止整段重组回填**；只对识别为凭据的 token 用其 `finditer` 源 span 在**原段文本上做就地子串替换**，非凭据 token 原样保留。spec 强制此契约 + 双空格负例锁定。
5. **mask 产物二次脱敏破坏幂等**：`_mask` 产物 `<前4>...<后4>` / `****` 是 fixpoint（`_mask(_mask(x))==_mask(x)`），后续规则不重入破坏；单测锁定 `redact_text(redact_text(s)) == redact_text(s)`。

## Operational Limits

- **并发预算**：N/A——`redact_text` 是纯函数 CPU 操作，无 IO、无 await。
- **内存预算**：O(len(s))；shell-token 分词仅对**疑似 command 段**（先按 `;`/`|`/`&&`/`&`/换行粗切）调用，不对整篇 markdown / JSON 做分词。
- **超时**：所有正则（standalone + 拼接式 shell-word `(?:[^\s"']+|"[^"]*"|'[^']*')+`）避免灾难性回溯（交替分支按首字符互斥、无歧义分解）；`finditer` 线性，实测病态输入亚毫秒。单字符串处理保持亚毫秒级。

## Security & Secrets

- **不引入新密钥**、不新增配置项、不扩大攻击面（纯本地字符串处理，无网络 / 无文件 IO）。
- **核心收益即安全收益**：缩小 flag 形凭据经报告 / 日志 / Notifier 泄露的概率。
- **不削弱**任何既有脱敏（行为只增不减）；mask 强度不变（既有低熵泄露问题由 follow-up 处理）。

## Cost / Quota Impact

- **零** token 消耗 / 零 API 调用 / 对 Anthropic 配额无影响——纯本地工具函数。

## Demo Path

5 分钟内、无 SSH / 无付费 API：

```bash
python -c "
from hostlens.core.redact import redact_text
for s in [
    'mysql -psup3rsecret -h db',          # [B] 粘连短 flag
    'redis-cli -a authpw123 ping',         # [B] 空格短 flag
    'sshpass -p hunter2value ssh u@h',     # [B] sshpass
    '--password \"my secret pw\" --verbose',# [A] 引号含空格值作单 token 整体脱敏
    'mysql -p\"my secret\" -h db',           # [B] 粘连引号含空格值不漏中段（拼接正则）
    'git clone https://ghp_abcd1234efgh@github.com/x',  # [C] 单段 token@host
    'PGPASSWORD=p@ssw0rd psql -U app',     # [D] env 白名单
    'ps -p 1234',                          # 负例：不动
    'PWD=/home/alice make',                # 负例：工作目录不动
    'MYSQL_PASSWORD_FILE=/run/secrets/db', # 负例：_FILE 路径不动
    'echo \"a  b\" && redis-cli get k',     # 负例：非凭据 token 不失真（双空格保留）
]:
    print(f'{s!r:50} -> {redact_text(s)!r}')
"
```

预期：前 7 行密钥段被前 4 后 4 / `****` 替换（含粘连引号含空格值不漏中段 `secret`），后 4 行**原样输出**（precision 负例，含双空格保留）。同时 `pytest tests/core/test_redact.py -q` 全绿（9 类 + 负例 + 幂等回归锚），既有 `test_redaction_at_render_boundary.py` 不受影响。
