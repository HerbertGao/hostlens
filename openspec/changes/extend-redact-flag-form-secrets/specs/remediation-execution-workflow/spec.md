## 修改需求

### 需求:audit 必须 append-only JSONL、两段式（intent + result）、记三态、脱敏、不可写不静默

系统必须把每次**真实执行**的 `hostlens fix` 以**两段式**追加写入 `~/.local/share/hostlens/audit.log`（**append-only、永不轮转、永不删除**，JSONL 每行一条）：
- **intent 记录**（执行**前**写）：who / when / target_name / plan 标识（finding_id + plan 内容 sha256）/ phase=`started`。这保证即便执行中途进程崩溃（SIGKILL/断电）也留下「曾尝试执行此 plan」的取证痕迹（缩小「改了状态却零记录」窗口）。
- **result 记录**（执行**后**写）：逐 step 三态 + rollback 结果。

`who` 必须取 **`pwd.getpwuid(os.geteuid()).pw_name` + 数值 uid**（不用可被 `$USER` 伪造的环境变量）；**`pwd.getpwuid` 抛 `KeyError`（容器 arbitrary UID 无 passwd 条目）时回退到 `str(os.geteuid())`，不崩溃**。step 失败区分三态：`precheck-blocked` / `forward-failed`（含 `exit_code` 可为 null、timed_out、或传输错误摘要）/ `verify-failed`；rollback 结果（`rollback-unavailable` / rollback 成 / rollback 败）也须记。写入前命令串过既有脱敏（`core/redact.py` `redact_text`，**best-effort**，见下「已知残留」）。**dry-run 不写 audit.log**（保持取证日志纯真实执行事件；dry-run 仅 stderr/stdout 展示将写的记录形态）。

**audit 不可写不静默，且区分时序**：执行前预检目录可写。**intent 写失败**（exec 尚未开始、零副作用）→ **中止执行**、stderr 报错、非零退出（损失为零）。**result 写失败**（exec 已完成、副作用已发生）→ stderr 明确告知「副作用已发生但审计未完整落盘」+ 已执行 step 摘要、非零退出，**不**静默吞掉。

**脱敏覆盖面与已知残留（显著声明）**：`redact_text` 覆盖 `key=value` / `Bearer` / JWT / `sk-` 形，**并覆盖已知客户端工具的 shell flag 形密钥**（`redis-cli -a <pw>` / `mysql -p<pw>` / `sshpass -p <pw>` / `curl -u user:<pw>` / `https://user:pw@host` URL userinfo 等，best-effort）。**残留泄漏面**：**未知工具**的 flag 形密钥（如 `customcli -p<pw>`）仍不被覆盖、会原样进 audit.log，而 audit.log **永不删除 → 永久留痕**。Plan 作者**仍必须**通过 `ExecutionTarget.exec` 的 `env` 参数注入密钥、禁止明文写进 `forward_cmd`（脱敏是 best-effort 不是安全边界，未知工具仍漏）。

#### 场景:成功执行两段式写 audit
- **当** 一个 plan 成功执行
- **那么** audit.log 先追加一行 intent（who/when/target/plan-hash/started），执行后再追加一行 result（各 step forward-ok/verify-ok）

#### 场景:三态失败被区分记录
- **当** 一次执行分别发生 precheck 拒 / forward 报错 / verify 失败
- **那么** audit result 对应记 `precheck-blocked` / `forward-failed`（含 `exit_code` 可 null **+ `timed_out` 字段：`true`=超时 / `false`=断连，消费方据此机械区分两种 `exit_code is None`**）/ `verify-failed`，可机械区分

#### 场景:audit 命令 best-effort 脱敏
- **当** plan 的 `forward_cmd` 含 `core/redact.py` 可识别形态的敏感串（`key=value` / `Bearer` / JWT / `sk-`，或**已知工具 flag 形**如 `mysql -p<pw>` / `redis-cli -a <pw>`）
- **那么** audit 写入与 CLI 预览展示均对该串脱敏；**注**：**未知工具**的 flag 形密钥（如 `customcli -p<pw>`）`redact_text` 不覆盖，属已知残留泄漏面（见 Security）

#### 场景:audit 不可写不静默且区分时序
- **当** audit.log 所在目录不可写
- **那么** 执行前预检报错退出（退出码 1、未发生副作用——这是执行前安全门拦截）
- **当** intent 写失败（exec 尚未开始）
- **那么** **中止执行**、stderr 报错、非零退出（零副作用、零损失）
- **当** result 写失败（exec 已完成、副作用已发生）
- **那么** stderr 明确告知「副作用已发生但审计未完整落盘」+ 已执行 step 摘要、非零退出，**不**静默吞掉
