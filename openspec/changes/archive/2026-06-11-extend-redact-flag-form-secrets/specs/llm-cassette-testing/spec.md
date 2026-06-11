## 修改需求

### 需求:`hostlens.core.redact` 必须暴露 cassette 共享敏感规则，且不改 `redact_text` runtime 语义

cassette 提交门禁的敏感标准比 runtime 日志脱敏更宽。`hostlens.core.redact` **必须**导出 `CASSETTE_SENSITIVE_PATTERNS`（含 sk- key / Bearer / JWT / credential 赋值 / `/Users|/home` 路径 / `.ssh` / IPv4 / email / hostname-FQDN 等规则）与 `detect_sensitive_text(text: str) -> str | None`（命中返回首个匹配的规则名，否则 `None`）。`cassette_lint.py` 与 `RecordingBackend` **必须**同源 import 这套规则，保证「录完即过 lint」。

本需求**禁止**把 cassette 门禁特有的**非-secret detection 类别**（HOME / `/Users|/home` 路径 / `.ssh` / IPv4 / email / hostname-FQDN）引入 `redact_text()` 的 runtime masking——runtime 故意保留这些非 secret 信息以助排错。runtime 的 **secret-masking 规则集**（凭据赋值 / flag 形 / URL userinfo / env 名 / JWT / sk- 等）由 `text-secret-redaction` capability 独立 own 并可扩展，**不**受本需求冻结；本需求只锁「cassette 的非-secret 类别不下沉到 runtime」这一方向。

#### 场景:detect_sensitive_text 命中返回规则名
- **当** 调用 `detect_sensitive_text("token=Bearer xyz123")`
- **那么** 返回非 None 的规则名字符串（如 `bearer_token` 或 `credential_assignment`）

#### 场景:detect_sensitive_text 干净文本返回 None
- **当** 调用 `detect_sensitive_text("hello world, connection refused")`
- **那么** 返回 `None`

#### 场景:cassette 非-secret 类别不下沉到 runtime masking
- **当** 对一个仅含 `/Users/alice` 路径（runtime 视为非 secret 可保留、cassette 门禁视为敏感）的字符串调用 `redact_text`
- **那么** runtime 不把该路径 masking——路径处理不被 cassette 规则收紧（`text-secret-redaction` 对 runtime secret 规则集的扩展不改变此非-secret 保留不变量）

#### 场景:lint 与 recorder 同源
- **当** `cassette_lint.py` 与 `RecordingBackend` 各自判定某文本是否敏感
- **那么** 两者必须基于同一份 `CASSETTE_SENSITIVE_PATTERNS`，对同一输入给出一致判定（录完的 cassette 必过 lint secret-scan）
