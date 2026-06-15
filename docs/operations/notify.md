# `hostlens notify` Operations Guide

M5 lands the Notifier layer: rendered inspection reports are pushed to
Telegram / 飞书 (Lark) channels on a schedule, and the `hostlens notify`
CLI lets operators introspect channels, dry-run renders, and send a real
test ping. This document covers the channel config file, the `only_if`
routing expression, the three CLI subcommands, the `doctor`
`--check-channels` probe, and the no-token local Demo Path.

> Reliability parameters (concurrency / timeout / retry / truncation) live
> in [OPERABILITY.md §8](../OPERABILITY.md#8-notifier-可靠性合约). This guide
> is the user-facing how-to; OPERABILITY is the limits SOT.

## Channel configuration (`notifiers.yaml`)

Channels are configured in `~/.config/hostlens/notifiers.yaml` (path from
`Settings.notifiers_config_path`; overridable via settings). The top level
is a `channels:` mapping of `<instance name> → { type, ...fields }`. The
`type` selects the adapter (`telegram` / `lark`); the remaining fields are
the adapter's config.

```yaml
channels:
  ops-telegram:
    type: telegram
    bot_token: ${TELEGRAM_BOT_TOKEN}
    chat_id: ${TELEGRAM_CHAT_ID}
  ops-lark:
    type: lark
    webhook_url: ${LARK_WEBHOOK_URL}
    secret: ${LARK_SIGN_SECRET}        # optional; enables HMAC sign
```

Per-type fields:

| type | required | optional |
|---|---|---|
| `telegram` | `bot_token`, `chat_id` | — |
| `lark` | `webhook_url` | `secret` (enables HMAC-SHA256 signing) |

### Secrets via `${ENV_VAR}` injection

Field values may embed `${ENV_VAR}` placeholders, resolved from the
environment at load time. **Never** write a bot token / webhook URL / sign
secret in plaintext into `notifiers.yaml` or commit it — use `${ENV_VAR}`
(CLAUDE.md §7).

Injection is fail-loud and single-layer:

- `${VAR}` → `os.environ["VAR"]`; an **unset** variable raises a
  `ConfigError` naming it (never silently resolves to `""`).
- `${}` (empty name) is illegal and raises.
- A bare `$` or a malformed `${X` (no closing brace) does not match the
  placeholder pattern and is kept verbatim.
- A substituted value is not re-scanned, so injected content that happens
  to contain `${...}` is left untouched.

After expansion each channel's `validate_config` must pass — required
fields must be **present and non-empty** (an empty string counts as
missing).

## Routing: `notify` + `only_if`

A schedule manifest references channels and gates each one with an
optional `only_if` expression:

```yaml
# schedules/nightly-cpu.yaml fragment
notify:
  - channel: ops-telegram
    only_if: "severity >= warning"      # only warning/critical get pushed
  - channel: ops-lark
    only_if: "'disk_full' in tags"      # only when a finding carries this tag
```

Each entry is `{ channel, only_if }`. `only_if` is optional — omit it to
always send. The empty string `""` is **not** "always send"; it is an
illegal value that fails loud at load time (to always send, omit the
field).

### `only_if` expression language

`only_if` reuses the hardened inspector finding DSL (`inspectors.dsl`) —
the same static-AST-gated, timeout-bounded evaluator, not a raw `eval`.
The routing context binds:

- `severity` — the report's aggregate severity as an **ordered rank**
  (`info=0 < warning=1 < critical=2`). The aggregate is the max over all
  finding severities (a report with no findings derives `info`).
- `info` / `warning` / `critical` — the three name→rank bindings, so
  `severity >= warning` is a numeric comparison (not lexicographic).
- `tags` — the sorted union of every finding's `tags`, so `'x' in tags`
  works.

Examples:

| expression | sends when |
|---|---|
| `severity >= warning` | aggregate severity is warning or critical |
| `severity == critical` | aggregate severity is exactly critical |
| `'disk_full' in tags` | any finding tagged `disk_full` |
| `severity >= warning and 'oom' in tags` | both conditions hold |

Allowed constructs are whatever the inspector DSL permits (comparisons,
boolean ops, membership, and its whitelisted functions); forbidden
constructs (lambda / comprehension / `__import__` / dunder attribute /
import) are rejected by the AST gate.

**Two validation timings** (a deliberate split):

- **Load time** — the manifest loader runs every `only_if` through the
  DSL's `validate_ast`. A malformed / forbidden / empty-string expression
  fails loud *before* the scheduler ever fires. This is a syntax/AST gate;
  it does **not** resolve whether names exist, so a typo like `severty`
  passes here.
- **Run time** — the expression is evaluated against the report context.
  **Any** evaluation exception (undefined name from a typo, type mismatch,
  timeout, every `simpleeval` runtime class) is caught and recorded as a
  `NotifyResult(status="failed")` for that channel — it never bubbles out
  of notify dispatch, never changes the already-decided `RunStatus`, and
  never disturbs the other channels.

A falsy `only_if` result is a normal routing **skip**
(`NotifyResult(status="skipped")`), distinct from a failure.

## CLI

### `hostlens notify channels [--json]`

List every configured channel with its type and config-validation status
(does `validate_config` pass, are referenced env vars set). **Read-only**:
never sends, never prints a secret value.

A missing / unreadable / malformed `notifiers.yaml` produces a readable
message and a non-crash exit (empty list for `--json`, a hint line
otherwise) — never a Python traceback. Per-channel problems (unknown type,
unset env var, failed validation) are surfaced as `valid=false` rows with
a reason, so one bad channel does not hide the healthy ones.

### `hostlens notify render --report <id> --channel <name> [--only-if <expr>]`

Load a persisted Report (by `report_id`, from `hostlens reports list`),
render the target channel's native payload (Telegram MarkdownV2 text /
Lark card JSON), and write it to stdout. **Dry-run is the only behavior:
nothing is ever sent.** This is the no-token, no-network Demo Path.

- `--only-if <expr>` (optional) prints the routing decision
  (send / skip / failed + reason) to stderr without sending; stdout stays
  the rendered payload.
- A truncated payload prints a `note: payload was truncated ...` line to
  stderr.
- Unknown `report_id` / orphan-stored report / unknown channel all fail
  loud with a non-zero exit and a readable reason.

### `hostlens notify test --channel <name> [--yes]`

Really send one fixed ping message to the channel (no Report needed). As
an **outbound op**:

- a non-TTY run without `--yes` exits 1 (never sends);
- a TTY run confirms interactively.

Per the spec's audited exemption, `notify test` does **not** trigger the
global write-op EUID==0 root refusal — it creates no file and changes no
inspected-host state, it only makes one outbound HTTPS request. (A future
CLI that *writes* `notifiers.yaml` would fall under §4.5 and must refuse
root.)

### Exit codes

Project-wide `3 > 2 > 1 > 0`:

- `0` success.
- `1` business failure — unknown report / orphan / unknown channel for
  `render`; a `test` send that did not succeed; the non-TTY-no-`--yes`
  guard for `test`.
- `2` configuration error — a present-but-malformed `notifiers.yaml` for
  `render` / `test`.
- `3` usage error — missing / invalid options.

stdout carries machine output (channel list / rendered payload); stderr
carries hints and errors; no traceback ever reaches the user.

## `doctor --check-channels`

`hostlens doctor --check-channels` adds a lightweight connectivity / config
probe per configured channel, landed under `doctor --json`
`checks.channels`:

- **Telegram** — calls the Bot API `getMe` (read-only; never delivers a
  message).
- **Lark** — validates config completeness only (does not post a business
  message).

A failing probe (invalid token / missing env var / failed validation) is
marked red with a reason but does **not** affect the other doctor checks.
No `notifiers.yaml` → `status="ok"` ("no channels configured").

## Demo Path (no token, no network)

Render a persisted report to a channel's native payload, entirely offline:

1. Pick a persisted report id: `hostlens reports list <target>` (or run
   `hostlens demo` to generate one).
2. Create a minimal `~/.config/hostlens/notifiers.yaml` (the Telegram /
   Lark examples above; the token values can be any non-empty string for a
   pure render — `render` never authenticates because it never sends).
   Export the referenced env vars so `${ENV_VAR}` injection resolves.
3. `hostlens notify channels` — confirm the channel shows up `valid=true`.
4. `hostlens notify render --report <id> --channel ops-telegram --only-if "severity >= warning"`
   — the rendered MarkdownV2 (or Lark card JSON) goes to stdout; the
   routing decision goes to stderr. Nothing is sent.

Optional real smoke test (needs a real bot/webhook): set the real
secrets and run
`hostlens notify test --channel ops-telegram --yes` to deliver one ping to
your own test chat.

## 报告渲染示例（新布局）

> 变更 `improve-report-rendering-and-i18n` 重做了 Telegram / 飞书 Lark 两个模板，
> 让它们渲染**同构**的信息结构。下面的示例展示新布局的各块；模板代码见
> `notifiers/templates/{telegram/report.md.j2, lark/report.card.j2}`，共享渲染
> filter 见 `notifiers/_filters.py`。

新布局自上而下五块（两通道一致）：

1. **抬头（非 intent）**：severity 图标 + `Hostlens 巡检 · <target> · <中文 severity>`。
   **不再**把整段巡检 intent 当标题。
2. **覆盖行**：`<时间> · N/M 项检查 · K 项跳过[ · Y 项失败]`，一眼看全跑没。
   计数来自 `meta.inspectors_used`：`ok`→ok、`requires_unmet`→跳过、
   `timeout`/`target_unreachable`/`exception`→失败；不变量 `ok+跳过+失败==总数`；
   `失败==0` 时省略「· Y 项失败」子句（不挂 `· 0 项失败` 噪声尾）。
3. **根因分析（置顶）**：人最该看的放最前——中文叙述 + `↳` 处置命令（来自
   Diagnostician 的 `description` / `suggested_actions`，约束为简体中文）。
4. **发现**：渲染时**四元组去重**（`(target_name, inspector_name, message, severity)`
   全字段相等才合并）+ **按 severity 降序**（critical → warning → info）+ 每条带
   **来源 inspector**；多 target 报告**按主机分节**（单主机退化为无分节）。
5. **健康态**：无 findings 时 `✅ 未发现异常` + 覆盖行（不吵）。

> severity 图标：critical→`🔴`、warning→`⚠️`、info→`ℹ️`。
> 中文 severity / confidence 标签：`严重` / `警告` / `信息`、`高` / `中` / `低`。

### 单主机 · 有异常（Telegram，示意读法）

下面按可读性展示**转义前**的内容（真实 MarkdownV2 payload 会对动态值做
`mdv2_escape`，`*` / `↳` / `·` 是结构 / 字面字符）。

```text
🔴 Hostlens 巡检 · web-01 · 严重
2026-06-15 03:00 · 5/6 项检查 · 1 项跳过

根因分析
• nginx upstream 连续 5xx 源于后端 mysql 连接耗尽，导致请求堆积。 (置信度 高)
↳ systemctl restart mysql
↳ 检查 max_connections 与连接池配置

发现
🔴 systemd 失败服务：nginx.service, mysql.service (linux.systemd.failed_units)
⚠️ 磁盘使用率超阈值：/ 92% (linux.disk.usage)
```

要点：抬头是 `Hostlens 巡检 · web-01 · 严重`（不是 intent 整句）；覆盖行 `5/6 项检查
· 1 项跳过`（无失败子句，因失败计数为 0）；根因置顶 + `↳` 处置；发现按 severity 降序、
每条带来源 inspector；`systemd 失败服务：nginx.service, mysql.service` 注入的是干净
join 串（`failed_names`），不是 raw 数组 repr（见
[规则 10](inspector-authoring-contract.md#规则-10--findingrule-message-必须是简短中文标签--注入关键数据禁空指针--禁纯英文长句)）。

### 多主机 · 确定性模式（Telegram，按主机分节）

当一份报告含**两个及以上不同主机**的 findings（`finding.target_name` 多值），发现块
**按主机分节**。同 message 跨主机因 `target_name` 不同**不**合并（四元组去重只在主机内
全等才合并）：

```text
🔴 Hostlens 巡检 · fleet · 严重
2026-06-15 03:00 · 12/12 项检查 · 0 项跳过

发现

web-01
🔴 systemd 失败服务：nginx.service (linux.systemd.failed_units)

web-02
⚠️ 磁盘使用率超阈值：/ 88% (linux.disk.usage)
```

> 多 target 分节依赖 `Finding.target_name` 到达模板。notifier 渲染入口先
> `redact_report_for_render` 脱敏再喂模板，故分节 / 去重消费的是**脱敏拷贝**——
> `target_name` 能否到达模板取决于脱敏层透传该字段（见 design 的 redaction 边界说明）。

### 健康态（无异常）

无 findings 时只渲染抬头 + 覆盖行 + 一行 `✅ 未发现异常`，不刷屏：

```text
ℹ️ Hostlens 巡检 · web-01 · 信息
2026-06-15 03:00 · 6/6 项检查 · 0 项跳过

✅ 未发现异常
```

### 飞书 Lark 卡片

Lark 渲染**同构**的结构（抬头 → 覆盖行 → 根因分析 → 发现 → 健康态），形态是交互式
卡片 JSON：抬头走 card `header.title`（颜色由 severity 映射），覆盖行 / 根因 / 每条发现
各是一个 `lark_md` div，多主机分节插入主机名 div。去重键 + 分节逻辑与 Telegram 完全一致
（共用 `_filters.py` 的 `dedup` / `group_by_target` / `sort_sev`），只是包裹成卡片 JSON
而非 MarkdownV2 文本。dry-run 渲染见 `hostlens notify render --channel <lark 通道>`。

## Known accepted risks

- **At-most-3-attempt, at-least-once delivery**: a send is retried up to 3
  times with bounded backoff; there is no de-dup, so a retry can deliver a
  duplicate message (accepted). See OPERABILITY §8.
- **No dead-letter queue**: a channel that exhausts its retry budget is
  recorded as `NotifyResult(status="failed", error=...)` in the Run; there
  is no persistent re-delivery queue in M5 (deferred).
- **Oversized payloads are truncated**, not split: a body over the
  channel's length limit (Telegram 4096 code units / Lark card body limit)
  is clipped at a safe boundary and flagged `truncated=True`.
