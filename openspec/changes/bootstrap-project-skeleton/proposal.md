## 为什么

项目目前只有 6 个设计文档（README / CLAUDE / TODO / ARCHITECTURE / OPERABILITY / openspec/config），没有任何 `src/` 代码，`pip install` 也不存在。M1 之后所有 Milestone（M1 Core 抽象、M2 Agent loop、M3 Diagnostician 等）的任务都依赖一个能 install、能 test、能 lint、能 CI、能 `hostlens doctor --json` 的标准 Python 项目骨架。**没有 M0，任何后续里程碑都无法启动**。

> 同时，按照 [TODO.md M0](../../../TODO.md) 与 [全局 CLAUDE.md] 的"先 spec 再代码"红线，这是项目第一个 OpenSpec 变更。

## 变更内容

- **新增**仓库目录结构 `src/hostlens/{agent,inspectors,targets,scheduler,notifiers,remediation,reporting,mcp_server,cli,core}/__init__.py`，与 [CLAUDE.md §3 计划目录](../../../CLAUDE.md) 完全一致
- **新增** `pyproject.toml`：项目元数据 / `[project]` 表的 `dependencies = [...]` 数组（PEP 621 runtime deps：typer / rich / pydantic / pydantic-settings / structlog）/ `[project.optional-dependencies]` 表分 `dev` / `mcp` / `docs` 三组（`mcp` 与 `docs` M0 留空占位）/ `[project.scripts] hostlens = "hostlens.cli:app"` / 锁定 Python `>=3.11`
- **新增** 代码质量工具链：`ruff`（lint + format）/ `mypy --strict` / `pre-commit` 配置
- **新增** 测试基线：`pytest` + `pytest-asyncio` (asyncio_mode=auto) + `pytest-cov` 配置 + 2 个 smoke 测试（1 sync + 1 async，验证 import 与 async 标记机制可用）
- **新增** GitHub Actions CI workflow（lint + mypy + pytest matrix: Python 3.11 / 3.12）
- **新增** 核心服务最小骨架：`core/config.py`（pydantic-settings）/ `core/logging.py`（structlog dev=human / prod=json）/ `core/exceptions.py`（`HostlensError` 基类 + 几个子类）
- **新增** CLI 骨架：`cli/__init__.py`（Typer app + 子命令注册位）/ `cli/doctor.py`（检查 Python 版本 / `ANTHROPIC_API_KEY` 存在性 / 配置目录可读性 / `--json` 输出）
- **新增** `LICENSE`（Apache-2.0）

**非目标 (Non-Goals)**：

- ❌ **不**实现任何业务能力（Inspector / ExecutionTarget / Notifier / Agent loop / Tool Registry / LLMBackend 全部留到 M1+）
- ❌ **不**调任何 LLM（doctor 仅检查 `ANTHROPIC_API_KEY` 存在性，不发请求）
- ❌ **不**实现完整 `BackendDiagnostics` —— 仅 doctor 的最基础版本（Python 版本 / env / 配置目录）
- ❌ **不**做 `setup.py` / `setup.cfg` 兼容（pyproject.toml 单文件）
- ❌ **不**支持 Python <3.11（async 特性 + `match` / `Self` 类型注解需要 3.11+）
- ❌ **不**集成生产依赖（AsyncSSH / docker-py / kubernetes / anthropic SDK 等）—— M0 **既不声明也不安装**这些依赖；`mcp` / `docs` extras 留空占位等待 M7 / M10 真用上时再填入对应包
- ❌ **不**实现 CLI 写操作 safety helper（`ensure_non_root_for_write` / `require_yes_for_non_tty_write`）—— M0 唯一命令 `hostlens doctor` 是只读，无写操作；这些 helper 推到 **M1 第一个写操作命令（如 `target add`）** 落地时同步引入，避免无写命令场景下空抽象

## 功能 (Capabilities)

### 新增功能

- `project-skeleton`: 仓库布局 + pyproject.toml + 依赖分组 + lint/test/CI 基础设施 + LICENSE / .gitignore
- `cli-foundation`: Typer CLI app + 子命令注册机制 + `hostlens doctor` 最早期版本（含 `--json` 输出 schema）
- `core-services`: `Config`（pydantic-settings）/ `Logging`（structlog）/ `Exceptions` 基类（`HostlensError` + `ConfigError` / `TargetError` / `InspectorError` 子类）

### 修改功能

无（首个变更，无既有 spec）

## 影响

### 对外契约影响

- **新增 CLI 命令**：`hostlens --help` / `hostlens doctor [--json]`（输出 schema 在 specs delta 中定义）
- **新增配置文件位置**：`~/.config/hostlens/`（仅探测可读性，M0 不读任何具体配置文件）
- **新增 entrypoint**：`hostlens` 命令（pip install 后可全局调用）
- **新增 env var**：`ANTHROPIC_API_KEY`（doctor 检查存在性；M0 不实际使用，为 M2 LLMBackend 预留）
- **不影响**：Inspector schema / Agent tool schema / MCP tool schema / Notifier Protocol / Schedule manifest / Tool Registry（全部 M0 未实现）

### Failure Modes

| 故障场景 | 表现 | 降级行为 |
|---|---|---|
| `pip install -e ".[dev]"` 失败（依赖冲突 / Python 版本不符） | `pip` 报错 | 用户自行修复 Python 环境（doctor 在装好之后才能跑） |
| `hostlens doctor` 时 `ANTHROPIC_API_KEY` 未设置 | doctor 输出 `{"checks": {"anthropic_key": {"status": "missing"}}}` | 非致命，doctor 返回 exit 0（M0 不强制要求；M2 引入 LLMBackend 后才强制） |
| `~/.config/hostlens/` 不可读 | doctor 输出 `{"checks": {"config_dir": {"status": "unreadable"}}}` | exit 1，明确指引用户 `mkdir -p ~/.config/hostlens && chmod 755` |
| `pytest` 在 `src/` 找不到模块 | pytest 报 import error | 明确 `pyproject.toml` `[tool.pytest.ini_options]` 配置 `pythonpath = ["src"]` |
| CI workflow 在某个 Python 版本失败 | GitHub Actions 红 | 立即修复（M0 后续所有 PR 强制 CI 绿） |

### Operational Limits

M0 阶段无业务执行（无 daemon / 无 LLM 调用 / 无 SSH 连接），但仍需定义脚手架阶段的运维约束：

| 维度 | M0 上限 / 行为 |
|---|---|
| `hostlens doctor` 本地检查总耗时 | ≤2 秒（无网络调用，全部走 stdlib + 文件系统） |
| CI workflow 单 job 总耗时（lint + mypy + pytest） | ≤5 分钟（GitHub Actions free tier 1 job 2 vCPU） |
| 内存预算 | 无显式限制（无长驻进程；doctor 与 pytest 在 std memory 内完成） |
| 并发预算 | 不适用（M0 无业务执行；M1+ 引入 ExecutionTarget 时按 [OPERABILITY.md §1](../../../docs/OPERABILITY.md) 定义） |
| 网络访问 | doctor 与所有 M0 命令**禁止**外网调用；CI 仅需 PyPI 下载依赖 |
| 文件系统写入 | doctor 只读；pytest 写 `.pytest_cache/` / `.coverage`；其他命令不写 `$HOME` 之外 |

完整运维约束（daemon 并发 / API quota / 报告存储等）见 [docs/OPERABILITY.md](../../../docs/OPERABILITY.md)，从 M1+ 起逐步生效。

### Security & Secrets

- **不引入**任何持久化密钥存储 —— 仅探测 `ANTHROPIC_API_KEY` env var 存在性
- **不写日志**任何环境变量值 —— `core/logging.py` 配置默认不打印 env dump
- **doctor 输出脱敏** —— 即使 `ANTHROPIC_API_KEY` 存在，doctor 也不打印其值，只输出 `{"status": "present", "detail": null}`（与 design D-9 / spec / task 7.4 完全一致；`detail` 严格为 `null`，禁止任何前缀 / 后缀 / 掩码）
- **`.gitignore` 已防御性排除** `.env` / `.env.*` / `.env.local` / `*.pem` / `*.key` / `credentials.json`；同时白名单 `!.env.example`（允许提交示例模板）；M0 task 1.3 验收时需 grep 确认这些条目全部存在
- **不扩大攻击面** —— 无网络监听 / 无 IPC / 无文件写到 `$HOME` 之外（即使写也只是 logs）

### Cost / Quota Impact

**零**。M0 不调任何 LLM，不消耗 Anthropic token。`hostlens doctor` 全部走本地检查。

### Demo Path

**5 分钟本地 reproduce 路径**（**无目标主机 SSH 接入、无付费 API 调用**；需 GitHub 与 PyPI 网络访问完成首次 clone 与 pip install，之后所有验收步骤离线可跑）：

```bash
# 首次 clone (需要 GitHub 网络; SSH 协议需要 git SSH key, 或可改 https:// URL)
git clone git@github.com:HerbertGao/hostlens.git    # 或: git clone https://github.com/HerbertGao/hostlens.git
cd hostlens
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"                              # 需要 PyPI 网络

# 验收 1: CLI 入口可用
hostlens --help

# 验收 2: doctor JSON 输出合法且符合 specs cli-foundation §需求:`hostlens doctor --json` schema
hostlens doctor --json | jq .
# 期望: {
#   "version": "0.1.0",
#   "timestamp": "<ISO8601>",
#   "checks": {
#     "python_version": {"status": "ok", "detail": "3.11.x"},
#     "anthropic_key":  {"status": "present" | "missing", "detail": null},
#     "config_dir":     {"status": "ok" | "unreadable" | "missing", "detail": null, "path": "..."}
#   },
#   "ready": true | false
# }

# 验收 3: 测试管线
pytest -v

# 验收 4: lint / 类型
pre-commit run --all-files
mypy --strict src/
```

四条命令全部 exit 0 即代表 M0 退出条件达成。
