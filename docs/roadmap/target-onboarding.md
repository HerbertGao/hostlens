# Roadmap：服务器批量纳管（Target Onboarding）

> 状态：**探索期产物 / 已部分裁决**。本文是 `/opsx:explore add-mcp-write-approval-flow` 一轮探索的收敛结论，
> 经 Codex + Software Architect 两路独立 review 对账，并以真实首批机器（`ts.mac-mini:~/tizi` 的 6 台节点）
> 做了现实锚定。**这是记录思考，不是实现**。落地前每个提案仍须走 OpenSpec propose 流程。
>
> **已裁决（2026-06-14）**：① **Gate 0 通过**（asyncssh 直连 Tailscale SSH，6/6 cred-less 连通，见 §3）；
> ② **`add-mcp-write-approval-flow` 正式废弃**，按 §5 拆为 `add-cli-target-import`（主）+ `add-mcp-target-import-propose`（次）。
>
> **落地进度**：§5 提案 A `add-cli-target-import` 已 merged+archived（#102）；提案 B `add-mcp-target-import-propose`（MCP `propose_target_import` propose-only + CLI `target import --from-plan` 本地落地）已实现（待 PR）。`test_channel` / `notify_report` 独立小提案未起；`remove_target` 不上 MCP。

---

## 0. 用户的真实目标（一句话）

> **用户批量迁移服务器到本工程时，本工程可以快速、准确地把指定服务器加入到巡检环境内。**

注意这句话里没有「MCP」也没有「approval」。这直接推翻了原计划的载体（见 §1）。

---

## 1. 核心结论：`add-mcp-write-approval-flow` 载体用错了

M7-ext 读期（`add-mcp-readonly-management-tools`，已归档 #101）把 4 个**写工具**
`import_targets` / `remove_target` / `test_channel` / `notify_report` 显式踢给了写期提案
`add-mcp-write-approval-flow`。但该名字预设了「**MCP 写 + 远程审批机制**」——而用户的首要目标是
「**批量纳管**」，两者不是一回事。

**Codex 与架构 subagent 高度一致地否决了原载体**，理由收敛为四条：

```
┌────────────────────────────────────────────────────────────────────┐
│ 1. 变更名把「批量纳管（首要目标）」和「MCP 写审批（次要机制）」捆死  │
│ 2. 批量纳管主路径是 CLI，不是 MCP                                    │
│    50 台机逐个 approve token = 反人类；一个 token 批 50 台 = 正是    │
│    prompt-injection 想要的（把 1 台恶意机藏进 49 台正常机一起过审） │
│ 3. import_targets 上 MCP 最多 propose-only（产计划不落盘）          │
│    Fork B（两段式 token）、Fork C（低风险写自动执行）都被否：       │
│    「加一台能 SSH 进去的机器，没有低风险版本」                       │
│ 4. 审批天然是本地的：CLI --dry-run → --yes，不碰任何远程审批红线    │
│    （对齐 M9 P3 红线：AI 担不了责的写只给方案不代执行）             │
└────────────────────────────────────────────────────────────────────┘
```

**裁决建议**：废弃 / 改名 `add-mcp-write-approval-flow`，拆成下面 §5 的多个提案。

---

## 2. 现实锚定：首批机器 = `ts.mac-mini:~/tizi`（6 台）

`~/tizi` 是用户既有的「本地清单 + 并行 SSH 巡检编排器」，Hostlens 要做的本质是**用 LLM 驱动的诊断
取代/增强 `scripts/status.sh`**。这批机器把抽象设计逼到了现实：

| 节点 | 短名 | 角色 | 连接地址（SSH 实际用） | 凭据 | 备注 |
|---|---|---|---|---|---|
| bandwagon | bwg | proxy | `100.76.213.134`（tailscale v4） | **无**（Tailscale SSH） | Debian 13 |
| cloudcone | cc | proxy | `100.70.202.137` | 无 | Debian 13 |
| vultr | vultr | proxy | `100.76.141.68` | 无 | Debian 13 |
| aliyun-bj | ali-bj | relay | `100.105.141.74` | 无 | snell+DERP+RustDesk+bark |
| aliyun-hk | ali-hk | relay | `100.69.1.106` | 无 | snell+DERP |
| telegrambot | tg-bot | extra | `fd7a:115c:...:6874`（tailnet **IPv6 only**） | 无 | **FakeDNS 陷阱**，见下 |

`~/tizi` 同时存在**两种清单视图**（恰好命中用户「四种来源全选」里的两种，且并排在同一项目）：

- `hosts` —— SSH config snippet（`Include ~/tizi/hosts`），最小「怎么连」：Host/HostName/User/AddressFamily
- `inventory.yml`（16KB）—— 真理之源，按角色分组 `hosts_proxy/relay/extra`，每台带富元数据

`inventory.yml` 的 schema（已实测，secret 已脱敏）：

```yaml
defaults: { ssh_user: root }
hosts_proxy:
  bandwagon:
    short: bwg
    provider: "Bandwagon (BWG)"
    region: US
    public_ipv4: ...        # ← 5 个地址候选，选错就连不上
    public_ipv6: ...
    tailscale_ipv4: 100.76.213.134
    tailscale_name: bandwagon.zonkey-duck.ts.net
    os: Debian 13 (trixie)
    fqdn: ...
    notes: "内存 472Mi，单核。BBR + fq 已启用。"
hosts_relay: { aliyun-bj: {...}, aliyun-hk: {...} }
hosts_extra: { telegrambot: {...} }
services: { sing-box: {...}, snell: {...}, easytier: {...} }   # 含端口/密码（secret）
```

### 这批机器逼出的三条「准确性」设计点

1. **地址选择本身就是准确性问题**：每台有 `public_ipv4 / public_ipv6 / tailscale_ipv4 / tailscale_name / fqdn`
   五个候选。运维真实选择（编码在 `hosts` 里）是 **Tailscale 地址**。一个「随便抓个 IP」的导入器会选错
   （公网 IP 可能被防火墙挡、或 SSH 只在 tailnet 放行）。→ **SSH-config 是权威的「连接」来源，YAML 富化「元数据」。**
2. **`telegrambot` 是杀手级边界**：IPv6-only over tailnet，且 mac 系统 DNS 把该名字解析到 **FakeDNS
   `198.18.x.x`（Surge 段）**——裸 `ssh telegrambot` 会连到假 IP。只有 SSH-config 条目（显式 tailscale IPv6 +
   `AddressFamily inet6`）连得对。→ **导入器绝不能靠 DNS 解析主机名，必须用清单里的显式地址。**
3. **凭据模型必须支持「外部/无显式凭据」**：Tailscale SSH（ACL `tag:admin → tag:server`）让
   `ssh root@100.x` 无需 key/password 即登录 root。Hostlens 的 `SSHEntry` 已支持
   （`key_path/password/passphrase` 全 `Optional[None]`，见 `targets/config.py:106-108`）——但**能否真连**取决于
   asyncssh 对 Tailscale SSH 握手的兼容性，这是 §3 的 Gate 0。

---

## 3. Gate 0（设计前必须先验证的假设）：asyncssh 能否连 Tailscale SSH

> 全局铁律：「先验证假设再设计——1 分钟验证省 30 分钟返工。」

**这是整个纳管能力的 make-or-break 前提**，且**不显然**：

- Hostlens 的 `SSHTarget` 用 **asyncssh**（纯 Python SSH 客户端，`targets/ssh.py`），它**不认识 Tailscale**——
  只会向 `100.76.213.134:22` 开 TCP 做标准 SSH 握手。
- Tailscale SSH 是基于 **tailnet 身份 + ACL** 授权的 SSH server，对已授权对端通常接受 `none` 认证方法。
- **未知**：asyncssh 默认认证流程（agent / 默认 key / `none`）能否被 Tailscale SSH server 接受完成握手。
  OpenSSH 能（README 实测 `ssh root@100.x` 直登），但 asyncssh ≠ OpenSSH。

**验证命令（请在 `ts.mac-mini` 上自己跑——这是连生产代理节点 root，不该由 AI 代跑）**：

```sh
# 在 ts.mac-mini 上，临时 venv 探活一台 proxy 节点：
python3 -m venv /tmp/hl_probe && /tmp/hl_probe/bin/pip install -q asyncssh
/tmp/hl_probe/bin/python3 - <<'PY'
import asyncio, asyncssh
async def main():
    try:
        async with asyncssh.connect("100.76.213.134", port=22, username="root",
                                    known_hosts=None, connect_timeout=12) as c:
            r = await c.run("hostname && uname -s", timeout=10)
            print("[OK]", r.stdout.strip().replace("\n"," | "))
    except Exception as e:
        print("[FAIL]", type(e).__name__, str(e)[:200])
asyncio.run(main())
PY
rm -rf /tmp/hl_probe
```

### ✅ Gate 0 结果（2026-06-14，实测通过）

从 `ts.mac-mini` 临时 venv 跑上述探活，**6/6 全部连通**：

```
[OK] bandwagon  (proxy): bandwagon2.unclegao.com | Debian 13 trixie | kernel 6.12 amd64 | podman
[OK] cloudcone  (proxy): cloudcone              | Debian 13 trixie | 6.12 amd64 | podman
[OK] vultr      (proxy): vultr                  | Debian 13 trixie | 6.12 amd64 | podman
[OK] aliyun-bj  (relay): iZ2ze...bqxZ           | Debian 13 trixie | 6.12 amd64 | podman
[OK] aliyun-hk  (relay): aliyun-hk-1            | Debian 13 trixie | 6.12 amd64 | podman
[OK] telegrambot(extra): Telegrambot            | Debian 13 trixie | 6.12 amd64 | podman   ← IPv6-only 也连通
```

**三条确定性结论**：

1. **asyncssh 透明走 Tailscale SSH、无显式凭据即登 root** → 纳管 = 直接登记 cred-less `SSHEntry`，零额外工作。
   **`transport: openssh` 逃生舱不需要了**——本探索发现的最大潜在 scope 被消除，提案 A 全速推进。
2. **IPv6-only 的 telegrambot 用裸 IPv6 地址连通** → 印证 §2 设计点 2：导入器**必须用清单里的显式地址、绝不靠
   主机名 DNS 解析**（否则撞 FakeDNS `198.18.x`）。SSH-config / inventory 的显式 `tailscale_ipv6` 是正确来源。
3. **真实能力数据**：6 台全 Debian 13 / kernel 6.12 / amd64 / **podman（无 docker、无 kubectl）** → 它们都按
   **SSH target** 纳管（不是 docker/k8s target）；容器巡检走 SSH 跑 `podman` 命令。
   含义：`TargetProbe` 探测到的 capability 是 `{SSH, shell}`，不含 `Capability.DOCKER`——
   `target add --type docker` 这条路与这批机器无关。podman 作为「runtime 指纹」可留作 inventory 元数据，未来驱动 inspector 选择。

---

## 4. 目标架构：四层「只读 → 写」流水线

架构 subagent 给出的分解（前三层全只读，复杂度前置到 plan/preview，落盘最后一步纯机械——兑现 §4.5
「写操作前置 plan/preview」铁律）：

```
  InventorySource (Protocol，加一个来源 = 加一个文件)   ← 与 Notifier/Inspector/Target 同构
   ssh_config │ yaml │ csv │ (自然语言 → 远期/提案 B)
        │  纯解析，无 IO，确定性  →  list[CandidateTarget]   (未验证候选，非 TargetEntry)
        ▼
  TargetProbe (复用 ExecutionTarget，不另起连接栈)
        │  ① 探活  ② 能力探测  ③ OS/runtime 指纹，并发限流
        ▼  → list[ProbeResult]  (ok / unreachable / auth_failed / ...)
  ImportPlan (人可读 diff —— 写前最后的只读产物)
        │  to_add / skipped(已存在) / failed_probe
        ▼  ── --dry-run 停在这（零副作用）──┐   ── --yes 继续 ↓
  save_targets_config (config.py 现在只有 load 没有 save ← 核心缺口)
        原子写(临时文件 + os.replace) + 幂等 upsert(按 name) + ${VAR} 占位保全
        ▼  ~/.config/hostlens/targets.yaml
```

### 四个关键设计约束（落地必钉死）

1. **`InventorySource.parse()` 严禁碰网络**：解析与探测正交。SSH-config 解析失败应是「语法错」而非「连不上」，
   让 `--dry-run` 在离线也能预览来源解析。产物是 `CandidateTarget`（未验证意图）而非 `TargetEntry`（已校验契约），
   避免来源层污染配置层契约。
2. **`CandidateTarget` 不含明文密钥**：只带凭据**引用**（`${VAR}` / key_path），与现有占位机制对齐。
   CSV 写明文密码 = 反模式，source 层应拒绝或强制转 env 引用。对 tizi 这批：凭据引用为**空**（外部 Tailscale 认证）。
3. **`TargetProbe` 必须先触发一次 exec**（最大非显然陷阱）：`capabilities` 是 **lazy-probe** 的——target 构造无 IO，
   首次 `exec` 才填 capability set（`targets/base.py:120`、`registry.py:196`）。不先 exec，写进 targets.yaml 的
   capabilities 就是空集，「准确纳管」直接落空。**这是用户「准确」诉求的实现核心，spec 必须有显式断言场景。**
4. **写盘三不变量**：原子（临时文件 + `os.replace`，现 `write_text` 直接覆盖中断即损坏）/ 幂等 upsert（重跑安全：
   50 台跑挂 30 台，修好重跑只补那 30 台）/ `${VAR}` 占位保全（复用现 `add_cmd` 的 raw round-trip
   `_load_raw_targets_dict`，**不经 loader 的 expand**，否则把 `${VAR}` 展成明文 secret 写回盘——`add_cmd` 已踩过这坑）。
   部分失败语义：默认「探测成功才纳管」（`--skip-unreachable` 默认），给 `--include-unreachable` 逃生舱；
   写盘阶段计划已算好，all-or-nothing 原子落盘即可，无需复杂回滚。

---

## 5. 提案 split（废弃旧载体，按依赖顺序）

```
  提案 A (CLI 批量纳管，主)  ←──  提案 B (MCP propose-only 写，次/可选)
       │  独立可交付 = 满足用户首要目标         │  复用 A 的 source/probe，只加「远程产 plan」薄层
       ▼                                       ▼
  (Gate 0 决定是否含 transport:openssh 子项)
```

| 提案 | 一句话 scope | 关键非目标 | 依赖 |
|---|---|---|---|
| **A `add-cli-target-import`**（主） | `hostlens target import <inventory>`：`InventorySource`（ssh_config + yaml 先行，覆盖 tizi）+ `TargetProbe` 并发探测 + `ImportPlan` 预览 + `save_targets_config` 原子幂等写；`--dry-run`/`--yes`/拒 root | ❌ 不碰 MCP surface；❌ 不做自然语言来源（留 B）；❌ 不做远程审批 | Gate 0 |
| **B `add-mcp-target-import-propose`**（次/可延后） | 把 import 接进 MCP 但**只产 `ImportPlan` 不落盘**；用户拿 plan 本地 `target import --from-plan` 落地。`side_effects="read"`，**零 dispatch gate 改动** | ❌ MCP 永不写 targets.yaml；❌ 不建两段式 token；❌ `remove_target` 不进本提案 | A |
| `refactor-extract-target-probe`（可选） | 把 `target test` 命令体里的探活+能力探测提取成 `TargetProbe`，供 test/doctor/import 三方复用 | ❌ 不改 `target test` 对外契约 | —（建议先并入 A，doctor 复用需求明确再独立） |

**被原载体捆绑的 4 个写工具，最终裁决**：

```
  ┌─────────────────┬──────────────────────┬─────────────────────────────┐
  │ 写工具           │ MCP surface 裁决      │ 理由                        │
  ├─────────────────┼──────────────────────┼─────────────────────────────┤
  │ import_targets  │ propose-only          │ high 风险写，对齐 M9「给方案 │
  │                 │ (产 ImportPlan 不落盘)│ 不代执行」；side_effects=read│
  │                 │ → 提案 B              │ ≡ remediation 产 runbook    │
  ├─────────────────┼──────────────────────┼─────────────────────────────┤
  │ remove_target   │ 不上 MCP（留 CLI）    │ 删错=静默监控盲区，比加更险  │
  │                 │                       │ 无远程驱动刚需              │
  ├─────────────────┼──────────────────────┼─────────────────────────────┤
  │ test_channel    │ 独立小提案评估         │ 与纳管无关，被原载体误捆绑   │
  │                 │ (倾向受限/propose)    │ 测试消息内容须不可由 LLM 控  │
  ├─────────────────┼──────────────────────┼─────────────────────────────┤
  │ notify_report   │ 独立小提案评估         │ 数据外泄面（LLM 选报告+通道）│
  │                 │ (倾向 propose-only)   │ 与纳管无关                  │
  └─────────────────┴──────────────────────┴─────────────────────────────┘
```

> **关键**：「MCP 写审批机制」不值得作为一个独立 capability 存在——它是 B / test_channel / notify_report
> 各自的实现约束。把「审批机制」当共享提案，正是 §4.10 反模式「软分类一定失控」的翻版。

---

## 6. 代码现状缺口（提案 A 的落点，均已核实）

- `targets/config.py`：有 `load_targets_config`，**无 `save_targets_config`** → 提案 A 核心新增（原子 + 幂等）。
- `cli/target.py:325`：`cfg_path.write_text(yaml.safe_dump(...))` 非原子覆盖；`_load_raw_targets_dict`(122) 是
  `${VAR}` 占位保全的现成防线，批量写必须复用。
- `cli/target.py:208` `target add`：单台；`--host`/`--user` 必填，`--key-path`/`--password-env` 可选 →
  **cred-less SSH 已支持**（对 tizi 这批关键）；拒 root、名称冲突 exit 2 已具备。
- `targets/base.py:120` + `registry.py:196`：capabilities lazy-probe（§4 约束 3 的依据）。
- `mcp_server/tools_adapter.py:129-145`：dispatch gate (4)(5) 对 write/approval fail-closed——提案 B 走 propose-only
  正是为了**不碰这两道门**（工具静态 `side_effects="read"`）。

---

## 7. 裁决记录 / 剩余 Open Questions

**已裁决（2026-06-14）**：
1. ~~**Gate 0**：asyncssh 能否直连 Tailscale SSH？~~ → ✅ **通过**（6/6 cred-less 连通，§3）。`transport:openssh` 子项不需要。
2. ~~**废名确认**~~ → ✅ **正式废弃 `add-mcp-write-approval-flow`**，按 §5 拆为 `add-cli-target-import`（主）+ `add-mcp-target-import-propose`（次）。

**剩余待裁决**：
3. **来源优先级**：提案 A 首发 `ssh_config + yaml` 两个 source 是否足够覆盖第一批？CSV / 自然语言留后续？
   （倾向是——tizi 这批正好 ssh_config + yaml 并排，覆盖首批 100%。）
4. **元数据落点**：`inventory.yml` 的富元数据（role / provider / region / notes / runtime=podman）导入后存哪——
   target 的 `tags`？是否驱动 inspector 选择（proxy→sing-box/snell，relay→snell/DERP，extra→podman 容器）？这可能牵出
   `targets.yaml` schema 的 metadata 扩展，属独立 spec 决策。
5. **`hosts` + `inventory.yml` 合并语义**：两视图同时存在时，连接参数以 SSH-config 为权威、YAML 富化元数据——
   是否要支持「指定一个 source 为 primary、另一个为 enrich」的显式合并模式？

---

## 8. 一句话推荐路线

> **先跑 Gate 0 → 起提案 A（`add-cli-target-import`，CLI 主路径，独立满足首要目标）→ A 落地后视需要起提案 B
> （MCP propose-only，锦上添花）。废弃 `add-mcp-write-approval-flow`；`test_channel`/`notify_report` 各自独立小提案，
> 不进纳管线。** 架构清晰度收益：面试官打开能直接看到「来源 → 探测 → 规划 → 事务写」四层流水线，
> `InventorySource` 与 `Notifier`/`Inspector`/`Target` 同构的「加一个 = 加一个文件」扩展点是简历级一致性展示。
