# Migration: M1 to M2

M2 introduces the `LLMBackend` abstraction (see
`openspec/changes/add-llm-backend-protocol/`). M0 / M1 configs continue
to load (the new `backend` and `agent` sections are optional in the
schema), but any command that drives the Agent loop now requires the
two new sections to be present.

## Minimum config diff

Add the following blocks to `~/.config/hostlens/config.yaml`:

```yaml
# Section 1 — backend ("with whom / how to authenticate")
backend:
  type: anthropic_api
  api_key: ${ANTHROPIC_API_KEY}
  # base_url: null            # optional; used for self-hosted proxies
  # cassette_path: null       # required when type=playback

# Section 2 — agent ("which model / loop knobs")
agent:
  primary_model: claude-opus-4-7
  # fallback_model: null      # reserved for later milestones
  health_check_model: claude-haiku-4-5
  max_turns: 20
  token_budget_input: 100000
  token_budget_output: 30000
  health_check_timeout_seconds: 10
```

Both sections are independent. `backend` covers authentication and
endpoint selection; `agent` covers model identifiers and loop knobs.

## What stays the same

- All M0 / M1 CLI commands continue to load without the new sections.
- Existing target / inspector YAML manifests are unchanged.
- `hostlens doctor` automatically picks up the new `backend` section
  when present and reports `api_key_set` plus an `api_key_fingerprint`
  (first 4 + last 4 chars). The full key never appears in output.

## What changes if you do not add the sections

- Any future command that calls `create_backend(settings)` raises
  `ConfigError("backend.type required to use LLM features")`. M2
  Agent-loop commands (`hostlens inspect --intent ...`) will be the
  first consumers; the M1 inspect path still works without `backend`.

## Backend type placeholders

`backend.type` accepts `bedrock`, `vertex`, and `claude_subscription`
in the schema, but `create_backend` raises `NotImplementedError` for
all three. They land in M10.5 / 1.0; do not use them yet.

## Rollback

Remove the `backend` and `agent` blocks from the config. Pydantic v2's
`extra="ignore"` makes the rollback non-destructive: any commits
referencing the new sections still load against an M1 binary.

---

## message 改写与 finding id 一次性 churn

变更 `improve-report-rendering-and-i18n` 把 FindingRule `message` 从「静态英文串」
改写成「简短中文标签 + `{field}` 注入数据」（规约见
[inspector-authoring-contract.md §规则 10](operations/inspector-authoring-contract.md#规则-10--findingrule-message-必须是简短中文标签--注入关键数据禁空指针--禁纯英文长句)）。
本提案先落 `linux.systemd.failed_units` 旗舰样板，其余 inspector 的 message 中文化分阶段
多 PR 长尾推进——**每次**改写一批 message 的升级都会触发下面这一次性副作用。

### 现象：升级后首跑 regression diff 有一次性 `resolved` + `added`

`message` 是 `compute_finding_id(inspector_name, inspector_version, message)` 的输入
（`severity` 被刻意排除，以支持 `changed_severity` 检测）。因此**改写 message 会改变同一
问题的 finding id**。升级到含 message 改写的版本后，**首跑** regression diff 会把：

- 旧 message 对应的 finding id 报成一次 **`resolved`**（旧 id 不再出现）；
- 新 message 对应的 finding id 报成一次 **`added`**（新 id 首次出现）。

**这是 id 重置，不是真实状态变化**——同一台主机上同一个真实问题（如同样那些 failed
单元）依然存在，只是它的 finding id 因 message 文本变了而重算。运维侧应把这次
`resolved` + `added` 解读为「id 重置」，**不要**当成「问题消失了 / 冒出了新问题」。

第二跑及以后，新 id 稳定，diff 恢复正常语义。这是 message 改写的**已知且认可的
一次性**副作用，无需任何操作；若你的告警把 `added` / `resolved` 当事件推送，可在这次
升级窗口临时静默一次，或提前知会值班。

### 适用范围限定（fleet-only 部署不受影响）

该 churn 是**任何以 `compute_finding_id`（恒 hash `message`，与 diff 粒度无关）为锚的
regression diff** 的副作用。当前它**只实际发生在 agent 模式的 per-target diff** 上——
确定性模式提案（`add-deterministic-inspection-mode` 的 report-data-model）**当前不为
fleet Report 实现任何 diff**（per-target 与「同 fleet id 整体比对」的 fleet-level diff
都不做）。因此：

- **agent 模式**（per-target diff，`hostlens reports diff`）：升级后首跑会看到这次
  一次性 `resolved` + `added`。
- **fleet-only / 确定性模式部署**：当前不做任何 diff，故 message 改写**不产生** diff
  噪声。

> **注意（非「fleet 永不受影响」）**：`compute_finding_id` 与 diff 粒度无关。**若未来**
> 给 fleet Report 加**任何** baseline diff（尤其 report-data-model 预留的「同
> `meta.target_id`(fleet id) 的上一份 fleet Report 整体比对」），message 改写会**同样**
> 在该 fleet diff 上产生一次性 `resolved` / `added` churn——届时本免责须同步修订。
