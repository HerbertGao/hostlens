## 1. 第一批 — standalone 正则（C URL + D env，低风险高 ROI）

- [x] 1.1 在 `core/redact.py` 加 `_URL_USERINFO` 双段正则 `([A-Za-z][A-Za-z0-9+.-]*://[^:@/\s]+):([^@/\s]+)@`（scheme **大小写不敏感**；mask group 2、保留 scheme://user:，统一走既有 `_mask`）。验收：`redis://appuser:s3cr3tpw@cache:6379` 输出不含 `s3cr3tpw`、保留 `appuser`；`HTTPS://u:pw@h` 大写 scheme 同样脱敏；`redis://localhost:6379/0` 原样不动
- [x] 1.2 在 `core/redact.py` 加 `_URL_TOKEN` 单段正则 `([A-Za-z][A-Za-z0-9+.-]*://)([^:@/\s]+)@`（scheme 大小写不敏感；mask group 2，覆盖 `https://ghp_xxx@host`）。验收：`git clone https://ghp_abcd1234efgh5678@github.com/o/r` 与 `HTTPS://ghp_...@h` 均不含 token；密钥不进任何渲染输出（脱敏测试）
- [x] 1.3 在 `core/redact.py` 加 `_ENV_CREDENTIAL` 正则 `\b(PGPASSWORD|MYSQL_PWD|REDIS_PASSWORD|REDISCLI_AUTH|MONGODB_PASSWORD)=(\S+)`（精确名 + `=`-锚定，**不加 `_FILE` lookahead**——`=` 锚定天然排除）。验收：`PGPASSWORD=p@ssw0rdvalue` / `MYSQL_PWD=dbsecretvalue` 脱敏；`PWD=/home/alice` 与 `MYSQL_PASSWORD_FILE=/run/secrets/db_pass` 原样不动（密钥脱敏 + 负例 precision 测试，且确认负例靠白名单/`=`-锚定挡住、非 lookahead）

## 2. 第二批 — shell-token 分词基建

- [x] 2.1 在 `core/redact.py` 加 `_segment_commands(s) -> list[(text, start, end)]`：按 `;`/`|`/`&&`/`||`/单 `&`(在 `&&` 之后判定)/换行粗切疑似 command 段，**保留每段原文 span** 以便就地回填。验收：单测覆盖 `a; b | c && d` 切成 4 段且 span 可回填重组 == 原串
- [x] 2.2 加 `_shell_tokens(segment) -> list[(raw, start, end)]`：用**拼接式 shell-word 正则** `re.compile(r'(?:[^\s"\']+|"[^"]*"|\'[^\']*\')+')` 的 `finditer`，每 token 返回**原始 token 文本** + **源 span** `m.start()/m.end()`（去引号取真值由 3.1 按 shell 拼接处理）；拼接 `(?:...)+` 使 `-p"my secret"`（粘连引号含空格）归**单 token**；**不抛**（未闭合引号处停在引号前）。验收：`--password "my secret pw"` → 单 token `"my secret pw"`；`mysql -p"my secret"` → 单 token `-p"my secret"`（不在引号内空格截断）；`mysql -p"unterminated` 不抛
- [x] 2.3 加 `_command_head(tokens) -> str | None`：跳过 `sudo`(及 `-u`/`-E` 选项)/`env`(及 `K=V` 前缀)/`docker exec <容器>`(及 `-it`)/`ssh <host>`(其 `-p port`/`-i`/`-o` 带值 opt)/`nice`/`time` wrapper 前缀，返回第一个真命令 token；**wrapper opt 解析失败时返回 None（该段不脱，安全侧）**。验收：`sudo docker exec c mysql ...`→`mysql`；`env FOO=bar mysql`→`mysql`；`ssh -p 22 h mysql`→`mysql`；`echo mysql`→`echo`

## 3. 第三批 — A 长 flag + B 工具短 flag（shell-token 正则流 + 就地 span 替换）

- [x] 3.1 加 `_redact_command_credentials(segment) -> str`：用 `_shell_tokens` 拿 (value, span) 列表 + `_command_head` 判白名单；[A] 长 flag——token 的 `value.lower()` ∈ `{--password,--secret,--token,--api-key,--api_key}`(**casefold 比对**，覆盖 `--Token`)则取**下一 token**为值(以 `-` 起始则跳过)；[B] 工具表——`mysql/mariadb` 仅粘连 `-p<值>`、`redis-cli` `-a`/`-a<值>`/`--pass`、`mongosh/mongo` `-p`、`sshpass` `-p`、`curl` `-u`/`--user` `user:<值>`(`value.partition(":")`：无 `:` 不脱不抛、有 `:` 脱第一个 `:` 之后)。**对识别出的凭据用其 token `m.start()/m.end()` 源 span 做就地替换**——suffix-glued flag 替换范围限定在该 flag token 源 span 内 flag 字母之后；**masking 保留 flag 前缀、值部分按 shell 拼接取真值**（去全部引号字符：`"my secret"`→`my secret`、脏粘连 `"sec"tail`→`sectail`）`_mask` 后**含空白才包一对双引号**成单 token 守幂等；**禁 `" ".join` 重组、禁 `str.find`/全局 `str.replace`、禁 `shlex.tell()`**（防 `mysql -pmysql` 误脱命令头、防引号 token 错位 span）。验收：`mysql -psup3rsecret`/`mysql -pmysql`→`mysql -p****`(命令头保留)、`mysql -p"my secret"` 引号含空格值整体脱敏(中段 `secret` 不现、保留 `-p` 与引号)、`--password "my secret pw"` 同、`--Token X` 大小写不敏感脱敏、`mysql -p mydatabase` 不动、`redis-cli -a authpw123` 脱敏、`redis-cli -aget -aget` 两处各就位、`sshpass -p hunter2value` 脱敏、`curl -u admin:s3cr3t`/`curl --user admin:s3cr3t` 脱敏保留 admin、`curl -u admin`/`curl -u a:b:c`(partition) 正确、`myhack -p x` 不动
- [x] 3.2 接入 `redact_text` 管线：对 `_segment_commands` 每段过 `_redact_command_credentials` 后按 span 回填，位置在既有 4 类 sub 之后、C/D 之后（统一既有 `_mask`）。验收：`--password "my secret pw" --verbose` 引号值作单 token 整体脱敏（不含完整串、不含中段 `secret`；前4后4 片段按 `_mask` 保留）；`sudo docker exec c mysql -psup3rsecret` 穿透脱敏；`echo mysql -psecretliteral` 不动
- [x] 3.3 就地替换的精度验收：`redis-cli set mykey "a  b"`（双空格无凭据）输出**逐字符等于**输入（引号/双空格不被折叠）；`mysql -psup3rsecret --comment "keep  spaces"` 只脱密钥、`--comment` 段双空格保留

## 4. 负例 precision + 幂等 + 既有不回归

- [x] 4.1 在 `tests/core/test_redact.py` 加 `TestNegativeProtects`：`ps -p 1234` / `kubectl logs -p` / `PWD=/home/alice make` / `MYSQL_PASSWORD_FILE=/run/secrets/x` / `myhack -p pw` / `the --password flag`(断言 over-mask 的当前行为) / `restart the mysql service` 各自断言期望行为
- [x] 4.2 加 `test_idempotent` 参数化：对全部规则正例 `assert redact_text(redact_text(s)) == redact_text(s)`，**显式含**含空格引号值 `--password "my secret pw"` / `mysql -p"my secret"`（验证包引号使二次 fixpoint）、脏粘连引号 `mysql -p"sec"tail`（验证去全部引号取真值 `sectail`→`-p****` 二次 fixpoint）、curl 含空格密码 `curl -u admin:"se cret"`（partition 后值含空格、包引号守幂等）、`--password ****` / `mysql -p<已mask>` 二次输入。验收：`pytest tests/core/test_redact.py -q` 全绿（9 类 + 负例 + 幂等）
- [x] 4.3 跑既有 `pytest tests/core/test_redact.py tests/reporting/test_redaction_at_render_boundary.py -q` 确认**既有断言全部不变即过**（mask 策略未动，既有用例零修改）

## 5. spec MODIFY 实测 + 文档 / 注释同步

- [x] 5.1 在 temp 副本对 `specs/llm-cassette-testing/spec.md` delta 干跑 `openspec-cn archive`，确认 rebuild 校验过（中文标题、场景 4-井号、需求标题与既有完全匹配），防 archive 返工
- [x] 5.2 更新 `docs/OPERABILITY.md §7.2` 默认脱敏规则列表：补 A/B/C/D 四条（注明新规则同样保留前 4 后 4，mask 强度分级留 follow-up）
- [x] 5.3 更新 `src/hostlens/cli/fix.py` 注释（line 16 / 109 / 283-284 + line 118 hint 字符串）：「`redact_text` 不覆盖 flag 形」→「仅覆盖已知工具 flag 形，best-effort；未知工具仍漏，故 root 拒绝最早门保留」

## 6. 验收与归档

- [x] 6.1 跑 proposal Demo Path 脚本，确认 6 正例脱敏、4 负例原样（含双空格保留）
- [x] 6.2 `mypy --strict src/hostlens/core/redact.py` 通过、全量 `pytest -q` 绿（确认 mask 策略未动、下游零回归）
- [x] 6.3 `openspec-cn validate extend-redact-flag-form-secrets --strict` 通过
