## 上下文

M2 落地的 `AnthropicAPIBackend`（`src/hostlens/agent/backends/anthropic_api.py`）是 Anthropic-schema-first 的薄 backend。OpenRouter 在 `https://openrouter.ai/api/v1/messages` 暴露 Anthropic Messages 兼容端点，经 `tests/manual/openrouter_probe.py`（2026-06-12）实测：配 `type=anthropic_api` + `base_url` 即可经它接 DeepSeek / Qwen 等第三方模型，`x-api-key` 认证、`tool_use` schema 翻译、`cache_control` 透传三项均验证通过。接入已可用，本变更补两个配置缺口。

当前约束：
- `BackendSettings`（`core/config.py:72`，`model_config = ConfigDict(extra="forbid")`）已有 `disable_thinking: bool = False` 作为「可选 / 与 `type` 无耦合校验 / 仅 `anthropic_api` 路径消费 / 缺省不改既有行为」的先例，本变更两字段照此设计。
- `AnthropicAPIBackend.capabilities`（`anthropic_api.py:93`）现为写死 `prompt_caching=True` 的 `ClassVar[BackendCapabilities]`。
- `BackendCapabilities`（`agent/backend.py:58`）是 frozen dataclass，7 字段全必填、不可变——值类本身已是 per-instance，问题只在 backend 把它声明为类属性。
- Agent loop 的 `cache_control` 注入已按 `backend.capabilities.prompt_caching` 分支（CLAUDE.md §4.8），`check_capability_consistency` 在 `messages_create` 入口校验「prompt_caching=False 时 payload 不得含 cache_control」。

## 目标 / 非目标

**目标：**
- 让用户经配置注入任意出站 header（OpenRouter `HTTP-Referer` / `X-OpenRouter-Title`）。
- 让非 Claude 端点能声明 `prompt_caching=False`，使 Agent loop 不注入 `cache_control`，cache hit rate 指标恢复真实。
- 两项改动对真 Anthropic 路径**零行为变化**，对 M0–M9 既有配置文件向后兼容。

**非目标：**
- 不新增 `BackendType`、不新增 capability 字段、不实现其他 backend（见 proposal Non-Goals）。
- 不改 Agent loop 注入 `cache_control` 的逻辑本身；不开放其余 6 个 capability 的 per-config 覆盖。
- 不改 `quota_check` 以适配 OpenRouter quota 语义（已知限制，记入风险）。

## 决策

### D-1：capability 覆盖只开放 `prompt_caching` 单项（不开放完整 capabilities 子块）

**选 route 2（定向 `prompt_caching: bool | None`），否决 route 1（完整 capabilities 子块）与 route 3（按 model id 前缀自动推断）。**

理由（按「是否既被 branch 又随模型变」双条件定，而非按 probe 证据外推）：grep `capabilities.<field>` 全仓，Agent loop / capability gate **只** branch 在两个字段——`prompt_caching`（`loop.py:542/589` 的 `cache_control` 注入门 + `backend.py:411/419/432` 的 gate）与 `tool_use`（`backend.py:440` 的非空 tools 门）。逐字段看其余 6 个为何不开放覆盖：`tool_use` 被 branch 但对 anthropic 兼容端点恒真（probe 已证 OpenRouter 翻译 tool_use schema），无按模型配的需求；`structured_output` 是 baseline spec 明示的「Planner 强制 JSON 输出」语义依赖（`backend.py:74` 注释同）——是语义依赖而非「无消费者」，对兼容端点同样恒真；`parallel_tool_use` / `extended_thinking` / `vision` / `streaming` loop/gate 均不 branch、无消费者（`extended_thinking` / `streaming` 还因 M2 Protocol 签名不含而恒 False）。给这 6 个开 per-config 覆盖纯属 footgun——制造「声明了但无人按它分支 / 端点恒真无需配」的失真旋钮，与本变更要修的「声明 prompt_caching=True 但实际无缓存」失真同型。唯独 `prompt_caching` 同时满足双条件（被 branch + 非 Claude 上游静默忽略 `cache_control`），所以「只有它该按实例配」不只是「最小」，而是「正确」。

> 注：`openrouter_probe.py` 实测只覆盖 `tool_use` + `cache_control` 两项，不证「6 项都协议级不变」；本决策不依赖那个外推——它依赖「loop 只 branch 在 prompt_caching/tool_use」这一可 grep 的代码事实。`tool_use` 虽被消费，但它由 tools 数组是否非空驱动、对 anthropic 兼容端点是恒真能力（probe 已证），无按模型覆盖的需求；故同样不开放覆盖。

- **route 1（完整子块）**否决：啰嗦，且诱导用户错配 6 个本不该动的旋钮。
- **route 3（model id 前缀推断）**否决：魔法且脆弱——`claude-*` 前缀判定碰到 OpenRouter 的 `anthropic/claude-*` 命名、或用户自建代理的任意 model id 就崩；显式配置 > 隐式推断。

### D-2：两字段沿用 `disable_thinking` 的「type-解耦 + 仅 anthropic_api 消费」契约

`extra_headers` 与 `prompt_caching` 都**不**做 `type` 交叉校验（任意 type 可设），仅 `create_backend` 的 `anthropic_api` 分支真正消费；缺省值（`None` / `None`）保证既有路径逐字不变。这与 `disable_thinking` 的既有设计对齐，避免引入新的「字段-type 耦合校验」模式（一致性 > 局部严格）。`prompt_caching` 用 `bool | None`（而非 `bool = True`）让「未设置」与「显式 True」在配置层可区分，`create_backend` 把 `None` 与 `True` 都映射为 backend 构造的 `prompt_caching=True`。

### D-3：capabilities 从 `ClassVar` 降为 `__init__` 设置的实例属性

`AnthropicAPIBackend.__init__` 新增 `prompt_caching: bool = True`，在 `__init__` 里 `self.capabilities = BackendCapabilities(prompt_caching=prompt_caching, tool_use=True, structured_output=True, parallel_tool_use=True, extended_thinking=False, vision=True, streaming=False)`。`LLMBackend` Protocol 声明的是**实例属性** `capabilities`（structural typing 不变），`check_capability_consistency(self.capabilities, ...)` 读 `self.` 已正确。代价：`AnthropicAPIBackend.capabilities` 类级访问（无实例）不再可用——经 grep 确认仅旧测试场景这么断言，改为实例访问即可（spec delta 已把该场景从类访问改为实例访问）。这与 `FakeBackend`（capabilities 早已是 `__init__` 注入）形成一致风格。

### D-4：认证 header 防覆盖在 `create_backend` 接线层做

`create_backend` 在把 `settings.backend.extra_headers` 传给 `AnthropicAPIBackend` 前，丢弃大小写不敏感的 `x-api-key` / `authorization` 键。理由：认证以 `api_key` 字段为唯一来源（SecretStr 脱敏路径完整），`extra_headers` 是「统计/路由」用途，绝不能成为绕过 SecretStr 的认证旁路。放在接线层而非 backend `__init__`，是因为这是「配置→backend」的策略决定，backend 只负责忠实透传它收到的 header。

### D-5：`extra_headers` 值在 `__repr__` 无条件全遮蔽（不复用 `core.redact` 形态识别）

否决「走 `core.redact` 脱敏」。`core.redact` 只有 `redact_text(s: str)`（形态正则：`sk-[a-zA-Z0-9-]{20,}` / Bearer / JWT / `key=value` / URL-userinfo）与 `is_sensitive_key`（key 名匹配 `password|secret|token|api_key|bearer`）。对**任意** header 值，二者都兜不住：裸 token（如 `X-Custom-Auth: deadbeefcafe...`）不命中任何形态、`X-Custom-Auth` 这类 key 名也不命中 `is_sensitive_key`。所以「值经 `core.redact` 脱敏」是一个工具层兑现不了的安全承诺。

收口为：`AnthropicAPIBackend.__repr__`（及任何未来输出 `extra_headers` 的日志点）对 **value 无条件全遮蔽**为 `***`（keys 可保留以便排错），**不**依赖形态识别——这是对「任意 header 值」唯一成立的强保证，且实现更简单（直接 mask，不调 `redact_text`）。

输出面已核实唯一：doctor `BackendHealthRow`（`cli/_doctor_schema.py:169`）**不**含 `extra_headers`，backend 当前**无** logger 点——故脱敏义务精确锚定到 `__repr__`，不泛化到不存在的 doctor JSON / 日志旁路。`extra_headers` **定位为非密钥统计 header**，不提供 `Settings` 序列化层的 `SecretStr` 级保护（那是 `api_key` 的职责）；认证 header 由 D-4 在接线层剥离。

## 风险 / 权衡

- **[用户对非 Claude 模型忘配 `prompt_caching: false`]** → 回到本变更要修的现状（指标失真，但不影响正确性）。缓解：`docs/ARCHITECTURE.md §9` 明示「非 Claude 端点须设 `prompt_caching: false`」；doctor **不**强校验——无法可靠判定 `base_url` 背后上游模型族（D-1 route 3 同款脆弱性），强校验会误报。
- **[`prompt_caching=False` 误配到真 Anthropic]** → 仅损失缓存收益（多花 token），正确性不受影响。纯成本退化，可接受。
- **[`extra_headers` 注入非法值（如换行）]** → httpx/SDK 构造请求时报错，由既有 try/except 包装为 `BackendUnavailable`，fail-fast 不静默吞。
- **[OpenRouter quota 语义与 Anthropic 不同]** → 本变更不改 `quota_check`（仍返回 None），OpenRouter 的余额/限流不经 `quota_check` 暴露。已知限制，记入文档；未来若接 OpenRouter `/key` 余额查询另起提案。
- **[既有配置文件加载]** → 两字段缺省 `None`，`extra="forbid"` 下新增可选字段不影响旧配置；M0–M9 配置零改动仍合法。

## 迁移计划

- 纯增量：新增两个可选字段 + 一个构造参数化，无 schema 破坏、无数据迁移。
- 回滚：还原 `AnthropicAPIBackend.capabilities` 为 ClassVar、移除两字段即可；无持久化状态依赖。
- 部署：随常规 PR squash-merge；无 daemon / scheduler 兼容性顾虑（不触及历史 run 记录）。

## 待解决问题

- 无阻塞性 open question。`extra_headers` 脱敏形态已在 D-5 定死为「值无条件全遮蔽 `***`」（不留「全遮蔽 vs 仅遮蔽疑似密钥」的实现裁量，避免又退回 `core.redact` 兜不住的形态识别）。
