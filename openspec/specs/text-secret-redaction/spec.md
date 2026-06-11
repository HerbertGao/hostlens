# text-secret-redaction 规范

## 目的
待定 - 由归档变更 extend-redact-flag-form-secrets 创建。归档后请更新目的。
## 需求
### 需求:`redact_text` 既有 4 类规则的 mask 强度不变，仅 key=value 值匹配扩展为引号感知

本提案在 `hostlens.core.redact.redact_text(s: str) -> str` 上**叠加**新规则。既有 4 类（`key=value`/`key:value` 赋值、`Bearer <tok>`、JWT `eyJ...`、`sk-...`）的 mask 强度/输出格式**必须**不变：长于 8 字符的值 `<前 4>...<后 4>`，≤8 字符 `****`；mask **强度分级**（凭据类改全 `****`）**禁止**纳入本提案范围（会破坏 `report-data-model` 既有 scenario，留 follow-up）。

**唯一例外**：`key=value` 规则（`_KEYWORD_ASSIGN`）的 **value 匹配**从裸 `(\S+)` 扩展为与 [B] 同构的引号感知 shell-word 片段，并改用 `_mask_glued_value` 包装。理由：裸 `(\S+)` 对引号含空格值 `password="a b"` 在引号内空格截断、只脱 `"a` 漏 `b"`（与 flag 形同类泄露）。**无空格值 mask 输出逐字节不变**（`_mask_glued_value` 对无引号无空格值 == `_mask`，故 `report-data-model` scenario 与既有 test 全保持）；引号含空格值现整体脱敏（`password="a b"` → `password=****`）。Bearer/JWT/sk- 三类不动。

#### 场景:既有 key=value 无空格值输出格式不变
- **当** 输入 `password=verylongsecretvalue`
- **那么** 输出含 `password=` 与 `...`（前 4 后 4 形式，与本提案前一致）

#### 场景:key=value 引号含空格值整体脱敏不漏尾
- **当** 输入 `password="abc def"`
- **那么** 输出不含漏脱的 ` def"` 尾段（不发生裸 `(\S+)` 在引号内空格截断致 `password=**** def"`）；值整体脱敏（`password=****`）

#### 场景:既有 sk- 输出格式不变
- **当** 输入 `sk-abcdefghijklmnopqrstuvwxyz1234567890`
- **那么** 输出保留前缀 `sk-a` 与后缀 `7890`、含 `...`

### 需求:`redact_text` 必须脱敏空格分隔的长 flag 形密钥

`redact_text` 必须把形如 `--password <值>` / `--secret <值>` / `--token <值>` / `--api-key <值>` / `--api_key <值>` 的空格分隔长 flag 凭据脱敏（关键字大小写不敏感）。`--password=<值>` 等 `=` 分隔形已由既有 `key=value` 规则覆盖，本需求只补**空格分隔**形。

[A] 必须在 **shell-token 正则流**上处理（见 [B] 需求的分词约定），**不**用裸 `\S+` 扫原文：紧跟长 flag 的**下一个 token** 即值（flag 名 casefold 比对，覆盖 `--Token`）。这样：

- 引号包裹的含空格密码（`--password "my secret pw"`）是单 token、被**整体**脱敏，不发生「只脱第一段、漏后段」的泄露
- 下一个 token 以 `-` 起始（是另一个 flag，非值）时**跳过**，不误脱

**accepted 取舍**：散文中出现 `--password <普通英文词>`（如 `the --password flag is required`）时，下一 token 会被当作值脱敏（误脱无害词）。这是**安全侧 over-mask**——脱掉一个非密钥词优于漏一个真密钥，明确接受。

#### 场景:空格分隔长 flag 被脱敏
- **当** 输入 `mysql --password supersecret123 -h db`
- **那么** 输出不含 `supersecret123` 子串
- **并且** 字面词 `--password` 保留可见

#### 场景:引号包裹的含空格值作单 token 整体脱敏
- **当** 输入 `cmd --password "my secret pw" --verbose`
- **那么** `my secret pw` 作**单一 token** 经 `_mask` 脱敏——输出不含完整串 `my secret pw`、不含中段词 `secret`（不发生裸正则 `\S+` 只截 `"my`、`secret pw"` 整段泄露的旧 bug）；前 4 后 4 字符片段按既有 mask 策略保留（与 mask 分级 follow-up 一致）

#### 场景:大小写不敏感的长 flag
- **当** 输入 `--Token MyTokenValue999`
- **那么** 输出不含 `MyTokenValue999`

#### 场景:值是另一个 flag 时跳过
- **当** 输入 `mysql --password -h dbhost`
- **那么** 输出原样保留 `-h dbhost`（`-h` 不被当作密码脱敏）

#### 场景:散文中长 flag 后的词被安全侧脱敏（accepted）
- **当** 输入 `the --password flag is required`
- **那么** 紧跟 token `flag` 被脱敏为 `****`（接受的 over-mask；非泄露）

### 需求:`redact_text` 必须脱敏已知客户端工具的短 flag 凭据

`redact_text` 必须对一组**已知客户端工具**的凭据短 flag 脱敏。匹配引擎**必须使用拼接式 shell-word 正则分词** `(?:[^\s"']+|"[^"]*"|'[^']*')+`（`finditer` 逐 token 给出**源 span** `m.start()/m.end()`）。

该正则的**拼接** `(?:...)+` 是关键：一个 shell word 可由相邻的「非空非引号 run / 双引号段 / 单引号段」拼接而成，故 `-p"my secret"`（`-p` 粘连引号值，**含空格**）归**单 token** `-p"my secret"`——这正堵住裸 `"[^"]*"|'...'|\S+` 的泄露（`\S+` 在引号内空格处截断，`-p"my` 被脱、`secret"` 漏）。同理 `--password "my secret pw"`（引号在 token 边界）归单 token `"my secret pw"`。双引号段须**转义感知**（`"(?:\\.|[^"\\])*"?`），使 `\"`（curl JSON payload `{\"k\":\"v\"}` 常见）不误判为引号关闭致后段漏脱；单引号在 shell 不处理转义，`'[^']*'?` 即可。**闭合引号 `"?` 必须可选**：一个 alt 同时覆盖闭合与**未闭合**引号（`mysql -p"oops-no-close` 抓到下一引号或 EOS、其内凭据被脱敏而非漏脱）。**这个「可选闭合」是 ReDoS 安全的关键**——若改用「闭合 alt + 未闭合 alt」两个都以 `"` 起始的重叠 alt，长转义引号 run（`"`+`\"`×n，如截断的 curl JSON 错误体）会在每个起始位被重扫，O(n²) 热路径 DoS（实测 32k → ~11s）；单个可选闭合 alt 无重叠、线性。

**不**用裸 `\S+`（引号内空格截断泄露）、**也不**用 `shlex`（`tell()` 不暴露可靠 token 起始偏移、去引号后 token 长 ≠ 源 span 长，无法就地替换——见 design 决策 4）。tokenizer 给出**原始 token 文本 + 源 span**；凭据判定与 masking 时按 shell 拼接语义去引号取真值（见「就地替换」需求）。

**命令段切分（引号感知）**：先把输入按 `;` / `|` / `&&` / `||` / 单 `&` / 换行粗切为「疑似 command 段」（单 `&` 须在 `&&` 之后判定避免吞噬），每段独立 token 化。**切分必须引号感知**——出现在单/双引号内的分隔符（如 `curl "https://x/?a=1&b=2"` URL query 里的 `&`、`redis-cli -a 'sec&&ret'` 值里的 `&&`）**不**作为段分隔符，否则该段命令头会被误判致漏脱。

**精确命令头检测**：一段的「命令头」= 跳过下列前缀包装后的第一个 token——`sudo` / `env`（额外跳 `KEY=VALUE` 赋值前缀）/ `docker exec <容器>` / `ssh <host>` / `nice` / `time`。每个 wrapper 跳过其**前导 `-` 选项**；**取值选项**（每 wrapper 一张表，如 `sudo -u/-g/-p`、`env -u/-C/-S`、`docker exec -u/--user/-e/--env/-w`、`ssh -p/-i/-o/-l/...`、`nice -n`、`time -o/-f`）额外跳过其**值 token**。取值选项缺失其值（命令截断）时返回 None（该段不脱、安全侧）。命令头属白名单时按工具语义脱敏。**白名单工具名只在命令头位置触发**——参数 / 散文位置的同名 token（`echo mysql -psecret` 的 head 是 `echo`、`the mysql -p flag` 的 head 是 `the`）**不触发**。

每个白名单工具按其**特有分隔语义**判定：

- `mysql` / `mariadb`：仅**粘连** `-p<值>`（`-p <值>` 的值是库名不是密码，禁止脱敏）
- `redis-cli`：`-a <值>` 或 `-a<值>`（空格或粘连）、`--pass <值>`
- `mongosh` / `mongo`：`-p <值>` 或 `-p<值>`
- `sshpass`：`-p <值>` 或 `-p<值>`
- `curl`：`-u user:<值>` / `--user user:<值>`——拆分语义**必须**用 `value.partition(":")`：无 `:` 时（纯用户名、无密码）**不脱、不抛**；有 `:` 时 mask **第一个 `:` 之后**全部（保留 user、`a:b:c` → 脱 `b:c`、`:pw` → user 空但脱 `pw`）。

未在白名单内的工具（`myhack -p pw`）**必须不脱敏**。token 正则在畸形 / 未闭合引号输入上**不抛**（未闭合引号由「可选闭合」alt 抓到下一引号或 EOS、其内凭据被脱敏）；整个 `redact_text` 不因畸形命令失败。C/D standalone 正则不依赖 token 化、照常生效。

**已知 accepted 残留**（均由 root 拒绝门 + env 注入纵深兜底，best-effort 边界）：

- 嵌套 shell（`sh -c '<整条命令字符串>'`、`bash -lc '...'`）内的凭据在引号参数内是单 token，命令头判为 `sh`/`bash`（不在白名单），不递归解析、漏脱
- 散文 / 非命令位置的凭据：凭据形 token 前缀是非白名单词（`OSError: mysql -p<pw> failed`、`the mysql -p flag`）→ head 非白名单 → 不脱
- 裸 `KEY=VALUE` 赋值前缀（无 `env` 字面，如 `PGPASSWORD=x mysql -psec`）：命令头检测不识别该前缀为可跳过 wrapper、head 判为 `PGPASSWORD=x`（非白名单），该段 `mysql -psec` 不脱（但 `PGPASSWORD=x` 已由 D 规则脱）；trailing `-psec` 漏脱属残留
- **未表内的取值 wrapper 选项**：某 wrapper 的取值选项不在其取值选项表内（罕见/自定义）时，其值 token 可能被当作命令头 → 该段不脱（安全侧、漏脱），属 best-effort
- `\`-转义空格（`--password my\ pass`）/ 续行 `\<换行>`：token 正则在空格 / 换行处断裂，后段漏
- **前导未闭合引号吞掉后续命令**（`echo "未闭合 ; mysql -p<pw>`）：命令段切分把未闭合引号内的 `;`/`|`/`&&` 视为引号内不切分，该未闭合段的命令头是非白名单词（`echo`）→ 其后 `mysql -p<pw>` 作为同段参数不脱（这是 miss 非 corruption，需对抗性前导未闭合引号才触发）

#### 场景:mysql 粘连短 flag 被脱敏
- **当** 输入 `mysql -psup3rsecret -h db`
- **那么** 输出不含 `sup3rsecret`

#### 场景:mysql 空格 -p 不被误脱敏（值是库名）
- **当** 输入 `mysql -p mydatabase`
- **那么** 输出原样保留 `mydatabase`

#### 场景:redis-cli 空格短 flag 被脱敏
- **当** 输入 `redis-cli -a authpw123 ping`
- **那么** 输出不含 `authpw123`

#### 场景:sshpass 短 flag 被脱敏
- **当** 输入 `sshpass -p hunter2value ssh user@host`
- **那么** 输出不含 `hunter2value`

#### 场景:mongosh/sshpass 粘连短 flag 被脱敏
- **当** 输入 `mongosh -psupersecret123` 或 `sshpass -psupersecret123 ssh u@h`（粘连 `-p<值>`）
- **那么** 输出不含 `supersecret123`（mongosh/mongo/sshpass 的粘连形与空格形同等覆盖，不只空格形）

#### 场景:curl userinfo 凭据被脱敏保留 user
- **当** 输入 `curl -u admin:s3cr3tvalue https://api.host`
- **那么** 输出不含 `s3cr3tvalue`
- **并且** 保留 `admin` 可见

#### 场景:curl -u 无冒号不脱不抛
- **当** 输入 `curl -u admin https://api.host`（`-u` 值无 `:`、纯用户名）
- **那么** `redact_text` 正常返回、输出原样保留 `admin`（`partition(":")` 守卫，不崩溃）

#### 场景:命令头穿透 sudo/docker exec 前缀
- **当** 输入 `sudo docker exec dbc mysql -psup3rsecret`
- **那么** 输出不含 `sup3rsecret`（命令头穿透 `sudo`/`docker exec dbc` 识别到 `mysql`）

#### 场景:非命令头位置的工具名不触发
- **当** 输入 `echo mysql -psecretliteral`
- **那么** 输出原样保留 `mysql -psecretliteral`（命令头是 `echo`、不在白名单）

#### 场景:env 前缀下短 flag 仍被脱敏
- **当** 输入 `env FOO=bar mysql -psup3rsecret`
- **那么** 输出不含 `sup3rsecret`（命令头穿透 `env FOO=bar` 识别到 `mysql`）

#### 场景:带选项的 wrapper 前缀穿透
- **当** 输入 `sudo -n mysql -psup3rsecret` / `nice -n 10 mysql -psup3rsecret` / `env -i mysql -psup3rsecret` / `docker exec --user root c mysql -psup3rsecret` / `time -p mysql -psup3rsecret`
- **那么** 各输出均不含 `sup3rsecret`（命令头跳过 wrapper 的前导选项与取值选项的值，识别到 `mysql`）

#### 场景:引号内分隔符不切断命令段致漏脱
- **当** 输入 `curl "https://x/?a=1&b=2" -u admin:supersecret123`（URL query 含 `&`）
- **那么** 输出不含 `supersecret123`、保留 `admin`，且 URL 的 `a=1&b=2` 原样（引号内 `&` 不作段分隔符、`curl` 仍是命令头）

#### 场景:转义引号不破坏 token 致漏脱
- **当** 输入 `curl -d "{\"k\":\"v\"}" -u admin:supersecret123 https://h`（JSON payload 含转义引号 `\"`）
- **那么** 输出不含 `supersecret123`、保留 `admin`（转义感知双引号段使 `\"` 不误判为引号关闭、JSON payload 归单 token、后续 `-u` 仍被识别脱敏）

#### 场景:未知工具的同形 flag 不被脱敏
- **当** 输入 `myhack -p hunter2value`
- **那么** 输出原样保留 `hunter2value`

#### 场景:未闭合引号值被脱敏且不抛
- **当** 输入 `mysql -p"unterminatedsecret extra`（未闭合引号）
- **那么** `redact_text` 必须正常返回（不抛）、且输出不含 `unterminatedsecret`（末尾 `"[^"\n]*` 兜底把未闭合 run 抓到行末脱敏）

### 需求:[A]/[B] 必须在原文上就地替换凭据 token，禁止 shlex 重组回填

[A]/[B] 脱敏**禁止**用 `shlex.split` 的 token 列表 `" ".join` 重组整段回填——那会吃掉引号 / 折叠空白 / 丢失转义，对**不含密钥的命令**也产生 lossy 改写，破坏负例 precision。实现必须只对**识别为凭据的 token**，在**原段文本上做就地子串 span 替换**（mask 该 token 的原始字面），其余 token 与分隔符原样保留。

**span 定位必须用 token 正则的源偏移、禁止裸字符串搜索**：实现**必须**用 shell-word 正则 `finditer` 给出的 `m.start()/m.end()` 源 span 定位待替换区段，**禁止**对 mask 目标值做 `str.find` / `str.replace`，**也禁止**用 `shlex.shlex` 的 `tell()`（实测 `tell()-len(token)` 在引号/多空格 token 上算出错位 span，致 mask 写错位置真值残留 + 命令损坏）。理由：suffix-glued flag（`-p<值>` / `-a<值>`）的密钥值可能**等于段内另一 token 的字面或命令头本身**（`mysql -pmysql`：值 `mysql` == 命令头 `mysql`）；裸 `str.find("mysql")` 从段首搜会命中**命令头**致真密钥全裸 + 命令损坏。对 suffix-glued flag，替换范围**必须**限定在**该 flag token 自身**源 span 内 flag 字母之后的值区段（`m.start()+len("-p")` 到 `m.end()`），不得溢出到其它 token。

**masking 必须按 shell 拼接语义取真值、产物保持单一 shell word**（幂等性硬约束）：替换时保留 flag 前缀（`-p` / `--password ` 等），对值部分按以下步骤产出替换串：

1. **取真值**：把值部分（flag 字母之后到 token 末尾的整段，可能含拼接的引号段）**去掉所有引号字符**得 shell 拼接后的真实值——`"my secret"` → `my secret`、`"sec"tail`（脏粘连引号）→ `sectail`、`-pmysql`（无引号）→ `mysql`
2. **mask**：`_mask(真值)`
3. **包裹**：mask 产物**含空白**则包一对双引号（`"my s...t pw"`）使其仍是单 token、否则裸输出（`****` / `auth...w123`）

理由：`_mask` 对含空格真值产出含空格产物，若写回无引号会被二次 `redact_text` re-tokenize 再脱（破坏幂等）；而对脏粘连引号 `-p"sec"tail` 若只 strip 外层引号对则残留孤立引号、同样破幂等——**去全部引号取真值 + 含空格才包一对引号**对两种形态都得 fixpoint（`-p"sec"tail` → `-p****`、`-p"my secret"` → `-p"my s...cret"`，二次均 == 一次，实测）。

#### 场景:suffix-glued 密钥等于命令头字面时只脱 suffix
- **当** 输入 `mysql -pmysql`（密钥值 `mysql` 恰等于命令头 `mysql`）
- **那么** 输出为 `mysql -p****`（命令头 `mysql` 保留、仅 `-p` 后的 suffix 被脱敏），**不得**把命令头脱成 `**** -pmysql`

#### 场景:同一密钥字面出现两次各自就位
- **当** 输入 `redis-cli -aget -aget`（重复 suffix-glued token）
- **那么** 两个 `-aget` 的 suffix 各自在其 token span 内被脱敏，不互相错位

#### 场景:粘连短 flag 的引号含空格值整体脱敏不漏中段
- **当** 输入 `mysql -p"my secret"`（`-p` 粘连引号、值**含空格**）
- **那么** 拼接式 shell-word 正则归单 token `-p"my secret"`，输出脱敏后**不含中段 `secret`**（不发生 `\S+` 在引号内空格截断致 `secret` 残留）；保留 `-p` 前缀，含空格真值包一对引号（如 `-p"my s...cret"`）

#### 场景:引号含空格长 flag 值脱敏后幂等
- **当** 对 `cmd --password "my secret pw"` 连续调用 `redact_text` 两次
- **那么** 第一次输出包裹引号（如 `--password "my s...t pw"`）使其仍为单 token，第二次输出与第一次**完全相等**（不发生二次 re-tokenize 再脱）

#### 场景:脏粘连引号值脱敏后幂等
- **当** 对 `mysql -p"sec"tail`（引号段后粘连非引号尾、shell 拼接真值为 `sectail`）连续调用 `redact_text` 两次
- **那么** 第一次按 shell 拼接取真值 `sectail` → `_mask` → `mysql -p****`（无空格不包引号、无残留孤立引号），第二次输出与第一次**完全相等**

#### 场景:无凭据命令的格式被完整保留
- **当** 输入 `redis-cli set mykey "a  b"`（双空格、无凭据）
- **那么** 输出与输入逐字符相等（双空格、引号不被折叠或丢失）

#### 场景:脱敏只改凭据 token 不动其余
- **当** 输入 `mysql -psup3rsecret --comment "keep  spaces"`
- **那么** 输出脱敏 `sup3rsecret` 但 `--comment "keep  spaces"` 段原样保留（双空格不折叠）

### 需求:`redact_text` 必须脱敏 URL userinfo 凭据

`redact_text` 必须脱敏 URL 中的 userinfo 密码（standalone 正则，不经命令段 token 化）：

- 双段形 `scheme://user:<密码>@host`：脱敏密码段，保留 `scheme://user:` 前缀
- 单段形 `scheme://<token>@host`（无冒号、token 直接接 `@`）：脱敏 token，覆盖 `git clone https://ghp_xxx@host` 等 PAT 嵌入形

`scheme` 匹配**大小写不敏感**（`(?i)`）——URL scheme 按 RFC 3986 大小写无关，`HTTPS://` / `REDIS://` 必须同样命中。scheme run **必须长度有界**（`[A-Za-z][A-Za-z0-9+.-]{0,30}`，真 scheme 都短）：无界 `*` 会使长非-URL 字母数字 blob（cert dump / base64 / log 行，都流经此热路径函数）触发 O(n²) 回溯（实测 64KB → ~17-36s DoS）；加界后每个起始位的工作量受限、整体线性（64KB → <0.05s）。

userinfo 段内禁止跨越 path 的 `/`（即 `user` / `密码` / `token` 必须是 `[^/@\s]` 字符），以防把 `host/a:b@c` 形路径误判为 userinfo。

**已知 accepted over-mask**：单段形对纯用户名 URL（`ssh://deploy@host`，`deploy` 是用户名非 token）会把用户名 over-mask 成 `****`。这是**安全侧取舍**——over-mask 一个用户名优于漏一个 `token@host` 形 PAT，明确接受。

#### 场景:大写 scheme 的 userinfo 仍被脱敏
- **当** 输入 `HTTPS://ghp_abcd1234efgh5678@github.com/o/r`
- **那么** 输出不含 `ghp_abcd1234efgh5678`（大写 scheme 不漏脱）

#### 场景:双段 userinfo 密码被脱敏
- **当** 输入 `redis://appuser:s3cr3tpw@cache.host:6379`
- **那么** 输出不含 `s3cr3tpw`
- **并且** 保留 `appuser` 可见

#### 场景:单段 token userinfo 被脱敏
- **当** 输入 `git clone https://ghp_abcd1234efgh5678@github.com/org/repo`
- **那么** 输出不含 `ghp_abcd1234efgh5678`

#### 场景:纯用户名单段 URL 被安全侧 over-mask（accepted）
- **当** 输入 `ssh://deployuser@host`（`deployuser` 是用户名非 token）
- **那么** `deployuser` 被 over-mask（accepted 安全侧取舍：over-mask 用户名优于漏 `token@host` 形 PAT）

#### 场景:无 userinfo 的 URL 不被改动
- **当** 输入 `redis://localhost:6379/0`
- **那么** 输出原样保留 `redis://localhost:6379/0`

### 需求:`redact_text` 必须脱敏已知 env 名凭据并排除路径形

`redact_text` 必须对一组**已知凭据 env 名**的赋值脱敏（standalone 正则），覆盖被既有 `\b(password|...)` 词边界漏掉的形态（`PGPASSWORD`、`MYSQL_PWD` 等）。白名单**至少**含：`PGPASSWORD` / `MYSQL_PWD` / `REDIS_PASSWORD` / `REDISCLI_AUTH` / `MONGODB_PASSWORD`。

匹配形态为**精确名 + `=`-锚定**，值组**须与 [B] 的 shell-word 正则逐字节同构**（含**转义感知 + 可选闭合双引号段**）：`\b(<白名单名>)=((?:[^\s"']+|"(?:\\.|[^"\\])*"?|'[^']*'?)+)`。四点缺一即泄露/DoS：裸 `(\S+)` 在引号内空格截断（`PGPASSWORD="my secret pw"` 只脱第一段）；**普通三选一** `("[^"]*"|'[^']*'|\S+)` 对脏粘连引号 `PGPASSWORD="sec"rettail` 只匹配 `"sec"`、漏脱粘连尾 `rettail`（须拼接 `(?:...)+` 归一整个 shell word）；**非转义感知双引号** `"[^"]*"` 对转义引号 `PGPASSWORD="a\" tail"` 在 `\"` 误判关闭、漏脱空格后尾段（须 `\\.` 转义感知）；**闭合/未闭合两个重叠 alt** 在长转义引号 run 上 O(n²) DoS（须合并为单个可选闭合 `"?`、见 [B] tokenizer）。masking 按 shell 拼接语义去全引号取真值、含空格才包一对引号（与 [A]/[B] 同 `_mask_glued_value` 包装、守幂等）。

该形态**天然**排除两类误伤，无需额外 lookahead：

- `MYSQL_PASSWORD_FILE=/path`（`_FILE` 路径形）——`=` 紧跟在 `_FILE` 后、不紧跟白名单名，故不匹配
- `PWD=/home/x`（shell 工作目录）——`PWD` 不在白名单

**禁止**放宽到通用 `*PWD*` / `*PASSWORD*`（会误伤上述两类）。

#### 场景:已知 env 名凭据被脱敏
- **当** 输入 `PGPASSWORD=p@ssw0rdvalue psql -U app`
- **那么** 输出不含 `p@ssw0rdvalue`

#### 场景:MYSQL_PWD 被脱敏
- **当** 输入 `MYSQL_PWD=dbsecretvalue mysql -u root`
- **那么** 输出不含 `dbsecretvalue`

#### 场景:引号包裹的含空格 env 值整体脱敏
- **当** 输入 `PGPASSWORD="my secret pw" psql`
- **那么** 输出不含完整 `my secret pw`、不含中段 `secret`（不发生 `(\S+)` 在引号内空格截断致 `secret pw"` 残留）；脱敏后仍单 token（如 `PGPASSWORD="my s...t pw"`）守幂等

#### 场景:脏粘连引号 env 值整体脱敏不漏尾
- **当** 输入 `PGPASSWORD="sec"rettail psql`（引号段后粘连非引号尾、shell 拼接真值 `secrettail`）
- **那么** 输出不含粘连尾 `rettail`（拼接式值组把 `"sec"rettail` 归一个 shell word，不发生普通三选一只脱 `"sec"` 漏 `rettail`）；二次脱敏 fixpoint

#### 场景:PWD 工作目录不被误脱敏
- **当** 输入 `PWD=/home/alice make build`
- **那么** 输出原样保留 `/home/alice`

#### 场景:`_FILE` 后缀路径不被脱敏
- **当** 输入 `MYSQL_PASSWORD_FILE=/run/secrets/db_pass`
- **那么** 输出原样保留 `/run/secrets/db_pass`

### 需求:`redact_text` 必须保持负例 precision 与幂等性

`redact_text` 必须不脱敏明确的非密钥 token，并对已脱敏文本幂等：

- 进程 / 通用 `-p` flag（`ps -p 1234` / `kubectl logs -p`）必须原样不动
- shell 工作目录 `PWD=` 与 `_FILE` 路径 env 必须原样不动（见 env 需求）
- 未知工具的凭据形 flag 必须原样不动（见短 flag 需求）
- 对任意字符串 `s`，必须满足 `redact_text(redact_text(s)) == redact_text(s)`（`_mask` 产物 `<前 4>...<后 4>` / `****` 是 fixpoint，不被二次脱敏破坏）

#### 场景:ps -p 进程号不被脱敏
- **当** 输入 `ps -p 1234`
- **那么** 输出原样保留 `ps -p 1234`

#### 场景:脱敏幂等
- **当** 对 `mysql -psup3rsecret -h db` 连续调用 `redact_text` 两次
- **那么** 第二次输出与第一次输出完全相等

#### 场景:无密钥纯文本不被改动
- **当** 输入 `restart the mysql service`
- **那么** 输出原样保留 `restart the mysql service`
